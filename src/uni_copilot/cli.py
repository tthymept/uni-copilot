"""
The `uni-copilot` command: plain scripts, no AI.

In plain words:
  uni-copilot sync     fetch NTULearn into the local store (about 1 minute),
                       print what changed per source
  uni-copilot status   how fresh the store is: last sync per source ("as of"),
                       errors, and how many events and diary lines it holds

  Later steps add: `events` and `changes` (step 7), and Phase 3 adds
  `remind` and `notify`.

How a command reaches the code (same idea as the MCP tools in Phase 1):
  pyproject.toml  [project.scripts] uni-copilot = "uni_copilot.cli:main"
    -> uv made a small uni-copilot.exe in .venv/Scripts that calls main()
    -> main() reads the words after `uni-copilot` with argparse
    -> "sync" runs cmd_sync(), which calls store.sync.run_sync(...)

Exit code (what Task Scheduler sees in step 12): 0 = every source worked,
1 = at least one source failed (its old data is kept, nothing marked removed).
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from uni_copilot.ntulearn_mcp.models import sgt_text
from uni_copilot.store.db import DB_PATH, get_state, open_db, schema_version, time_from_db
from uni_copilot.store.sync import run_sync

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
    args = parser.parse_args(argv)

    # Phase 1 clients log every step at INFO; for a command line only warnings matter.
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
    run = {"sync": cmd_sync, "status": cmd_status}[args.command]
    return run(args.db)


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
