"""
The read side of the store: what pilot-mcp (step 8) and the CLI ask it.

In plain words:
  list_events     "what's due in the next N days, and what was posted in
                  the last N days?" (from `events`)
  changes_since   "what's in the diary after time X?", each line together
                  with its event's title and course (from `event_changes`
                  joined with `events`)
  whats_new       changes_since(your bookmark) for one channel, without the
                  first-sync import (see below)
  freshness       the "as of" time: when each source last synced, and any
                  errors (from `state`)
  last_seen /     the bookmark per channel ("terminal", "telegram"): when
  mark_seen       you last looked. mark_seen is the only write in here, and
                  it only moves our own bookmark.

The first-sync import isn't news. The very first sync of a source writes a
"new" line for everything it finds (80 on 25 Oct). Those mean "we started
watching", not "the prof posted 80 things", so whats_new leaves out every
line written at a source's first sync. They stay in the diary.

Nothing here talks to NTULearn: reading the local file only, so it's instant.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from uni_copilot.store.db import get_state, row_to_event, set_state, time_from_db, time_to_db
from uni_copilot.store.models import Change, Event, Kind, Source

Channel = Literal["terminal", "telegram"]

SOURCES: tuple[Source, ...] = ("ntulearn_gradebook", "ntulearn_announcement", "ntulearn_message")

# A channel that has never looked yet sees the changes of the last 7 days.
FIRST_LOOK_DAYS = 7


# One diary line together with the event it's about (title, course, ...).
@dataclass(frozen=True)
class EventChange:
    change: Change
    event: Event


# How fresh the store is. as_of = the OLDEST last sync of the sources (so it's
# never claimed fresher than it is); None if a source never synced.
@dataclass
class Freshness:
    last_sync: dict[str, datetime | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # only sources whose last try failed

    # The time every source is at least as fresh as.
    @property
    def as_of(self) -> datetime | None:
        times = list(self.last_sync.values())
        return None if not times or None in times else min(times)


# What whats_new returns: the changes, the bookmark they were compared with,
# and how fresh the data is.
@dataclass
class WhatsNew:
    channel: str
    since: datetime
    changes: list[EventChange]
    freshness: Freshness


# Events that matter in a window of `days` around now, soonest/newest first:
#   deadlines due from now to now + days, and
#   announcements/messages posted from now - days to now.
# Removed events are left out. `kinds` limits the kinds; `course` matches
# part of the course code, any case ("da3" finds DA3000).
def list_events(
    conn: sqlite3.Connection, days: int = 7, kinds: list[Kind] | None = None,
    course: str | None = None, now: datetime | None = None,
) -> list[Event]:
    now = now or datetime.now(timezone.utc)
    kinds = kinds or ["deadline", "announcement", "message"]
    rows = conn.execute(
        f"""
        SELECT * FROM events
        WHERE removed_at IS NULL
          AND kind IN ({", ".join("?" for _ in kinds)})
          AND (? IS NULL OR course_code LIKE '%' || ? || '%')
          AND (   (kind = 'deadline' AND due_at > ? AND due_at <= ?)
               OR (kind != 'deadline' AND posted_at >= ?))
        ORDER BY kind = 'deadline' DESC, due_at ASC, posted_at DESC
        """,
        [*kinds, course, course, time_to_db(now), time_to_db(now + timedelta(days=days)),
         time_to_db(now - timedelta(days=days))],
    )
    return [row_to_event(r) for r in rows]


# One event by its id (removed ones too), or None.
def get_event(conn: sqlite3.Connection, event_id: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    return row_to_event(row) if row else None


# Every diary line written after `since`, oldest first, each with its event.
# JOIN = put each diary row next to the events row with the same event_id.
def changes_since(conn: sqlite3.Connection, since: datetime) -> list[EventChange]:
    rows = conn.execute(
        """
        SELECT c.id AS c_id, c.change_type, c.changed_at, c.before, c.after, e.*
        FROM event_changes AS c
        JOIN events AS e ON e.event_id = c.event_id
        WHERE c.changed_at > ?
        ORDER BY c.id
        """,
        (time_to_db(since),),
    )
    out = []
    for r in rows:
        change = Change(r["event_id"], r["change_type"], time_from_db(r["changed_at"]), r["before"], r["after"], r["c_id"])
        out.append(EventChange(change, row_to_event(r)))
    return out


# When each source's first sync happened = the earliest diary line of that
# source. Lines written at exactly that time are the first-sync import.
def first_sync_times(conn: sqlite3.Connection) -> dict[str, datetime]:
    rows = conn.execute(
        """
        SELECT e.source, MIN(c.changed_at) AS first
        FROM event_changes AS c JOIN events AS e ON e.event_id = c.event_id
        GROUP BY e.source
        """
    )
    return {r["source"]: time_from_db(r["first"]) for r in rows}


# True if a diary line is part of its source's first-sync import.
def is_first_sync(item: EventChange, first: dict[str, datetime]) -> bool:
    return item.change.changed_at == first.get(item.event.source)


# Last sync time and last error per source, from the state notes.
def freshness(conn: sqlite3.Connection) -> Freshness:
    f = Freshness()
    for source in SOURCES:
        f.last_sync[source] = time_from_db(get_state(conn, f"last_sync:{source}"))
        error = get_state(conn, f"last_error:{source}")
        if error:
            f.errors[source] = error
    return f


# The channel's bookmark: when it last looked (None if never).
def last_seen(conn: sqlite3.Connection, channel: Channel) -> datetime | None:
    return time_from_db(get_state(conn, f"last_seen:{channel}"))


# Moves the channel's bookmark to `when` (default now). The only write here.
def mark_seen(conn: sqlite3.Connection, channel: Channel, when: datetime | None = None) -> datetime:
    when = when or datetime.now(timezone.utc)
    with conn:
        set_state(conn, f"last_seen:{channel}", time_to_db(when))
    return when


# What changed since the channel last looked, without the first-sync import.
# Only reads: the bookmark stays where it is until mark_seen is called.
def whats_new(conn: sqlite3.Connection, channel: Channel, now: datetime | None = None) -> WhatsNew:
    now = now or datetime.now(timezone.utc)
    since = last_seen(conn, channel) or now - timedelta(days=FIRST_LOOK_DAYS)
    first = first_sync_times(conn)
    changes = [c for c in changes_since(conn, since) if not is_first_sync(c, first)]
    return WhatsNew(channel, since, changes, freshness(conn))


# Self-test: uv run python -m uni_copilot.store.queries
# Made-up events in memory: two syncs, then the questions pilot-mcp will ask.
def _main() -> None:
    from uni_copilot.store.db import open_db
    from uni_copilot.store.events import save_events

    conn = open_db(":memory:")
    t1 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)  # 10:00 SGT: first sync
    t2 = t1 + timedelta(hours=2)  # 12:00 SGT: second sync

    def deadline(n: int, days_ahead: int, status: str = "open") -> Event:
        return Event(f"ntulearn_gradebook:_{n}_1", "ntulearn_gradebook", "deadline", "DA3000", "_1_1",
                     f"Made-up Assignment {n}", due_at=t1 + timedelta(days=days_ahead), status=status,
                     content_hash=f"h{n}")

    def post(n: int, hours_ago: int) -> Event:
        return Event(f"ntulearn_announcement:_{n}_1", "ntulearn_announcement", "announcement", "IE2108", "_2_1",
                     f"Made-up news {n}", body="Full text.", posted_at=t1 - timedelta(hours=hours_ago),
                     content_hash=f"p{n}")

    save_events(conn, [deadline(1, 3), deadline(2, 20), post(7, 30)], t1)
    for s in SOURCES:
        set_state(conn, f"last_sync:{s}", time_to_db(t1))
    conn.commit()
    mark_seen(conn, "terminal", t1)
    # 11:00 a new announcement is posted; the 12:00 sync finds it, and DA1 got submitted.
    save_events(conn, [deadline(1, 3, "submitted"), deadline(2, 20), post(7, 30), post(8, -1)], t2)
    for s in SOURCES:
        set_state(conn, f"last_sync:{s}", time_to_db(t2))
    conn.commit()

    print("1) list_events(days=7) at 12:00:")
    for e in list_events(conn, days=7, now=t2):
        when = e.due_at if e.kind == "deadline" else e.posted_at
        print(f"   {e.kind:<12} {e.course_code}  {when:%d %b %H:%M} UTC  {e.status or '':<9} {e.title}")
    print(f"   course='da3' -> {len(list_events(conn, days=7, course='da3', now=t2))} event(s); "
          f"days=30 -> {len(list_events(conn, days=30, now=t2))}")

    new = whats_new(conn, "terminal", now=t2 + timedelta(hours=3))
    print(f"\n2) whats_new('terminal') at 15:00, bookmark {new.since:%H:%M} UTC, as of {new.freshness.as_of:%H:%M} UTC:")
    for c in new.changes:
        print(f"   {c.change.change_type:<15} {c.event.title}  {c.change.before or ''} {'->' if c.change.after else ''} "
              f"{c.change.after or ''}")
    tg = whats_new(conn, "telegram", now=t2 + timedelta(hours=3))
    print(f"   telegram (never looked): {len(tg.changes)} change(s); the 3 first-sync 'new' lines are left out")

    mark_seen(conn, "terminal", t2 + timedelta(hours=3))
    print(f"\n3) after mark_seen('terminal'): {len(whats_new(conn, 'terminal', now=t2 + timedelta(hours=3)).changes)} change(s)")
    print(f"4) get_event: {get_event(conn, 'ntulearn_announcement:_8_1').body!r}")
    conn.close()


if __name__ == "__main__":
    _main()
