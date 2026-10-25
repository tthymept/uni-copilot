"""
Saving events and writing the diary: the heart of Phase 2.

In plain words:
  Every sync hands us a list of events: "this is what NTULearn shows now".
  For each one we look up what we stored last time and compare:
    not stored before          -> "new"
    due/start time different   -> "moved"            (before/after = the times)
    status different           -> "status_changed"   (before/after = the words)
    content_hash different     -> "edited"           (title or body changed)
    all the same               -> nothing written to the diary
  Then the stored copy is replaced by the new one (an "upsert": insert, or
  update if it already exists), keeping when we first saw it.

  Stored events that are missing from the list may be "removed", but only
  if the caller says this sync was supposed to include them (`covers`). That
  rule lives in sync.py (step 5): a course whose fetch failed, or an item that
  only left the time window, is NOT covered, so it's never marked removed.

  All of it happens in one transaction: if anything goes wrong half-way,
  nothing from this sync is saved and the store stays as it was.

Why two syncs with nothing new write nothing: every comparison above is
"different or not". Same data in -> no differences -> no diary rows. That's
the Phase 2 "done" test (idempotent sync).
"""

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from uni_copilot.store.db import (
    EVENT_COLUMNS,
    change_to_row,
    event_to_row,
    open_db,
    row_to_change,
    row_to_event,
    time_to_db,
)
from uni_copilot.store.models import Change, Event

# Upsert in SQL: insert the row, or if that event_id already exists, overwrite
# every column with the new values ("excluded" = the row we tried to insert).
_UPSERT_EVENT = (
    f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in EVENT_COLUMNS)}) "
    f"ON CONFLICT (event_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in EVENT_COLUMNS if c != "event_id")
)

_INSERT_CHANGE = (
    "INSERT INTO event_changes (event_id, change_type, changed_at, before, after) "
    "VALUES (:event_id, :change_type, :changed_at, :before, :after)"
)


# What one save did: the diary lines it wrote, and how many events it was given.
@dataclass
class SaveResult:
    changes: list[Change] = field(default_factory=list)
    seen: int = 0

    # How many of each change type, e.g. {"new": 3, "moved": 1}.
    def counts(self) -> dict[str, int]:
        return dict(Counter(c.change_type for c in self.changes))

    # Events given that changed in no way (seen minus those with a diary line).
    @property
    def unchanged(self) -> int:
        changed_ids = {c.event_id for c in self.changes if c.change_type != "removed"}
        return self.seen - len(changed_ids)


# Every stored event (removed ones too), by event_id. The table is small
# (hundreds of rows), so reading it all at once is simplest and fast.
def load_events(conn: sqlite3.Connection) -> dict[str, Event]:
    return {row["event_id"]: row_to_event(row) for row in conn.execute("SELECT * FROM events")}


# Compares the stored copy (None if never seen) with the new one and returns
# the diary lines. Pure function: no database, easy to test.
def diff(old: Event | None, new: Event, now: datetime) -> list[Change]:
    if old is None:
        return [Change(new.event_id, "new", now)]
    if not old.is_active:  # it had been removed and now it's back
        return [Change(new.event_id, "new", now, before="removed")]

    changes: list[Change] = []
    if old.due_at != new.due_at:
        changes.append(Change(new.event_id, "moved", now, time_to_db(old.due_at), time_to_db(new.due_at)))
    if old.starts_at != new.starts_at:
        changes.append(Change(new.event_id, "moved", now, time_to_db(old.starts_at), time_to_db(new.starts_at)))
    if old.status != new.status:
        changes.append(Change(new.event_id, "status_changed", now, old.status, new.status))
    if old.content_hash != new.content_hash:
        # Show the titles only if the title itself changed; a body edit has no short "before/after".
        titles = (old.title, new.title) if old.title != new.title else (None, None)
        changes.append(Change(new.event_id, "edited", now, *titles))
    return changes


# Builds the copy to store: the new source fields plus our bookkeeping.
# first_seen_at is kept from the old copy; modified_at moves only on a change.
def merge_bookkeeping(old: Event | None, new: Event, now: datetime, changed: bool) -> Event:
    return replace(
        new,
        first_seen_at=old.first_seen_at if old else now,
        last_seen_at=now,
        modified_at=now if changed or old is None else old.modified_at,
        removed_at=None,
    )


# Saves one sync's events and writes the diary, in one transaction.
#   fetched: what the source shows now (event_ids must be unique).
#   now:     the time of this sync (UTC); every row written gets this time.
#   covers:  for a stored, not-yet-removed event that's missing from `fetched`,
#            says whether this sync was supposed to include it. True -> it's
#            marked removed. None (the default) -> nothing is ever removed.
def save_events(
    conn: sqlite3.Connection,
    fetched: Iterable[Event],
    now: datetime,
    covers: Callable[[Event], bool] | None = None,
) -> SaveResult:
    fetched = list(fetched)
    ids = [e.event_id for e in fetched]
    if len(ids) != len(set(ids)):
        dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
        raise ValueError(f"the same event_id was given twice: {dupes}")

    result = SaveResult(seen=len(fetched))
    with conn:  # one transaction: COMMIT at the end, ROLLBACK if anything raises
        stored = load_events(conn)

        for new in fetched:
            old = stored.get(new.event_id)
            changes = diff(old, new, now)
            conn.execute(_UPSERT_EVENT, event_to_row(merge_bookkeeping(old, new, now, bool(changes))))
            for change in changes:
                conn.execute(_INSERT_CHANGE, change_to_row(change))
            result.changes.extend(changes)

        if covers is not None:
            fetched_ids = set(ids)
            for old in stored.values():
                if old.is_active and old.event_id not in fetched_ids and covers(old):
                    conn.execute(_UPSERT_EVENT, event_to_row(replace(old, removed_at=now, modified_at=now)))
                    change = Change(old.event_id, "removed", now)
                    conn.execute(_INSERT_CHANGE, change_to_row(change))
                    result.changes.append(change)
    return result


# The whole diary, oldest first (for the self-test and quick checks).
def all_changes(conn: sqlite3.Connection) -> list[Change]:
    return [row_to_change(r) for r in conn.execute("SELECT * FROM event_changes ORDER BY id")]


# Self-test: uv run python -m uni_copilot.store.events
# Plays five made-up syncs against a database in memory. No network, nothing saved.
def _main() -> None:
    conn = open_db(":memory:")
    t0 = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)  # 12:00 SGT

    # Short helpers that build made-up events.
    def deadline(n: int, due_day: int, status: str = "open", course: str = "XX1000") -> Event:
        return Event(
            event_id=f"ntulearn_gradebook:_{n}_1", source="ntulearn_gradebook", kind="deadline",
            course_code=course, course_site_id="_1_1", title=f"Made-up Assignment {n}",
            due_at=datetime(2026, 11, due_day, 15, 59, tzinfo=timezone.utc), status=status,
            content_hash=f"title{n}", found_in=("calendar", "gradebook"),
        )

    def announcement(n: int, body: str) -> Event:
        return Event(
            event_id=f"ntulearn_announcement:_{n}_1", source="ntulearn_announcement", kind="announcement",
            course_code="XX1000", course_site_id="_1_1", title=f"Made-up news {n}", body=body,
            posted_at=t0 - timedelta(days=1), content_hash=f"hash-of:{body}",
        )

    # Each sync says what it covers, like sync.py will: only its own source.
    def covers_source(source: str) -> Callable[[Event], bool]:
        return lambda e: e.source == source

    def show(label: str, result: SaveResult) -> None:
        print(f"{label}: seen {result.seen}, unchanged {result.unchanged}, changes {result.counts() or 'none'}")

    a1 = announcement(7, "Quiz in LT1.")
    show("1) First sync, 3 things", save_events(conn, [deadline(1, 7), deadline(2, 14), a1], t0))
    show(
        "2) Same again, 2 h later",
        save_events(conn, [deadline(1, 7), deadline(2, 14), a1], t0 + timedelta(hours=2)),
    )
    show(
        "3) DA1 moved, DA2 submitted, news edited",
        save_events(
            conn,
            [deadline(1, 9), deadline(2, 14, "submitted"), announcement(7, "Quiz moved to LT2.")],
            t0 + timedelta(hours=4),
        ),
    )
    show(
        "4) News gone (announcements covered)",
        save_events(conn, [], t0 + timedelta(hours=6), covers_source("ntulearn_announcement")),
    )
    show(
        "5) Deadlines missing, but the fetch failed (not covered)",
        save_events(conn, [], t0 + timedelta(hours=8), covers=lambda e: False),
    )

    print("\n6) A crash half-way saves nothing (transaction rolled back):")

    def broken(_: Event) -> bool:
        raise RuntimeError("made-up crash")

    before = len(all_changes(conn))
    try:
        save_events(conn, [deadline(3, 21)], t0 + timedelta(hours=10), covers=broken)
    except RuntimeError as e:
        print(f"   raised: {e}")
    print(f"   diary rows before {before}, after {len(all_changes(conn))}; "
          f"Assignment 3 stored: {'ntulearn_gradebook:_3_1' in load_events(conn)}")

    print("\n7) The diary:")
    for c in all_changes(conn):
        extra = f"  {c.before} -> {c.after}" if c.before or c.after else ""
        print(f"   #{c.id} {c.changed_at:%H:%M} UTC  {c.change_type:<15} {c.event_id}{extra}")

    da1 = load_events(conn)["ntulearn_gradebook:_1_1"]
    print(f"\n8) Assignment 1 bookkeeping: first seen {da1.first_seen_at:%H:%M}, "
          f"last seen {da1.last_seen_at:%H:%M}, modified {da1.modified_at:%H:%M} UTC")
    conn.close()


if __name__ == "__main__":
    _main()
