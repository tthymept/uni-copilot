"""
Gradebook: due dates per course, and whether you've submitted each item.

Verified facts this builds on (plan, Appendix A):
- GET /learn/api/public/v2/courses/{id}/gradebook/columns lists a course's
  gradebook columns; `grading.due` is the due date. Some items with a due date
  are missing from the calendar, so step 9 merges both sources.
- GET /learn/api/public/v2/courses/{id}/gradebook/users/{userId} is your row:
  one entry per column with a `status`: NeedsGrading (submitted, not marked)
  or Graded. No entry for a column = nothing submitted.

Grades: that second endpoint also returns your grade for each column. This
file keeps ONLY columnId, status and exempt, and drops everything else the
moment the response arrives (see my_statuses). Nothing here is logged, cached
or returned that could contain a grade. The opt-in grades tool (later) is a
separate thing and stays off unless NTULEARN_ENABLE_GRADES=1.
"""

import argparse
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from uni_copilot.ntulearn_mcp.client.calendar import parse_time
from uni_copilot.ntulearn_mcp.client.http import NtuLearnHttp
from uni_copilot.ntulearn_mcp.models import SubmissionStatus

log = logging.getLogger(__name__)

# NTULearn status value -> our status. Anything not listed becomes "unknown".
STATUS_MAP: dict[str, SubmissionStatus] = {
    "NeedsGrading": "submitted",
    "Graded": "graded",
}


# One gradebook column that has a due date. Internal, like CalendarItem.
@dataclass(frozen=True)
class DueColumn:
    id: str              # gradebook column id = calendar item id
    course_site_id: str
    title: str           # written by the prof: untrusted text
    due: datetime        # UTC


# Every column in one course site that has a due date (columns without one,
# like totals, are skipped). Follows paging for big gradebooks.
def due_columns(http: NtuLearnHttp, course_site_id: str) -> list[DueColumn]:
    columns = []
    for raw in _fetch_all(http, f"/learn/api/public/v2/courses/{course_site_id}/gradebook/columns"):
        due = parse_time((raw.get("grading") or {}).get("due"))
        if raw.get("id") and due is not None:
            columns.append(DueColumn(id=raw["id"], course_site_id=course_site_id,
                                     title=raw.get("name") or "", due=due))
    return columns


# Your submission status for every column in one course site that has an
# entry in your row: {column id: status}. Columns missing from the result
# mean "not_submitted" (the caller decides that, see status_for).
def my_statuses(http: NtuLearnHttp, course_site_id: str, user_id: str) -> dict[str, SubmissionStatus]:
    rows = _fetch_all(http, f"/learn/api/public/v2/courses/{course_site_id}/gradebook/users/{user_id}")
    return statuses_from_rows(rows)


# GETs a list endpoint and follows paging.nextPage until the end.
def _fetch_all(http: NtuLearnHttp, path: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    next_path: str | None = path
    while next_path:
        body = http.get_json(next_path)
        results += body.get("results", [])
        next_path = (body.get("paging") or {}).get("nextPage")
    return results


# ---------- Pure functions (no network; tested offline in step 15) ----------

# Turns raw gradebook rows into {column id: status}, reading only columnId,
# status and exempt. Grade fields are never read, so they can't leak further.
def statuses_from_rows(rows: list[dict[str, Any]]) -> dict[str, SubmissionStatus]:
    out: dict[str, SubmissionStatus] = {}
    for row in rows:
        column_id = row.get("columnId")
        if not column_id:
            continue
        if row.get("exempt"):
            out[column_id] = "exempt"
        else:
            out[column_id] = STATUS_MAP.get(row.get("status") or "", "unknown")
    return out


# The status of one column: from your row if it's there, else not_submitted.
def status_for(column_id: str, statuses: dict[str, SubmissionStatus]) -> SubmissionStatus:
    return statuses.get(column_id, "not_submitted")


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.gradebook
# Live, GET only. Prints counts and status names, never titles or grades.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of the gradebook client (GET only).").parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    from datetime import timedelta, timezone

    from uni_copilot.ntulearn_mcp.client.calendar import calendar_items
    from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
    from uni_copilot.ntulearn_mcp.config import load_settings

    http = NtuLearnHttp()
    courses = CoursesClient(http, load_settings())
    uid = courses.user_id()
    sites = [s for c in courses.list_courses().courses for s in c.sites]
    now = datetime.now(timezone.utc)

    future: list[DueColumn] = []
    future_status: Counter[str] = Counter()
    past_status: Counter[str] = Counter()
    for site in sites:
        cols = due_columns(http, site.id)
        statuses = my_statuses(http, site.id, uid) if cols else {}
        for col in cols:
            s = status_for(col.id, statuses)
            (future_status if col.due > now else past_status)[s] += 1
            if col.due > now:
                future.append(col)
    print(f"1) {len(sites)} sites, {len(future)} future-due columns")
    print(f"2) your status, due in future: {dict(future_status)}")
    print(f"   your status, due in past:   {dict(past_status)}")

    cal = {i.id for i in calendar_items(http, now, now + timedelta(weeks=20))}
    gb = {c.id for c in future if c.due <= now + timedelta(weeks=20)}
    print(f"3) next 20 weeks: gradebook {len(gb)}, calendar {len(cal)}, both {len(gb & cal)}, "
          f"gradebook only {len(gb - cal)}, calendar only {len(cal - gb)}")


if __name__ == "__main__":
    _main()
