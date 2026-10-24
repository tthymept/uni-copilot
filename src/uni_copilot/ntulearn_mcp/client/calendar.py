"""
Calendar items: NTULearn's own list of due dates across all your courses.

In plain words:
  Calendar = "what's due, and when?"
  - One request covers ALL your courses (cheap).
  - It has NO status: it can't tell whether you've submitted something.
  - It can miss items (23 Oct: IE2106 "Assignment 1" was in the gradebook
    but not in the calendar).
  - It returns items from the START of the day you ask for, so items due
    earlier today come back too; the caller filters by exact time.
  That's why step 9 also uses the gradebook (status + missed items).

Verified facts this builds on (plan, Appendix A):
- GET /learn/api/public/v1/calendars/items?since=&until= matched NTULearn's
  "Due dates" view exactly (34/34 on 23 Oct).
- A range longer than 16 weeks is rejected (17 weeks -> 400), so long ranges
  are split into windows of at most 16 weeks.
- For gradebook items, the item id = the gradebook column id, and `end` = the
  due date. That's what lets step 9 merge this list with the gradebook.

One call covers every course, so this is the cheap source; the gradebook
(step 8) costs one call per course but catches items the calendar misses.
"""

import argparse
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from uni_copilot.ntulearn_mcp.client.http import NtuLearnHttp
from uni_copilot.ntulearn_mcp.config import CALENDAR_MAX_WEEKS

log = logging.getLogger(__name__)

CALENDAR_PATH = "/learn/api/public/v1/calendars/items"


# One calendar entry, trimmed to what we use. This is internal (not sent to
# Claude directly), so it's a plain frozen dataclass rather than a pydantic model.
@dataclass(frozen=True)
class CalendarItem:
    id: str              # for GradebookColumn items: the gradebook column id
    type: str            # GradebookColumn, Course, Personal, Institution, ...
    calendar_id: str     # for course items: the course site id
    title: str           # written by the prof: untrusted text
    start: datetime      # UTC
    end: datetime        # UTC; for gradebook items this is the due date


# Asks NTULearn for every calendar item between `since` and `until` (UTC),
# splitting the range into <=16-week windows and removing duplicates that
# appear in two windows (an item sitting exactly on a window boundary).
def calendar_items(http: NtuLearnHttp, since: datetime, until: datetime) -> list[CalendarItem]:
    seen: dict[str, CalendarItem] = {}
    windows = split_windows(since, until, CALENDAR_MAX_WEEKS)
    for start, end in windows:
        params = {"since": start.isoformat(), "until": end.isoformat()}
        for raw in http.get_all(CALENDAR_PATH, params):
            item = to_item(raw)
            if item is not None:
                seen[item.id] = item
    log.info("calendar: %d items in %d window(s)", len(seen), len(windows))
    return sorted(seen.values(), key=lambda i: i.end)


# ---------- Pure functions (no network; tested offline in step 15) ----------

# Cuts [since, until] into back-to-back pieces no longer than max_weeks.
# e.g. 20 weeks with max 16 -> [16 weeks, 4 weeks].
def split_windows(since: datetime, until: datetime, max_weeks: int) -> list[tuple[datetime, datetime]]:
    if until <= since:
        return []
    step = timedelta(weeks=max_weeks)
    windows = []
    start = since
    while start < until:
        end = min(start + step, until)
        windows.append((start, end))
        start = end
    return windows


# Reads an ISO time from NTULearn ("2026-10-27T15:59:00.000Z") as UTC.
# Returns None if it's missing or unreadable.
def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# Turns one raw calendar item into a CalendarItem. Skips (returns None) items
# with no id or no usable times, rather than failing the whole list.
def to_item(raw: dict[str, Any]) -> CalendarItem | None:
    start, end = parse_time(raw.get("start")), parse_time(raw.get("end"))
    if not raw.get("id") or end is None:
        return None
    return CalendarItem(
        id=raw["id"],
        type=raw.get("type") or "",
        calendar_id=raw.get("calendarId") or "",
        title=raw.get("title") or "",
        start=start or end,
        end=end,
    )


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.calendar
# Live, GET only. Prints counts and types, never titles.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of the calendar client (GET only).").parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
    from uni_copilot.ntulearn_mcp.config import load_settings

    http = NtuLearnHttp()
    now = datetime.now(timezone.utc)

    week = calendar_items(http, now, now + timedelta(days=7))
    print(f"1) next 7 days: {len(week)} items, types {dict(Counter(i.type for i in week))}")

    long = calendar_items(http, now, now + timedelta(weeks=20))
    print(f"2) next 20 weeks (2 windows): {len(long)} items, types {dict(Counter(i.type for i in long))}")
    print(f"   every 7-day item is also in the 20-week list: {set(i.id for i in week) <= set(i.id for i in long)}")

    sites = {s.id: c for c in CoursesClient(http, load_settings()).list_courses(include_organizations=True).courses
             for s in c.sites}
    known = [i for i in long if i.calendar_id in sites]
    print(f"3) calendar_id is one of your course sites: {len(known)}/{len(long)}")
    print(f"   per subject: {dict(Counter(sites[i.calendar_id].code for i in known))}")
    print(f"4) items where start == end: {sum(i.start == i.end for i in long)}/{len(long)}")


if __name__ == "__main__":
    _main()
