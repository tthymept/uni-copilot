"""
Shared test helpers. pytest loads this file automatically before the tests.

In plain words:
  The clients normally talk to NTULearn through NtuLearnHttp. In tests we hand
  them a FakeHttp instead: same method names (get_json, get_all), but it
  answers from a dictionary of made-up responses, so no network is used.
  All data here is synthetic (made-up course codes like XX1001), never real.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.config import Settings

USER_ID = "_u_1"
NOW = datetime.now(timezone.utc)


# Stands in for NtuLearnHttp. `routes` maps an API path to the JSON it should
# return: a dict, a function of the request params (for answers that depend on
# them), or an exception to raise. Every requested path is recorded in `calls`.
class FakeHttp:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[str] = []

    # Same signature as NtuLearnHttp.get_json; looks the path up in routes.
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError(f"unexpected request: {path}")
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return value(params) if callable(value) else value

    # Same signature as NtuLearnHttp.get_all; our fake lists have one page.
    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        return self.get_json(path, params).get("results", [])


# Builds one raw course object like the memberships API returns.
def raw_course(id: str, display_id: str, name: str, term: str | None,
               available: bool = True, org: bool = False) -> dict[str, Any]:
    return {
        "id": id,
        "displayId": display_id,
        "displayName": name,
        "isAvailable": available,
        "isOrganization": org,
        "term": {"name": term} if term else None,
    }


# A made-up set of memberships: two subjects this term (one with lecture +
# tutorial sites), an unavailable site, an old term, and a Non-curriculum site.
RAW_COURSES = [
    raw_course("_10_1", "26S1-XX1001-LEC", "XX1001 DATA THINGS (LEC)", "26S1"),
    raw_course("_11_1", "26S1-XX1001-T01", "XX1001 DATA THINGS TUTORIAL T01", "26S1"),
    raw_course("_20_1", "26S1-YY2002-LEC", "YY2002 PROBABILITY", "26S1"),
    raw_course("_21_1", "26S1-YY2002-OLD", "YY2002 HIDDEN SITE", "26S1", available=False),
    raw_course("_30_1", "25S2-ZZ3003-LEC", "ZZ3003 LAST SEMESTER", "25S2"),
    raw_course("_40_1", "TRAINING_01", "SAFETY TRAINING", "Non-curriculum"),
]


# The two routes every CoursesClient needs: users/me and memberships.
def course_routes(raw_courses: list[dict[str, Any]] = RAW_COURSES) -> dict[str, Any]:
    return {
        "/learn/api/v1/users/me": {"id": USER_ID},
        f"/learn/api/v1/users/{USER_ID}/memberships": {"results": [{"course": c} for c in raw_courses]},
    }


# A CoursesClient wired to a FakeHttp. Extra routes are added on top of the
# course routes, so tests for deadlines/announcements/messages can reuse it.
@pytest.fixture
def make_courses() -> Callable[..., tuple[CoursesClient, FakeHttp]]:
    def build(extra_routes: dict[str, Any] | None = None, term: str | None = None):
        http = FakeHttp({**course_routes(), **(extra_routes or {})})
        return CoursesClient(http, Settings(term_override=term, enable_grades=False)), http
    return build


# ISO time string like NTULearn sends ("...Z"), `hours` from now.
def iso(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------- Phase 2: the event store ----------

# A fixed "now" for store tests (Sun 25 Oct 2026, 12:00 SGT), so results don't
# depend on when the tests run.
T0 = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)


# A fresh database in memory for one test: tables created, nothing saved to disk.
@pytest.fixture
def db():
    from uni_copilot.store.db import open_db

    conn = open_db(":memory:")
    yield conn
    conn.close()


# A made-up deadline Event, due `days` after T0.
def deadline_event(n: int, days: float, status: str = "open", course: str = "XX1001", title: str | None = None):
    from uni_copilot.store.models import Event

    title = title or f"Made-up Assignment {n}"
    return Event(
        event_id=f"ntulearn_gradebook:_{n}_1", source="ntulearn_gradebook", kind="deadline",
        course_code=course, course_site_id="_10_1", title=title,
        due_at=T0 + timedelta(days=days), status=status, content_hash=f"hash:{title}",
        found_in=("calendar", "gradebook"),
    )


# A made-up announcement (or message) Event, posted `days` before T0.
def post_event(n: int, days_ago: float, body: str = "Made-up body.", course: str = "XX1001",
               kind: str = "announcement", title: str | None = None):
    from uni_copilot.store.models import Event

    source = "ntulearn_announcement" if kind == "announcement" else "ntulearn_message"
    title = title or f"Made-up post {n}"
    return Event(
        event_id=f"{source}:_{n}_1", source=source, kind=kind,
        course_code=course, course_site_id="_10_1", title=title, body=body,
        posted_at=T0 - timedelta(days=days_ago), content_hash=f"hash:{title}:{body}",
    )
