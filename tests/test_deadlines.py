"""Offline tests for client/deadlines.py: merging calendar + gradebook, status, filtering."""

from datetime import timedelta

import pytest

from conftest import NOW, iso
from uni_copilot.ntulearn_mcp.client.calendar import CalendarItem
from uni_copilot.ntulearn_mcp.client.deadlines import DeadlinesClient, merge_items, since_until, with_status
from uni_copilot.ntulearn_mcp.client.gradebook import DueColumn
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError

SITES = {"_10_1": "XX1001", "_11_1": "XX1001", "_20_1": "YY2002"}
WEEK = NOW + timedelta(days=7)


# Shorthand for a CalendarItem due `hours` from now.
def cal(id, site, hours, title="T"):
    t = NOW + timedelta(hours=hours)
    return CalendarItem(id=id, type="GradebookColumn", calendar_id=site, title=title, start=t, end=t)


# Shorthand for a DueColumn due `hours` from now.
def col(id, site, hours, title="T"):
    return DueColumn(id=id, course_site_id=site, title=title, due=NOW + timedelta(hours=hours))


# Due after "since" and no later than "until"; exactly "since" doesn't count.
def test_since_until():
    assert since_until(NOW + timedelta(hours=1), NOW, WEEK)
    assert not since_until(NOW, NOW, WEEK)
    assert since_until(WEEK, NOW, WEEK)
    assert not since_until(WEEK + timedelta(seconds=1), NOW, WEEK)


# Same id in both sources -> one item from both; gradebook-only items are kept;
# items due earlier today (the calendar returns those) and other sites are dropped.
def test_merge_items():
    merged = merge_items(
        [cal("_1_1", "_10_1", 5, "Cal title"), cal("_2_1", "_10_1", -3), cal("_3_1", "_99_1", 5)],
        [col("_1_1", "_10_1", 5, "Gb title"), col("_4_1", "_20_1", 2)],
        SITES, NOW, WEEK,
    )
    assert [(m.id, m.sources) for m in merged] == [("_4_1", ["gradebook"]), ("_1_1", ["calendar", "gradebook"])]
    assert merged[1].title == "Cal title"  # the calendar's title wins


# Status comes from your row; a site whose status couldn't be read gives "unknown".
def test_with_status():
    m = merge_items([cal("_1_1", "_10_1", 5)], [col("_2_1", "_20_1", 6)], SITES, NOW, WEEK)
    statuses = {"_10_1": {"_1_1": "submitted"}, "_20_1": None}
    deadlines = [with_status(x, SITES, statuses) for x in m]
    assert [(d.course_code, d.status) for d in deadlines] == [("XX1001", "submitted"), ("YY2002", "unknown")]


# The full client on fake NTULearn data: union, status, filtering, and notes.
def test_deadlines_client_end_to_end(make_courses):
    calendar = {"results": [
        {"id": "_1_1", "type": "GradebookColumn", "calendarId": "_10_1", "title": "A1", "start": iso(24), "end": iso(24)},
        {"id": "_2_1", "type": "GradebookColumn", "calendarId": "_10_1", "title": "Old", "start": iso(-2), "end": iso(-2)},
        {"id": "_9_1", "type": "GradebookColumn", "calendarId": "_40_1", "title": "Training", "start": iso(5), "end": iso(5)},
    ]}
    routes = {
        "/learn/api/public/v1/calendars/items": lambda params: calendar,
        "/learn/api/public/v2/courses/_10_1/gradebook/columns": {"results": [
            {"id": "_1_1", "name": "A1", "grading": {"due": iso(24)}},
            {"id": "_3_1", "name": "Only in gradebook", "grading": {"due": iso(48)}},
            {"id": "_8_1", "name": "Total", "grading": {}},
        ]},
        "/learn/api/public/v2/courses/_11_1/gradebook/columns": {"results": []},
        "/learn/api/public/v2/courses/_20_1/gradebook/columns": NtuLearnError("/x", 403),
        "/learn/api/public/v2/courses/_10_1/gradebook/users/_u_1": {"results": [
            {"columnId": "_1_1", "status": "NeedsGrading", "displayGrade": {"text": "-"}},
        ]},
    }
    courses, http = make_courses(routes)
    result = DeadlinesClient(http, courses).upcoming(days=7)

    assert [(d.column_id, d.status, d.sources) for d in result.deadlines] == [
        ("_1_1", "submitted", ["calendar", "gradebook"]),
        ("_3_1", "not_submitted", ["gradebook"]),
    ]
    assert result.deadlines[0].due_sgt.endswith("SGT")
    notes = " ".join(result.notes)
    assert "YY2002 gradebook unavailable" in notes          # the 403 site became a note
    assert "1 item(s) from sites outside" in notes          # the training item was left out


# Gradebook columns are cached between calls; statuses are fetched every time.
def test_columns_cached_statuses_fresh(make_courses):
    routes = {
        "/learn/api/public/v1/calendars/items": lambda params: {"results": []},
        "/learn/api/public/v2/courses/_10_1/gradebook/columns": {"results": [
            {"id": "_1_1", "name": "A1", "grading": {"due": iso(24)}}]},
        "/learn/api/public/v2/courses/_11_1/gradebook/columns": {"results": []},
        "/learn/api/public/v2/courses/_20_1/gradebook/columns": {"results": []},
        "/learn/api/public/v2/courses/_10_1/gradebook/users/_u_1": {"results": []},
    }
    courses, http = make_courses(routes)
    client = DeadlinesClient(http, courses)
    client.upcoming(days=7)
    client.upcoming(days=7)
    assert sum(c.endswith("/_10_1/gradebook/columns") for c in http.calls) == 1
    assert sum(c.endswith("/gradebook/users/_u_1") for c in http.calls) == 2


# Bad inputs are refused with a readable message before any request.
def test_deadlines_bad_input(make_courses):
    courses, http = make_courses()
    client = DeadlinesClient(http, courses)
    with pytest.raises(ValueError, match="days"):
        client.upcoming(days=0)
    with pytest.raises(ValueError, match="No course matches"):
        client.upcoming(days=7, course="zzzz")
