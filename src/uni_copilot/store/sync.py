"""
One sync: fetch every source, save to the store, remember how it went.

In plain words:
  For each source (deadlines, announcements, messages):
    1. fetch it with the Phase 1 client (GET only, ~1 request/s)
    2. turn the results into Events (sources/ntulearn.py)
    3. save them and write the diary (store/events.py, one transaction)
    4. note the time and any problems in the `state` table
  One source failing doesn't stop the others.

  The two safety rules about "removed" live here, in `covers`:
    - A course whose fetch failed this time is NOT covered: its items are
      kept as they were, never marked removed. If the whole source failed
      (e.g. the calendar or the sign-in), nothing of it is removed.
    - An item that only LEFT THE TIME WINDOW (an announcement now 31 days
      old) is NOT covered: it wasn't fetched because we didn't ask for it,
      not because it was deleted.
  Plus one rule about status: if NTULearn couldn't tell us a deadline's
  status this time ("unknown"), the status we already knew is kept, so a
  hiccup never shows up as "submitted -> open".

What sync covers (Phase 2 decision):
  deadlines            due from 14 days ago to 120 days ahead
  announcements        posted in the last 30 days
  messages             posted in the last 30 days

State notes written per source (read by pilot-mcp's sync_status and "as of"):
  last_attempt:<source>  when this source was last tried
  last_sync:<source>     when it last worked (the "as of" time)
  last_error:<source>    "" if it worked, else a short error (no secrets)
  last_notes:<source>    courses that couldn't be read, one per line
"""

import argparse
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from uni_copilot.ntulearn_mcp.client.announcements import AnnouncementsClient
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.client.deadlines import DeadlinesClient
from uni_copilot.ntulearn_mcp.client.http import NtuLearnHttp
from uni_copilot.ntulearn_mcp.client.messages import MessagesClient
from uni_copilot.ntulearn_mcp.config import load_settings
from uni_copilot.sources.ntulearn import announcements_to_events, deadlines_to_events, messages_to_events
from uni_copilot.store.db import open_db, set_state, time_to_db
from uni_copilot.store.events import load_events, save_events
from uni_copilot.store.models import Event

log = logging.getLogger(__name__)

DEADLINE_DAYS_BACK = 14
DEADLINE_DAYS_AHEAD = 120
POST_DAYS = 30

# The clients work out "now" a moment after we do, so their window edges are a
# few seconds off ours. Items within this margin of an edge are never treated
# as covered, so that gap can't make anything look removed.
EDGE_MARGIN = timedelta(hours=1)

# Words that mark a note as "something failed" (the notes come from Phase 1's
# clients, e.g. "IE2108 gradebook unavailable on site _1_1 (403)").
# Other notes are just information ("3 item(s) from sites outside ... left out").
FAILURE_WORDS = ("unavailable", "couldn't be read")

ERROR_MAX_CHARS = 300


# How one source went in one sync. Printed by `uni-copilot sync` (step 6).
@dataclass
class SourceReport:
    source: str
    ok: bool = False
    seen: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    removals_checked: bool = False  # False when a failure meant nothing could be marked removed
    seconds: float = 0.0


# The Phase 1 clients sync needs, sharing one sign-in and one 1 request/s pacer.
@dataclass
class Clients:
    http: NtuLearnHttp
    courses: CoursesClient
    deadlines: DeadlinesClient
    announcements: AnnouncementsClient
    messages: MessagesClient


# Builds the clients the same way server.py does.
def make_clients() -> Clients:
    http = NtuLearnHttp()
    courses = CoursesClient(http, load_settings())
    return Clients(
        http, courses, DeadlinesClient(http, courses), AnnouncementsClient(http, courses), MessagesClient(http, courses)
    )


# ---------- Pure functions (no network, no database) ----------

# True if a Phase 1 note says something couldn't be read.
def is_failure(note: str) -> bool:
    return any(word in note for word in FAILURE_WORDS)


# Which courses failed, from a source's notes. Returns None if a failure
# isn't about one known course (e.g. "Calendar unavailable"): then the whole
# source counts as failed and nothing of it may be marked removed.
def failed_courses(notes: list[str], codes: set[str]) -> set[str] | None:
    failed: set[str] = set()
    for note in filter(is_failure, notes):
        # Longest code first, so a note about "IE2108X" isn't put on "IE2108".
        code = next((c for c in sorted(codes, key=len, reverse=True) if note.startswith(c)), None)
        if code is None:
            return None
        failed.add(code)
    return failed


# Builds the `covers` rule for save_events: a stored event counts as "should
# have been fetched" only if it's this source's, from a current course that
# didn't fail, and its time is inside the window (with the edge margin).
# Returns None (= remove nothing) if the whole source failed.
def make_covers(
    source: str, time_field: str, since: datetime, until: datetime,
    codes: set[str], failed: set[str] | None,
) -> Callable[[Event], bool] | None:
    if failed is None:
        return None
    lo, hi = since + EDGE_MARGIN, until - EDGE_MARGIN

    # The rule itself, called by save_events for each missing stored event.
    def covers(e: Event) -> bool:
        t = getattr(e, time_field)
        return (
            e.source == source
            and e.course_code in codes
            and e.course_code not in failed
            and t is not None
            and lo <= t <= hi
        )

    return covers


# For deadlines whose status came back "unknown" this time, keeps the status
# already stored (if any), so a failed status fetch isn't a status change.
def keep_known_status(events: list[Event], unknown_ids: set[str], stored: dict[str, Event]) -> list[Event]:
    out = []
    for e in events:
        old = stored.get(e.event_id)
        if e.event_id in unknown_ids and old is not None and old.is_active and old.status:
            e = replace(e, status=old.status)
        out.append(e)
    return out


# ---------- The sync ----------

# Runs one full sync and returns a report per source. `now` is the sync's
# time stamp (every row written gets it); tests pass a fixed one.
def run_sync(conn: sqlite3.Connection, clients: Clients | None = None, now: datetime | None = None) -> list[SourceReport]:
    clients = clients or make_clients()
    now = now or datetime.now(timezone.utc)
    sources = ["ntulearn_gradebook", "ntulearn_announcement", "ntulearn_message"]

    # Sign in and read this term's course codes once. If that fails, every
    # source fails the same way: record it and stop.
    try:
        clients.http.ensure_signed_in()
        codes = {c.code for c in clients.courses.list_courses().courses}
    except Exception as e:  # noqa: BLE001 - any failure here is reported, not raised
        error = _short_error(e)
        reports = [SourceReport(s, error=error) for s in sources]
        for r in reports:
            _save_state(conn, r, now)
        return reports

    return [
        _run_source(conn, "ntulearn_gradebook", lambda: _fetch_deadlines(conn, clients), "due_at",
                    now - timedelta(days=DEADLINE_DAYS_BACK), now + timedelta(days=DEADLINE_DAYS_AHEAD), codes, now),
        _run_source(conn, "ntulearn_announcement", lambda: _fetch_announcements(clients), "posted_at",
                    now - timedelta(days=POST_DAYS), now, codes, now),
        _run_source(conn, "ntulearn_message", lambda: _fetch_messages(clients), "posted_at",
                    now - timedelta(days=POST_DAYS), now, codes, now),
    ]


# Fetch, convert and save one source; never raises (a failure goes in the report).
def _run_source(
    conn: sqlite3.Connection, source: str, fetch: Callable[[], tuple[list[Event], list[str]]],
    time_field: str, since: datetime, until: datetime, codes: set[str], now: datetime,
) -> SourceReport:
    report = SourceReport(source)
    started = time.monotonic()
    try:
        events, report.notes = fetch()
        covers = make_covers(source, time_field, since, until, codes, failed_courses(report.notes, codes))
        result = save_events(conn, events, now, covers)
        report.ok, report.seen, report.counts = True, result.seen, result.counts()
        report.removals_checked = covers is not None
    except Exception as e:  # noqa: BLE001 - one source failing mustn't stop the others
        report.error = _short_error(e)
        log.warning("%s failed: %s", source, report.error)
    report.seconds = time.monotonic() - started
    _save_state(conn, report, now)
    return report


# Deadlines from 14 days back to 120 ahead, as events, with known statuses kept.
def _fetch_deadlines(conn: sqlite3.Connection, clients: Clients) -> tuple[list[Event], list[str]]:
    result = clients.deadlines.upcoming(days=DEADLINE_DAYS_AHEAD, past_days=DEADLINE_DAYS_BACK)
    events = deadlines_to_events(result.deadlines)
    unknown = {f"ntulearn_gradebook:{d.column_id}" for d in result.deadlines if d.status == "unknown"}
    if unknown:
        events = keep_known_status(events, unknown, load_events(conn))
    return events, result.notes


# Announcements from the last 30 days, as events.
def _fetch_announcements(clients: Clients) -> tuple[list[Event], list[str]]:
    result = clients.announcements.recent(since_days=POST_DAYS)
    return announcements_to_events(result.announcements), result.notes


# Messages from the last 30 days, as events.
def _fetch_messages(clients: Clients) -> tuple[list[Event], list[str]]:
    result = clients.messages.recent(since_days=POST_DAYS)
    return messages_to_events(result.messages), result.notes


# Writes the four state notes for one source and commits them.
def _save_state(conn: sqlite3.Connection, report: SourceReport, now: datetime) -> None:
    stamp = time_to_db(now)
    with conn:
        set_state(conn, f"last_attempt:{report.source}", stamp)
        if report.ok:
            set_state(conn, f"last_sync:{report.source}", stamp)
        set_state(conn, f"last_error:{report.source}", report.error or "")
        set_state(conn, f"last_notes:{report.source}", "\n".join(report.notes))


# An error as one short line: its type and message (our errors never contain
# cookie values or passwords; status codes and names only).
def _short_error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:ERROR_MAX_CHARS]


# Self-test: uv run python -m uni_copilot.store.sync          (in memory, two syncs)
#            uv run python -m uni_copilot.store.sync --real   (one sync into .data/uni-copilot.sqlite)
# Live, GET only. Prints counts, times and statuses only; never titles or bodies.
def _main() -> None:
    parser = argparse.ArgumentParser(description="Live self-test of one sync (GET only, counts only).")
    parser.add_argument("--real", action="store_true", help="save into the real database instead of memory")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")

    conn = open_db() if args.real else open_db(":memory:")
    clients = make_clients()
    rounds = 1 if args.real else 2
    for n in range(1, rounds + 1):
        print(f"{n}) sync {'into the real database' if args.real else 'in memory'}:")
        for r in run_sync(conn, clients):
            what = r.counts or "no changes"
            removals = "removals checked" if r.removals_checked else "removals skipped"
            status = f"seen {r.seen}, {what}, {removals}" if r.ok else f"FAILED: {r.error}"
            print(f"   {r.source:<22} {r.seconds:5.1f} s  {status}")
            for note in r.notes:
                print(f"   {'':<22} note: {note}")
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    diary = conn.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0]
    print(f"Store now holds {total} events and {diary} diary lines.")
    conn.close()


if __name__ == "__main__":
    _main()
