"""Offline tests for client/calendar.py and client/gradebook.py."""

import json
from datetime import datetime, timedelta, timezone

from uni_copilot.ntulearn_mcp.client.calendar import parse_time, split_windows, to_item
from uni_copilot.ntulearn_mcp.client.gradebook import STATUS_MAP, status_for, statuses_from_rows

T0 = datetime(2026, 10, 24, tzinfo=timezone.utc)


# 20 weeks with a 16-week limit -> [16 weeks, 4 weeks], back to back.
def test_split_windows_long_range():
    windows = split_windows(T0, T0 + timedelta(weeks=20), 16)
    assert windows == [(T0, T0 + timedelta(weeks=16)), (T0 + timedelta(weeks=16), T0 + timedelta(weeks=20))]


# A short range is one window; an empty or backwards range is none.
def test_split_windows_short_and_empty():
    assert split_windows(T0, T0 + timedelta(days=7), 16) == [(T0, T0 + timedelta(days=7))]
    assert split_windows(T0, T0, 16) == []
    assert split_windows(T0, T0 - timedelta(days=1), 16) == []


# NTULearn's "Z" times become UTC; missing or broken values become None.
def test_parse_time():
    assert parse_time("2026-10-27T15:59:00.000Z") == datetime(2026, 10, 27, 15, 59, tzinfo=timezone.utc)
    assert parse_time("2026-10-27T23:59:00+08:00") == datetime(2026, 10, 27, 15, 59, tzinfo=timezone.utc)
    assert parse_time(None) is None
    assert parse_time("not a date") is None


# A calendar item keeps id, type, site id, title, start/end; broken ones are skipped.
def test_to_item():
    item = to_item({"id": "_5_1", "type": "GradebookColumn", "calendarId": "_10_1", "title": "Quiz 1",
                    "start": "2026-10-27T15:59:00.000Z", "end": "2026-10-27T15:59:00.000Z"})
    assert item is not None and item.id == "_5_1" and item.calendar_id == "_10_1"
    assert to_item({"type": "GradebookColumn"}) is None  # no id
    assert to_item({"id": "_6_1", "end": None}) is None   # no time


# Status mapping: NeedsGrading -> submitted, Graded -> graded, exempt wins,
# anything unknown -> "unknown". Rows without a columnId are ignored.
def test_statuses_from_rows():
    rows = [
        {"columnId": "_1_1", "status": "NeedsGrading"},
        {"columnId": "_2_1", "status": "Graded"},
        {"columnId": "_3_1", "status": "Graded", "exempt": True},
        {"columnId": "_4_1", "status": "SomethingNew"},
        {"status": "Graded"},
    ]
    assert statuses_from_rows(rows) == {
        "_1_1": "submitted", "_2_1": "graded", "_3_1": "exempt", "_4_1": "unknown",
    }


# Grades never leak: even when rows carry grade fields, the output holds only statuses.
def test_statuses_drop_grade_fields():
    rows = [{"columnId": "_1_1", "status": "Graded", "displayGrade": {"text": "A+", "score": 97.5},
             "score": 97.5, "text": "A+"}]
    out = statuses_from_rows(rows)
    assert out == {"_1_1": "graded"}
    dumped = json.dumps(out)
    assert "A+" not in dumped and "97.5" not in dumped


# A column with no entry in your row means nothing was submitted.
def test_status_for_missing_row():
    assert status_for("_9_1", {"_1_1": "graded"}) == "not_submitted"
    assert status_for("_1_1", {"_1_1": "graded"}) == "graded"


# Only the two verified NTULearn values are mapped.
def test_status_map_values():
    assert STATUS_MAP == {"NeedsGrading": "submitted", "Graded": "graded"}
