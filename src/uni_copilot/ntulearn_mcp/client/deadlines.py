"""
Upcoming deadlines: calendar + gradebook merged, each with your submission status.

In plain words:
  Calendar  = "what's due, and when?"  One request for all courses, no status,
              can miss items. (client/calendar.py)
  Gradebook = "what's due, and have I done it?"  One request per course site,
              same due dates + your status, catches what the calendar misses.
              (client/gradebook.py)
  This file = the UNION of both (nothing missed, each item once, matched by
              id) with the status ALWAYS taken from the gradebook.

  What neither source catches:
  - Work submitted outside NTULearn (Gradescope, Google Forms): may stay
    not_submitted. Phase 2's mark_done is for that.
  - Deadlines that only appear in text ("bring your matric card to Friday's
    quiz"): that's what recent_announcements is for.
  - Calendar items that aren't gradebook columns (course events, personal
    entries): none seen yet; they would show status "unknown".

How it works (plan, section 4.1 and Appendix A):
1. The calendar gives due dates for all courses in one call (cheap, but can
   miss items, and it returns items from the start of the day you ask for).
2. The gradebook gives due dates per course site (one call each) and catches
   what the calendar misses. Column id = calendar item id, so duplicates are
   removed by id: the union of both, each item once.
3. Your gradebook row gives the status, fetched only for sites that actually
   have something due in the window.
4. Everything is filtered by the exact due time and sorted soonest first.

Speed: at ~1 request/s a cold call costs ~20 s. Gradebook columns rarely change,
so they're cached in memory for COLUMNS_TTL_S; statuses are always fetched
fresh, because they change the moment you submit.
"""

import argparse
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from uni_copilot.ntulearn_mcp.auth.session import AuthError
from uni_copilot.ntulearn_mcp.client.calendar import CalendarItem, calendar_items
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient, match_courses
from uni_copilot.ntulearn_mcp.client.gradebook import DueColumn, due_columns, my_statuses, status_for
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError, NtuLearnHttp
from uni_copilot.ntulearn_mcp.models import Course, Deadline, DeadlineList, SubmissionStatus

log = logging.getLogger(__name__)

COLUMNS_TTL_S = 30 * 60
MAX_DAYS = 120  # about one semester; the calendar splits it into 16-week windows anyway


# Builds DeadlineLists. Keeps the column cache, so the server creates one of
# these at startup and reuses it for every upcoming_deadlines call.
class DeadlinesClient:
    # Keeps the shared HTTP and courses clients; the column cache starts empty.
    def __init__(self, http: NtuLearnHttp, courses: CoursesClient):
        self._http = http
        self._courses = courses
        self._lock = threading.Lock()
        self._columns: dict[str, tuple[float, list[DueColumn]]] = {}

    # The main entry point: everything due in the next `days` days for this
    # term's courses (or one course if `course` is given, e.g. "IE2108").
    # Raises ValueError for a bad `days` or a course that matches 0 or 2+ subjects.
    def upcoming(self, days: int = 7, course: str | None = None) -> DeadlineList:
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_DAYS}")
        subjects = self._pick_subjects(course)
        site_codes = {s.id: c.code for c in subjects for s in c.sites}
        now = datetime.now(timezone.utc)
        until = now + timedelta(days=days)
        notes: list[str] = []

        # 1. Calendar: one call (per 16 weeks) for every course.
        try:
            cal = calendar_items(self._http, now, until)
        except NtuLearnError as e:
            cal = []
            notes.append(f"Calendar unavailable ({e.status}); using the gradebook only.")
        other = sum(1 for i in cal if i.calendar_id not in site_codes and since_until(i.end, now, until))
        if other and course is None:
            notes.append(f"{other} item(s) from sites outside this term's courses were left out.")

        # 2. Gradebook columns for every site in scope (cached).
        columns: list[DueColumn] = []
        for site_id, code in site_codes.items():
            try:
                columns += self._cached_columns(site_id)
            except NtuLearnError as e:
                notes.append(f"{code} gradebook unavailable on site {site_id} ({e.status}); "
                             "items only in its gradebook may be missing.")

        # 3. Merge, then fetch your status only where something is due.
        merged = merge_items(cal, columns, site_codes, now, until)
        statuses: dict[str, dict[str, SubmissionStatus] | None] = {}
        for site_id in sorted({m.course_site_id for m in merged}):
            try:
                statuses[site_id] = my_statuses(self._http, site_id, self._courses.user_id())
            except NtuLearnError as e:
                statuses[site_id] = None
                notes.append(f"{site_codes[site_id]} status unavailable ({e.status}); shown as unknown.")

        deadlines = [with_status(m, site_codes, statuses) for m in merged]
        log.info("deadlines: %d in %d day(s)", len(deadlines), days)
        return DeadlineList(days=days, deadlines=deadlines, notes=notes)

    # The subjects in scope: all of this term's, or the ones matching `course`.
    def _pick_subjects(self, course: str | None) -> list[Course]:
        all_courses = self._courses.list_courses().courses
        if course is None:
            return all_courses
        found = match_courses(all_courses, course)
        codes = ", ".join(c.code for c in all_courses)
        if not found:
            raise ValueError(f"No course matches '{course}'. This term: {codes}")
        if len(found) > 1:
            raise ValueError(f"'{course}' matches several courses: {', '.join(c.code for c in found)}")
        return found

    # One site's due columns, from the cache if fetched less than 30 min ago.
    def _cached_columns(self, site_id: str) -> list[DueColumn]:
        with self._lock:
            hit = self._columns.get(site_id)
            if hit and time.monotonic() - hit[0] < COLUMNS_TTL_S:
                return hit[1]
        cols = due_columns(self._http, site_id)
        with self._lock:
            self._columns[site_id] = (time.monotonic(), cols)
        return cols


# ---------- Pure functions (no network; tested offline in step 15) ----------

# True if `due` is after `since` and no later than `until`. The calendar
# returns items from the start of the since-day, so this exact check matters.
def since_until(due: datetime, since: datetime, until: datetime) -> bool:
    return since < due <= until


# One deadline before its status is known: its details and where it was found.
# Not frozen (unlike CalendarItem): `sources` grows to both when the gradebook
# has the same id as a calendar item.
@dataclass
class Merged:
    id: str
    course_site_id: str
    title: str
    due: datetime
    sources: list[str]


# Union of calendar items and gradebook columns, one entry per id, only for
# sites in scope and due inside (since, until], soonest first.
# When both sources have an item, the calendar's due time and title win: the
# calendar is what NTULearn's own "Due dates" page shows you (34/34 match).
def merge_items(
    cal: list[CalendarItem], columns: list[DueColumn], site_codes: dict[str, str],
    since: datetime, until: datetime,
) -> list[Merged]:
    out: dict[str, Merged] = {}
    for i in cal:
        if i.calendar_id in site_codes and since_until(i.end, since, until):
            out[i.id] = Merged(i.id, i.calendar_id, i.title, i.end, ["calendar"])
    for c in columns:
        if c.id in out:
            out[c.id].sources.append("gradebook")
        elif c.course_site_id in site_codes and since_until(c.due, since, until):
            out[c.id] = Merged(c.id, c.course_site_id, c.title, c.due, ["gradebook"])
    return sorted(out.values(), key=lambda m: (m.due, m.id))


# Turns a Merged item into the Deadline model Claude receives. statuses[site]
# is None when that site's status couldn't be read -> "unknown".
def with_status(
    m: Merged, site_codes: dict[str, str], statuses: dict[str, dict[str, SubmissionStatus] | None],
) -> Deadline:
    site_statuses = statuses.get(m.course_site_id)
    status: SubmissionStatus = "unknown" if site_statuses is None else status_for(m.id, site_statuses)
    return Deadline(
        column_id=m.id,
        course_code=site_codes[m.course_site_id],
        course_site_id=m.course_site_id,
        title=m.title,
        due_at=m.due,
        status=status,
        sources=m.sources,
    )


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.deadlines
# Live, GET only. Prints codes, due times, statuses and sources; never titles.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of upcoming deadlines (GET only).").parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    from uni_copilot.ntulearn_mcp.config import load_settings

    http = NtuLearnHttp()
    client = DeadlinesClient(http, CoursesClient(http, load_settings()))
    try:
        http.ensure_signed_in()
    except AuthError as e:
        print("Not signed in:", e)
        return

    t0 = time.monotonic()
    week = client.upcoming(days=7)
    print(f"1) next 7 days: {len(week.deadlines)} deadlines ({time.monotonic() - t0:.0f} s, cold)")
    for d in week.deadlines:
        print(f"   {d.course_code:7} {d.due_sgt}  {d.status:13} {'+'.join(d.sources)}")
    print(f"   notes: {week.notes or 'none'}")

    t0 = time.monotonic()
    month = client.upcoming(days=30)
    print(f"2) next 30 days: {len(month.deadlines)} deadlines ({time.monotonic() - t0:.0f} s, columns cached)")
    print(f"   status: {dict(Counter(d.status for d in month.deadlines))}")
    print(f"   sources: {dict(Counter('+'.join(d.sources) for d in month.deadlines))}")
    print(f"   per course: {dict(Counter(d.course_code for d in month.deadlines))}")

    if month.deadlines:
        code = month.deadlines[0].course_code
        one = client.upcoming(days=30, course=code.lower())
        print(f"3) course='{code.lower()}': {len(one.deadlines)} deadlines, all {code}: "
              f"{all(d.course_code == code for d in one.deadlines)}")
    try:
        client.upcoming(days=7, course="zzzz")
    except ValueError as e:
        print("4) course='zzzz' ->", e)


if __name__ == "__main__":
    _main()
