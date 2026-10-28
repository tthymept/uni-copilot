"""
The `uni-copilot` command: plain scripts, no AI.

In plain words:
  uni-copilot sync     fetch NTULearn into the local store (about 1 minute),
                       print what changed per source
  uni-copilot status   how fresh the store is: last sync per source ("as of"),
                       errors, and how many events and diary lines it holds
  uni-copilot events   deadlines due in the next N days and posts from the
                       last N days, from the store (instant, no NTULearn)
  uni-copilot changes  the diary of the last N days, with titles
  uni-copilot notify "<text>"   send a plain-text Telegram message to your chat
                       ("-" instead of the text reads it from stdin)
  uni-copilot telegram-chat-id  setup: find your chat id after you messaged the bot
  uni-copilot remind   send the deadline reminders (24 h / 2 h) and sync alert
                       that are due now, if any; run every 30 min (step 6)
  uni-copilot done "<words or id>"   mark a deadline done (handed in outside
                       NTULearn) so its reminders stop; --undo takes it back
  uni-copilot digest   the morning digest, run every 30 min: acts once a day
                       after 07:30 (syncing first if the store is old), with
                       Claude Code headless and no window; --force runs it now

How a command reaches the code (same idea as the MCP tools in Phase 1):
  pyproject.toml  [project.scripts] uni-copilot = "uni_copilot.cli:main"
    -> uv made a small uni-copilot.exe in .venv/Scripts that calls main()
    -> main() reads the words after `uni-copilot` with argparse
    -> "sync" runs cmd_sync(), which calls store.sync.run_sync(...)

Exit code (what Task Scheduler sees in step 12): 0 = every source worked,
1 = at least one source failed (its old data is kept, nothing marked removed).
For `remind`: 0 = sent or nothing to send, 1 = sending failed (nothing is
recorded, so the next run tries again). For `digest`: 0 = done, not due, or
already done today; 1 = Claude failed (tried again at the next run, up to 3
times a day).

Scheduling rule ("poke often, decide in code", 28 Oct): Task Scheduler only
pokes `remind` and `digest` every 30 minutes; the code decides whether
anything is due, from what the store remembers. Missed runs (sleep, reboot)
then don't matter, and another scheduler (cron, systemd) could poke instead.

--log FILE (step 12): everything the command prints goes to the end of FILE
instead of the screen, starting with a time stamp line. The scheduled task
runs with pythonw.exe (no window), which has no screen to print to.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import sqlite3
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path

from uni_copilot.filelock import file_lock
from uni_copilot.ntulearn_mcp.config import DATA_DIR, PROJECT_ROOT, SGT
from uni_copilot.ntulearn_mcp.models import sgt_text
from uni_copilot.notify.telegram import (
    ENV_FILE,
    TelegramBot,
    TelegramError,
    TelegramNotConfigured,
    load_telegram_settings,
    strip_html,
)
from uni_copilot.store.db import (
    DB_PATH,
    clear_marked_done,
    get_state,
    marked_done_times,
    open_db,
    row_to_event,
    schema_version,
    set_marked_done,
    set_state,
    time_from_db,
    time_to_db,
)
from uni_copilot.store.models import Event
from uni_copilot.store.queries import EventChange, changes_since, first_sync_times, freshness, is_first_sync, list_events
from uni_copilot.store.reminders import format_message, plan_reminders, record_sent
from uni_copilot.store.sync import run_sync

LOG_MAX_BYTES = 1_000_000  # ~1 MB, about two years of sync logs at 9 runs a day

# One sync at a time across programs (scheduled sync, the digest's sync).
SYNC_LOCK = DATA_DIR / "sync.lock"

# The digest (28 Oct): once a day, after 07:30 SGT; sync first if the
# deadlines are older than 3 hours (the PC was asleep); at most 3 tries a day.
DIGEST_AFTER = day_time(7, 30)
DIGEST_STALE = timedelta(hours=3)
DIGEST_MAX_TRIES = 3
DIGEST_TIMEOUT_S = 600
DIGEST_PROMPT = ("Scheduled morning digest: nobody is at the PC. Use the morning-digest skill: check what's new "
                 "on the telegram channel and send the digest only if something changed. Never ask questions. "
                 "End with the skill's one-line result.")
# Locked like the Telegram session: built-in tools Skill and ToolSearch only,
# no connectors or Chrome, no NTULearn login; the 5 pilot tools pre-allowed.
DIGEST_ALLOWED = ["mcp__pilot__whats_new", "mcp__pilot__list_events", "mcp__pilot__get_event",
                  "mcp__pilot__send_telegram", "mcp__pilot__mark_seen"]

# Friendly names for the sources, in the order they're shown.
SOURCE_LABELS = {
    "ntulearn_gradebook": "deadlines",
    "ntulearn_announcement": "announcements",
    "ntulearn_message": "messages",
}


# Reads the command line and runs the chosen command. Returns the exit code.
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uni-copilot", description="uni-copilot: NTU academic helper (no AI).")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=argparse.SUPPRESS)  # tests use a temp file
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("sync", help="fetch NTULearn into the local store (GET only, about 1 minute)")
    commands.add_parser("status", help="how fresh the store is, and what it holds")
    events = commands.add_parser("events", help="deadlines coming up and recent posts, from the store")
    events.add_argument("--days", type=int, default=7, help="deadlines due in the next N days, posts from the last N days (default 7)")
    events.add_argument("--kind", choices=["deadline", "announcement", "message"], action="append",
                        help="only this kind (repeat for several)")
    events.add_argument("--course", help="part of a course code, e.g. DA3")
    changes = commands.add_parser("changes", help="the diary of changes, with titles")
    changes.add_argument("--days", type=int, default=7, help="changes found in the last N days (default 7)")
    notify = commands.add_parser("notify", help="send a plain-text Telegram message to your chat")
    notify.add_argument("text", help='the message, or "-" to read it from stdin')
    commands.add_parser("telegram-chat-id", help="setup: find your chat id (send the bot a message first)")
    remind = commands.add_parser("remind", help="send the deadline reminders and sync alert due now, if any")
    remind.add_argument("--dry-run", action="store_true", help="show what would be sent; send and record nothing")
    done = commands.add_parser("done", help="mark a deadline done (handed in outside NTULearn); stops its reminders")
    done.add_argument("what", help='words from the course code and title ("IE2108 lab 4"), or the event id')
    done.add_argument("--undo", action="store_true", help="remove your done mark again")
    digest = commands.add_parser("digest", help="the morning digest (once a day after 07:30; run every 30 min)")
    digest.add_argument("--force", action="store_true", help="run it now, even before 07:30 or if done today")
    digest.add_argument("--claude", help="full path to claude.exe (default: found next to the npm install)")
    parser.add_argument("--log", type=Path, help="append all output to this file instead of the screen")
    args = parser.parse_args(argv)

    if args.log is None:
        return _run(args)
    with open_log(args.log) as log_file:
        sys.stdout = sys.stderr = log_file  # every print and warning now goes to the file
        try:
            print(f"=== {sgt_text(datetime.now(timezone.utc))}  uni-copilot {args.command}")
            code = _run(args)
            print(f"=== exit code {code}")
            return code
        except Exception as e:  # noqa: BLE001 - a crash must still end up in the log
            print(f"=== CRASHED: {type(e).__name__}: {e}")
            return 2
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


# Runs the chosen command (after --log, if any, has redirected the output).
def _run(args: argparse.Namespace) -> int:
    # Phase 1 clients log every step at INFO; for a command line only warnings matter.
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s", stream=sys.stderr, force=True)
    if args.command == "events":
        return cmd_events(args.db, args.days, args.kind, args.course)
    if args.command == "changes":
        return cmd_changes(args.db, args.days)
    if args.command == "notify":
        return cmd_notify(args.text)
    if args.command == "telegram-chat-id":
        return cmd_telegram_chat_id()
    if args.command == "remind":
        return cmd_remind(args.db, args.dry_run)
    if args.command == "done":
        return cmd_done(args.db, args.what, args.undo)
    if args.command == "digest":
        return cmd_digest(args.db, args.force, args.claude)
    run = {"sync": cmd_sync, "status": cmd_status}[args.command]
    return run(args.db)


# Opens the log file for adding to its end. If it's grown past 1 MB, the old
# one is kept as <name>.old (replacing any older one) and a new file starts.
# Line-buffered (buffering=1): each line is written at once, so a run that
# Windows cuts off still leaves its lines (on 28 Oct two runs left nothing).
def open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".old"))
    return open(path, "a", encoding="utf-8", buffering=1)


# `uni-copilot sync`: one sync, unless another program is syncing right now
# (then this one skips: that sync brings the same fresh data).
def cmd_sync(db: Path) -> int:
    with file_lock(SYNC_LOCK) as got:
        if not got:
            print("Another sync is running; skipped this one.")
            return 0
        return _sync(db)


# One full sync into the store, one line per source (call with SYNC_LOCK held).
def _sync(db: Path) -> int:
    conn = open_db(db)
    print("Syncing NTULearn (GET only, about 1 minute)...")
    started = datetime.now(timezone.utc)  # the sync's time stamp = the "as of" time saved in state
    reports = run_sync(conn, now=started)
    conn.close()

    for r in reports:
        label = SOURCE_LABELS.get(r.source, r.source)
        if r.ok:
            changes = ", ".join(f"{kind} {n}" for kind, n in sorted(r.counts.items())) or "no changes"
            skipped = "" if r.removals_checked else "  (removals skipped: a course couldn't be read)"
            print(f"  {label:<14} {r.seconds:4.0f} s  {r.seen:3} seen   {changes}{skipped}")
        else:
            print(f"  {label:<14} {r.seconds:4.0f} s  FAILED: {r.error}")
        for note in r.notes:
            print(f"  {'':<14} note: {note}")

    failed = [r for r in reports if not r.ok]
    total = sum(sum(r.counts.values()) for r in reports)
    if failed:
        print(f"Done with problems: {len(failed)} source(s) failed; their old data was kept. {total} change(s).")
        if any("LoginRequired" in (r.error or "") for r in failed):
            print("Sign-in needed: ask Claude Code to run the ntulearn `login` tool, then sync again.")
        return 1
    print(f"Done. As of {sgt_text(started)}. {total} change(s).")
    return 0


# `uni-copilot status`: last sync per source, errors, and what the store holds.
def cmd_status(db: Path) -> int:
    if not db.exists():
        print(f"No store yet ({db}). Run: uv run uni-copilot sync")
        return 1
    conn = open_db(db)
    now = datetime.now(timezone.utc)
    print(f"Store: {db} (schema version {schema_version(conn)})")
    for source, label in SOURCE_LABELS.items():
        active, total = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE removed_at IS NULL), COUNT(*) FROM events WHERE source = ?", (source,)
        ).fetchone()
        last = time_from_db(get_state(conn, f"last_sync:{source}"))
        error = get_state(conn, f"last_error:{source}")
        when = f"as of {sgt_text(last)} ({ago(last, now)})" if last else "never synced"
        problem = f"  last try FAILED: {error}" if error else ""
        print(f"  {label:<14} {active:3} active ({total} stored)  {when}{problem}")
        notes = get_state(conn, f"last_notes:{source}")
        for note in (notes or "").splitlines():
            print(f"  {'':<14} note: {note}")
    lines, latest = conn.execute("SELECT COUNT(*), MAX(changed_at) FROM event_changes").fetchone()
    latest_text = f", latest {sgt_text(time_from_db(latest))}" if latest else ""
    print(f"  diary          {lines} line(s){latest_text}")
    sent, last_sent = conn.execute("SELECT COUNT(*), MAX(sent_at) FROM notifications_sent").fetchone()
    last_text = f", latest {sgt_text(time_from_db(last_sent))}" if last_sent else ""
    print(f"  reminders      {sent} sent{last_text}")
    print(f"  marked done    {len(marked_done_times(conn))} by you")
    conn.close()
    return 0


# `uni-copilot events`: deadlines due soon and recent posts, straight from the store.
def cmd_events(db: Path, days: int, kinds: list[str] | None, course: str | None) -> int:
    if not db.exists():
        print(f"No store yet ({db}). Run: uv run uni-copilot sync")
        return 1
    conn = open_db(db)
    now = datetime.now(timezone.utc)
    print(as_of_line(conn, now))
    items = list_events(conn, days=days, kinds=kinds, course=course, now=now)
    marked = marked_done_times(conn)
    conn.close()

    deadlines = [e for e in items if e.kind == "deadline"]
    posts = [e for e in items if e.kind != "deadline"]
    if not kinds or "deadline" in kinds:
        print(f"Deadlines due in the next {days} day(s): {len(deadlines)}")
        for e in deadlines:
            status = "done*" if e.event_id in marked else e.status
            print(f"  {sgt_text(e.due_at)}  {e.course_code:<7} {status:<9}  {e.title}")
        if any(e.event_id in marked for e in deadlines):
            print("  * marked done by you (uni-copilot done)")
    if not kinds or set(kinds) - {"deadline"}:
        print(f"Posts from the last {days} day(s): {len(posts)}")
        for e in posts:
            print(f"  {sgt_text(e.posted_at)}  {e.course_code:<7} {e.kind:<12}  {e.title}")
    return 0


# `uni-copilot changes`: the diary of the last N days. The first-sync import
# (everything found the first time) is shown as one summary line per source.
def cmd_changes(db: Path, days: int) -> int:
    if not db.exists():
        print(f"No store yet ({db}). Run: uv run uni-copilot sync")
        return 1
    conn = open_db(db)
    now = datetime.now(timezone.utc)
    print(as_of_line(conn, now))
    items = changes_since(conn, now - timedelta(days=days))
    first = first_sync_times(conn)
    conn.close()

    print(f"Changes found in the last {days} day(s):")
    shown_first: set[str] = set()
    for item in items:
        source = item.event.source
        if is_first_sync(item, first):
            if source not in shown_first:
                count = sum(1 for i in items if i.event.source == source and is_first_sync(i, first))
                label = SOURCE_LABELS.get(source, source)
                print(f"  {sgt_text(item.change.changed_at)}  first sync: started watching {count} {label}")
                shown_first.add(source)
            continue
        print(f"  {sgt_text(item.change.changed_at)}  {describe_change(item)}")
    if not items:
        print("  none")
    return 0


# `uni-copilot notify "<text>"`: one plain-text message to your chat.
# "-" reads the text from stdin, for long or multi-line text, e.g.
#   Get-Content note.txt | uv run uni-copilot notify -
# stdin is read as UTF-8 (Python on Windows would otherwise guess the old
# Windows code page). Never prints the token or the chat id.
def cmd_notify(text: str) -> int:
    if text == "-":
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        text = sys.stdin.read()
    try:
        bot = TelegramBot(load_telegram_settings())
        sent = bot.send_message(text)
    except (TelegramNotConfigured, TelegramError, ValueError) as e:
        print(f"Not sent: {e}")
        return 1
    print(f"Sent ({sent.parts} message{'s' if sent.parts > 1 else ''}).")
    return 0


# `uni-copilot telegram-chat-id`: setup helper. Lists the private chats that
# messaged the bot recently (Telegram keeps them for 24 h) with first names,
# so you can copy your chat id into .env. Run it before the Telegram plugin is
# set up: only one program may read the bot's messages.
def cmd_telegram_chat_id() -> int:
    try:
        settings = load_telegram_settings(need_chat_id=False)
        bot = TelegramBot(settings)
        username = bot.get_me()
        chats = bot.find_chat_ids()
    except (TelegramNotConfigured, TelegramError) as e:
        print(f"Couldn't check: {e}")
        return 1

    if not chats:
        print(f"No messages to @{username} in the last 24 h.")
        print(f"Open t.me/{username} in Telegram, press Start, send \"hi\", then run this again.")
        return 1
    print(f"Private chats that messaged @{username} recently:")
    for chat_id, first_name in chats:
        mark = "  (already in .env)" if chat_id == settings.chat_id else ""
        print(f"  {chat_id}  {first_name}{mark}")
    if settings.chat_id in {chat_id for chat_id, _ in chats}:
        return 0
    print(f"Add this line to {ENV_FILE.name} (in {ENV_FILE.parent}):")
    if len(chats) == 1:
        print(f"  TELEGRAM_CHAT_ID={chats[0][0]}")
    else:
        print("  TELEGRAM_CHAT_ID=<the number on the line with your first name>")
        print("More than one person messaged the bot; only yours belongs in .env.")
    return 0


# `uni-copilot remind`: sends the reminders and sync alert due now as one
# Telegram message, then records them. Records only after Telegram accepted
# the message, so a failed send is tried again next run. Prints counts and
# course codes only (it runs every 30 min into .data\remind.log).
# --dry-run prints the message as plain text and sends/records nothing.
def cmd_remind(db: Path, dry_run: bool) -> int:
    if not db.exists():
        print(f"No store yet ({db}). Run: uv run uni-copilot sync")
        return 1
    conn = open_db(db)
    now = datetime.now(timezone.utc)
    plan = plan_reminders(conn, now)
    if plan.is_empty:
        conn.close()
        print("Nothing to send.")
        return 0

    what = ", ".join(f"{r.event.course_code} ({r.reminder})" for r in plan.reminders)
    if plan.sync_alert:
        what = ", ".join(filter(None, ["sync alert", what]))
    text = format_message(plan, now)
    if dry_run:
        conn.close()
        print(f"Would send: {what}. Nothing sent or recorded. The message (formatting removed):")
        for line in strip_html(text).splitlines():
            print(f"  | {line}")
        return 0

    try:
        sent = TelegramBot(load_telegram_settings()).send_message(text, html=True)
    except (TelegramNotConfigured, TelegramError) as e:
        conn.close()
        print(f"Not sent ({what}): {e}. Nothing recorded; the next run tries again.")
        return 1
    record_sent(conn, plan, datetime.now(timezone.utc))
    conn.close()
    fallback = f", {sent.plain_fallbacks} resent as plain text" if sent.plain_fallbacks else ""
    print(f"Sent: {what} ({sent.parts} message{'s' if sent.parts > 1 else ''}{fallback}).")
    return 0


# `uni-copilot done "<words or id>"`: marks one deadline done by you, so its
# reminders stop and lists show it as done. Finds it by event id, by NTULearn
# id ("_1234_1"), or by words that all appear in its course code + title
# ("ie2108 lab 4"). Only upcoming, not-removed deadlines can be marked; --undo
# looks among the ones you marked. Several matches -> lists them, marks nothing.
def cmd_done(db: Path, what: str, undo: bool) -> int:
    if not db.exists():
        print(f"No store yet ({db}). Run: uv run uni-copilot sync")
        return 1
    conn = open_db(db)
    now = datetime.now(timezone.utc)
    marked = marked_done_times(conn)
    if undo:
        candidates = [e for e in _deadlines(conn) if e.event_id in marked]
    else:
        candidates = [e for e in _deadlines(conn) if e.due_at > now and e.removed_at is None]
    matches = _match(candidates, what)

    if len(matches) != 1:
        conn.close()
        where = "marked-done deadlines" if undo else "upcoming deadlines"
        if not matches:
            print(f"No match for {what!r} among {where}. Try a course code and a word from the title.")
        else:
            print(f"{len(matches)} {where} match {what!r}; use more words or the id:")
            for e in matches[:10]:
                print(f"  {e.event_id}  {e.course_code:<7} {sgt_text(e.due_at)}  {e.title}")
        return 1

    event = matches[0]
    label = f"{event.course_code} {event.title} (due {sgt_text(event.due_at)})"
    with conn:
        changed = clear_marked_done(conn, event.event_id) if undo else set_marked_done(conn, event.event_id, now)
    conn.close()
    if undo:
        print(f"Undone: {label} counts as NTULearn says again ({event.status}).")
    elif changed:
        print(f"Marked done: {label}. No more reminders for it.")
        print(f'Undo: uv run uni-copilot done --undo "{event.event_id}"')
    else:
        print(f"Already marked done since {sgt_text(marked[event.event_id])}: {label}.")
    return 0


# `uni-copilot digest`: poked every 30 minutes; decides for itself.
#   - before 07:30 SGT, or already done today -> nothing (unless --force);
#   - 3 failed tries today -> gives up until tomorrow (each try uses your plan);
#   - deadlines older than 3 h (the PC was asleep) -> sync first, or wait for
#     a sync already running, so a late digest isn't built on old data;
#   - then Claude Code headless with the morning-digest skill, no window.
# "Done today" is saved when Claude finished OK, also when there was nothing
# new to send (the skill then sends nothing).
def cmd_digest(db: Path, force: bool, claude: str | None) -> int:
    if not db.exists():
        print(f"No store yet ({db}). Run: uv run uni-copilot sync")
        return 1
    now = datetime.now(timezone.utc)
    today = now.astimezone(SGT).date().isoformat()
    conn = open_db(db)
    done_day = get_state(conn, "digest_done")
    day, _, n = (get_state(conn, "digest_tries") or "").partition(":")
    tries = int(n) if day == today and n.isdigit() else 0
    last_sync = freshness(conn).last_sync.get("ntulearn_gradebook")
    conn.close()

    if not force:
        if now.astimezone(SGT).time() < DIGEST_AFTER:
            print("Not yet: the digest runs after 07:30.")
            return 0
        if done_day == today:
            print("Already done today.")
            return 0
        if tries >= DIGEST_MAX_TRIES:
            print(f"Gave up for today after {tries} failed tries; see the lines above.")
            return 0

    if last_sync is None or now - last_sync > DIGEST_STALE:
        _sync_before_digest(db, last_sync)

    print("Running the morning digest (Claude Code headless)...")
    code, output = run_claude_digest(claude)
    print(output or "(no output)")
    ok = code == 0 and "Not sent:" not in output  # the skill's line when sending failed
    conn = open_db(db)
    with conn:
        if ok:
            set_state(conn, "digest_done", today)
        else:
            set_state(conn, "digest_tries", f"{today}:{tries + 1}")
    conn.close()
    return 0 if ok else 1


# The store is old: sync first. If another program is syncing, wait up to 3
# minutes for it, then check again (it has probably just brought fresh data).
def _sync_before_digest(db: Path, last_sync: datetime | None) -> None:
    print(f"Deadlines are from {sgt_text(last_sync) if last_sync else 'never'}: syncing first.")
    with file_lock(SYNC_LOCK, wait_s=180) as got:
        if not got:
            print("Another sync is still running; going ahead with the store as it is.")
            return
        conn = open_db(db)
        fresh = freshness(conn).last_sync.get("ntulearn_gradebook")
        conn.close()
        if fresh is not None and fresh != last_sync and datetime.now(timezone.utc) - fresh <= DIGEST_STALE:
            print(f"Another sync just finished (as of {sgt_text(fresh)}).")
            return
        _sync(db)


# Runs `claude -p` with the morning-digest prompt and returns (exit code,
# output). No window: CREATE_NO_WINDOW, and claude.exe is started directly
# (not through claude.cmd, so no cmd.exe console either). Locked down like the
# Telegram session; ANTHROPIC_API_KEY removed so it always uses your plan.
def run_claude_digest(claude: str | None = None) -> tuple[int, str]:
    exe = claude or find_claude()
    if not exe:
        return 1, "Claude Code not found: pass --claude <full path to claude.exe>."
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    command = [exe, "-p", DIGEST_PROMPT, "--tools", "Skill,ToolSearch", "--no-chrome",
               "--allowedTools", *DIGEST_ALLOWED, "--disallowedTools", "mcp__ntulearn__login"]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(command, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=DIGEST_TIMEOUT_S,
                           creationflags=flags)
    except subprocess.TimeoutExpired:
        return 1, f"Claude took over {DIGEST_TIMEOUT_S // 60} minutes; stopped."
    except OSError as e:
        return 1, f"Claude Code couldn't start ({type(e).__name__})."
    return r.returncode, "\n".join(part.strip() for part in (r.stdout, r.stderr) if part.strip())


# Where claude.exe is: UNI_COPILOT_CLAUDE if set, else the real exe inside the
# npm install next to `claude.cmd` on PATH, else whatever `claude` is on PATH.
def find_claude() -> str | None:
    if os.environ.get("UNI_COPILOT_CLAUDE"):
        return os.environ["UNI_COPILOT_CLAUDE"]
    launcher = shutil.which("claude.cmd") or shutil.which("claude")
    if not launcher:
        return None
    exe = Path(launcher).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return str(exe) if exe.exists() else launcher


# Every stored deadline (removed ones too), soonest first.
def _deadlines(conn: sqlite3.Connection) -> list[Event]:
    rows = conn.execute("SELECT * FROM events WHERE kind = 'deadline' ORDER BY due_at")
    return [row_to_event(r) for r in rows]


# The events `what` points at: an exact event id or NTULearn id if one fits,
# else every event whose course code + title contains all the words (any case).
def _match(events: list[Event], what: str) -> list[Event]:
    exact = [e for e in events if what in (e.event_id, e.native_id)]
    if exact:
        return exact
    words = what.lower().split()
    return [e for e in events if words and all(w in f"{e.course_code} {e.title}".lower() for w in words)]


# One diary line as text, e.g. "moved  DA3000  DA3 Report: Fri 07 Nov 23:59 SGT -> Sun 09 Nov 23:59 SGT".
def describe_change(item: EventChange) -> str:
    c, e = item.change, item.event
    text = f"{c.change_type:<15} {e.course_code or '':<7} {e.title}"
    if c.change_type == "moved":
        before, after = time_from_db(c.before), time_from_db(c.after)
        text += f": {sgt_text(before) if before else 'no date'} -> {sgt_text(after) if after else 'no date'}"
    elif c.before or c.after:
        text += f": {c.before} -> {c.after}"
    return text


# "As of Sun 25 Oct 11:52 SGT (5 min ago)", plus a warning for any failed source.
def as_of_line(conn: sqlite3.Connection, now: datetime) -> str:
    f = freshness(conn)
    if f.as_of is None:
        return "As of: never fully synced. Run: uv run uni-copilot sync"
    line = f"As of {sgt_text(f.as_of)} ({ago(f.as_of, now)})"
    if f.errors:
        line += f"; last try failed for: {', '.join(SOURCE_LABELS.get(s, s) for s in f.errors)}"
    return line


# "12 min ago", "3 h ago", "2 days ago": how long since `then`.
def ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


if __name__ == "__main__":
    sys.exit(main())
