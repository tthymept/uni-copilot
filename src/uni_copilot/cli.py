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

  Phase 3 adds `remind` and `done` later.

How a command reaches the code (same idea as the MCP tools in Phase 1):
  pyproject.toml  [project.scripts] uni-copilot = "uni_copilot.cli:main"
    -> uv made a small uni-copilot.exe in .venv/Scripts that calls main()
    -> main() reads the words after `uni-copilot` with argparse
    -> "sync" runs cmd_sync(), which calls store.sync.run_sync(...)

Exit code (what Task Scheduler sees in step 12): 0 = every source worked,
1 = at least one source failed (its old data is kept, nothing marked removed).

--log FILE (step 12): everything the command prints goes to the end of FILE
instead of the screen, starting with a time stamp line. The scheduled task
runs with pythonw.exe (no window), which has no screen to print to.
"""

import argparse
import logging
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uni_copilot.ntulearn_mcp.models import sgt_text
from uni_copilot.notify.telegram import (
    ENV_FILE,
    TelegramBot,
    TelegramError,
    TelegramNotConfigured,
    load_telegram_settings,
)
from uni_copilot.store.db import DB_PATH, get_state, open_db, schema_version, time_from_db
from uni_copilot.store.queries import EventChange, changes_since, first_sync_times, freshness, is_first_sync, list_events
from uni_copilot.store.sync import run_sync

LOG_MAX_BYTES = 1_000_000  # ~1 MB, about two years of sync logs at 9 runs a day

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
    run ={"sync": cmd_sync, "status": cmd_status}[args.command]
    return run(args.db)


# Opens the log file for adding to its end. If it's grown past 1 MB, the old
# one is kept as <name>.old (replacing any older one) and a new file starts.
def open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".old"))
    return open(path, "a", encoding="utf-8")


# `uni-copilot sync`: one full sync into the store, one line per source.
def cmd_sync(db: Path) -> int:
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
    conn.close()

    deadlines = [e for e in items if e.kind == "deadline"]
    posts = [e for e in items if e.kind != "deadline"]
    if not kinds or "deadline" in kinds:
        print(f"Deadlines due in the next {days} day(s): {len(deadlines)}")
        for e in deadlines:
            print(f"  {sgt_text(e.due_at)}  {e.course_code:<7} {e.status:<9}  {e.title}")
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
