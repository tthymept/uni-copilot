"""
The SQLite file: opening it, creating the tables, and converting rows.

In plain words:
  - A database is a file with tables in it. A table is like a spreadsheet:
    columns are the fields (title, due_at, ...), each row is one thing.
  - We talk to it in SQL, a small language for "create this table",
    "insert this row", "give me the rows where ...". Python's built-in
    `sqlite3` module sends the SQL to the file. No server, nothing to install.
  - This file only knows HOW things are stored. WHAT to store (new? edited?
    moved?) is decided in events.py (step 3).

The three tables (see "How it all connects" in the Phase 2 build log):
  events         one row per event, as it looks now (primary key: event_id)
  event_changes  the diary: one row per change found, only ever added to
  state          small key -> value notes (last sync time, last-seen bookmarks)

Schema versions: the file remembers which version of the tables it has, in
SQLite's built-in `PRAGMA user_version` (a number, 0 for a new file).
MIGRATIONS is a list of steps; open_db() runs every step the file hasn't had
yet. Later phases add a step to the END of the list (never edit an old one),
so an existing database upgrades without losing data.

Times are stored as text in one fixed format, UTC with microseconds:
"2026-11-07T15:59:00.000000+00:00". Same length every time, so sorting the
text sorts by time, and reading it back gives exactly the same datetime.
"""

import argparse
import sqlite3
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uni_copilot.ntulearn_mcp.config import DATA_DIR
from uni_copilot.store.models import Change, Event

DB_PATH = DATA_DIR / "uni-copilot.sqlite"

# Each item upgrades the tables by one version: MIGRATIONS[0] takes a new file
# from version 0 to 1, MIGRATIONS[1] (a later phase) from 1 to 2, and so on.
MIGRATIONS: list[str] = [
    # Version 1 (Phase 2): events, event_changes, state.
    """
    CREATE TABLE events (
        event_id       TEXT PRIMARY KEY,          -- "<source>:<native id>", the dedupe key
        source         TEXT NOT NULL,
        kind           TEXT NOT NULL,
        course_code    TEXT,
        course_site_id TEXT,
        title          TEXT NOT NULL,             -- UNTRUSTED
        body           TEXT NOT NULL DEFAULT '',  -- UNTRUSTED, full light Markdown
        url            TEXT,
        starts_at      TEXT,                      -- times: UTC text, see the top of db.py
        due_at         TEXT,
        posted_at      TEXT,
        status         TEXT,                      -- open / submitted / graded / done, never a grade
        content_hash   TEXT NOT NULL DEFAULT '',
        found_in       TEXT NOT NULL DEFAULT '',  -- "calendar,gradebook"
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        modified_at    TEXT NOT NULL,
        removed_at     TEXT                       -- NULL while it still exists
    );
    CREATE INDEX idx_events_due ON events (due_at);
    CREATE INDEX idx_events_posted ON events (posted_at);
    CREATE INDEX idx_events_course ON events (course_code);
    CREATE INDEX idx_events_source ON events (source);

    CREATE TABLE event_changes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 1, 2, 3, ... given by SQLite
        event_id    TEXT NOT NULL REFERENCES events (event_id),
        change_type TEXT NOT NULL,   -- new / edited / moved / status_changed / removed
        changed_at  TEXT NOT NULL,   -- time of the sync that found it
        before      TEXT,
        after       TEXT
    );
    CREATE INDEX idx_changes_time ON event_changes (changed_at);
    CREATE INDEX idx_changes_event ON event_changes (event_id);

    CREATE TABLE state (
        key   TEXT PRIMARY KEY,      -- e.g. "last_sync:ntulearn_gradebook", "last_seen:terminal"
        value TEXT NOT NULL
    );
    """,
]

# Event fields in table-column order. The dataclass and the table use the same
# names, so one list drives both directions of the conversion below.
EVENT_COLUMNS = [f.name for f in fields(Event)]


# Opens (or creates) the database file, sets it up, and brings the tables up
# to the latest version. Pass ":memory:" for a throwaway database in memory
# (used by tests and the self-test).
def open_db(path: Path | str = DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # rows can be read by column name: row["title"]
    conn.execute("PRAGMA foreign_keys = ON")  # enforce event_changes.event_id -> events
    conn.execute("PRAGMA busy_timeout = 5000")  # wait up to 5 s if another program is writing
    conn.execute("PRAGMA journal_mode = WAL")  # readers (pilot-mcp) don't block the writer (sync)
    migrate(conn)
    return conn


# Runs every migration the file hasn't had yet. Each one runs inside a
# transaction together with the new version number: if anything fails, the
# file stays exactly at its old version.
def migrate(conn: sqlite3.Connection) -> int:
    version = schema_version(conn)
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


# The version number stored in the file (0 for a brand-new file).
def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


# Names of the tables in the file, sorted (SQLite's own bookkeeping tables left out).
def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r["name"] for r in rows]


# ---------- times: datetime <-> text ----------

# A UTC datetime -> fixed-format text for the database (None stays None).
def time_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


# Text from the database -> a UTC datetime (None stays None).
def time_from_db(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


# ---------- rows: Event / Change <-> table rows ----------

# An Event -> a dict of column values, ready for an INSERT or UPDATE.
def event_to_row(event: Event) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for name in EVENT_COLUMNS:
        value = getattr(event, name)
        if name.endswith("_at"):
            value = time_to_db(value)
        elif name == "found_in":
            value = ",".join(value)
        row[name] = value
    return row


# A row from the events table -> an Event (the checks in Event run again).
def row_to_event(row: sqlite3.Row) -> Event:
    values: dict[str, Any] = {}
    for name in EVENT_COLUMNS:
        value = row[name]
        if name.endswith("_at"):
            value = time_from_db(value)
        elif name == "found_in":
            value = tuple(value.split(",")) if value else ()
        values[name] = value
    return Event(**values)


# A Change -> a dict of column values (id left out: SQLite gives it).
def change_to_row(change: Change) -> dict[str, Any]:
    return {
        "event_id": change.event_id,
        "change_type": change.change_type,
        "changed_at": time_to_db(change.changed_at),
        "before": change.before,
        "after": change.after,
    }


# A row from the event_changes table -> a Change.
def row_to_change(row: sqlite3.Row) -> Change:
    return Change(
        event_id=row["event_id"],
        change_type=row["change_type"],
        changed_at=time_from_db(row["changed_at"]),
        before=row["before"],
        after=row["after"],
        id=row["id"],
    )


# ---------- state: small key -> value notes ----------

# Reads one note from the state table, or None if it was never set.
def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# Writes one note to the state table, replacing the old value if there was one.
# The caller decides when to commit (so it can be part of a bigger transaction).
def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# Self-test: uv run python -m uni_copilot.store.db          (in memory, nothing saved)
#            uv run python -m uni_copilot.store.db --real   (creates/opens .data/uni-copilot.sqlite)
def _main() -> None:
    parser = argparse.ArgumentParser(description="Create the event store and show its tables.")
    parser.add_argument("--real", action="store_true", help=f"use the real file ({DB_PATH})")
    args = parser.parse_args()

    if args.real:
        conn = open_db()
        print(f"Database file: {DB_PATH}")
        print(f"Schema version: {schema_version(conn)}")
        for name in table_names(conn):
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            print(f"   {name}: {count} rows")
        conn.close()
        return

    conn = open_db(":memory:")
    print(f"1) New database in memory, schema version {schema_version(conn)}. Tables and columns:")
    for name in table_names(conn):
        columns = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
        print(f"   {name}: {', '.join(columns)}")

    print(f"\n2) Running the migrations again does nothing: version {migrate(conn)}")

    now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
    event = Event(
        event_id="ntulearn_gradebook:_999_1",
        source="ntulearn_gradebook",
        kind="deadline",
        course_code="XX1000",
        course_site_id="_123_1",
        title="Made-up Assignment 1",
        due_at=datetime(2026, 11, 7, 15, 59, 0, 123000, tzinfo=timezone.utc),
        status="open",
        content_hash="abc123",
        found_in=("calendar", "gradebook"),
        first_seen_at=now,
        last_seen_at=now,
        modified_at=now,
    )
    placeholders = ", ".join(f":{c}" for c in EVENT_COLUMNS)
    conn.execute(f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({placeholders})", event_to_row(event))
    conn.execute(
        "INSERT INTO event_changes (event_id, change_type, changed_at, before, after) "
        "VALUES (:event_id, :change_type, :changed_at, :before, :after)",
        change_to_row(Change(event.event_id, "new", now)),
    )
    set_state(conn, "last_seen:terminal", time_to_db(now))
    conn.commit()

    row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
    print("\n3) One event saved and read back:")
    print(f"   due_at stored as text: {row['due_at']}")
    print(f"   found_in stored as text: {row['found_in']!r}")
    print(f"   read back equals the original: {row_to_event(row) == event}")

    change = row_to_change(conn.execute("SELECT * FROM event_changes").fetchone())
    print(f"\n4) Diary row: id={change.id} {change.change_type} {change.event_id}")
    print(f"   state last_seen:terminal = {get_state(conn, 'last_seen:terminal')}")

    print("\n5) The database refuses a diary line for an event that doesn't exist:")
    try:
        conn.execute(
            "INSERT INTO event_changes (event_id, change_type, changed_at) VALUES (?, ?, ?)",
            ("ntulearn_gradebook:_nope_1", "new", time_to_db(now)),
        )
        print("   NOT refused")
    except sqlite3.IntegrityError as e:
        print(f"   refused: {e}")
    conn.close()


if __name__ == "__main__":
    _main()
