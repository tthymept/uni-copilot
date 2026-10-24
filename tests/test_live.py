"""
Opt-in live smoke test against YOUR NTULearn account. Skipped unless
NTULEARN_LIVE=1. GET only (through the normal clients), ~1 request/s, and it
prints counts and statuses only, never titles, bodies, names or grades.

Run (PowerShell):
  $env:NTULEARN_LIVE='1'; uv run pytest tests/test_live.py -s; Remove-Item Env:NTULEARN_LIVE
"""

import os
from collections import Counter

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NTULEARN_LIVE") != "1", reason="set NTULEARN_LIVE=1 to run")


# One shared set of real clients for the whole module, so it signs in once.
@pytest.fixture(scope="module")
def clients():
    from uni_copilot.ntulearn_mcp.client.announcements import AnnouncementsClient
    from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
    from uni_copilot.ntulearn_mcp.client.deadlines import DeadlinesClient
    from uni_copilot.ntulearn_mcp.client.http import NtuLearnHttp
    from uni_copilot.ntulearn_mcp.client.messages import MessagesClient
    from uni_copilot.ntulearn_mcp.config import load_settings

    http = NtuLearnHttp()
    courses = CoursesClient(http, load_settings())
    return {
        "http": http,
        "courses": courses,
        "deadlines": DeadlinesClient(http, courses),
        "announcements": AnnouncementsClient(http, courses),
        "messages": MessagesClient(http, courses),
    }


# Signed in (silently) and users/me answers.
def test_live_signed_in(clients):
    clients["http"].ensure_signed_in()
    assert clients["http"].is_signed_in()
    print(f"\n  signed in, cookies: {len(clients['http'].cookie_summary())}")


# This term has at least one subject.
def test_live_courses(clients):
    result = clients["courses"].list_courses()
    print(f"\n  term {result.term}: {len(result.courses)} subjects, "
          f"{sum(len(c.sites) for c in result.courses)} sites")
    assert result.courses


# Deadlines for 14 days come back with statuses (counts only).
def test_live_deadlines(clients):
    result = clients["deadlines"].upcoming(days=14)
    print(f"\n  deadlines (14 days): {len(result.deadlines)}, "
          f"status {dict(Counter(d.status for d in result.deadlines))}, notes {len(result.notes)}")
    assert all(d.due_sgt.endswith("SGT") for d in result.deadlines)


# Announcements and messages for 7 days (counts only).
def test_live_news(clients):
    anns = clients["announcements"].recent(since_days=7)
    msgs = clients["messages"].recent(since_days=7)
    print(f"\n  announcements (7 days): {len(anns.announcements)}, messages (7 days): {len(msgs.messages)}")
