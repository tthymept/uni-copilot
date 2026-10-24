"""Offline tests for models.py: UTC/SGT times, frozen results, schemas, no grade fields."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from uni_copilot.ntulearn_mcp.models import Announcement, Deadline, DeadlineList, Message, sgt_text


# Builds a valid made-up Deadline; tests change one field at a time.
def make_deadline(**changes):
    fields = dict(column_id="_1_1", course_code="XX1001", course_site_id="_10_1", title="A1",
                  due_at="2026-10-27T15:59:00.000Z", status="not_submitted", sources=["calendar"])
    return Deadline(**{**fields, **changes})


# Times are stored in UTC and shown in SGT (UTC+8).
def test_utc_and_sgt():
    d = make_deadline(due_at="2026-10-27T23:59:00+08:00")
    assert d.due_at == datetime(2026, 10, 27, 15, 59, tzinfo=timezone.utc)
    assert d.due_sgt == "Tue 27 Oct 23:59 SGT"
    assert sgt_text(datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)) == "Fri 02 Jan 00:00 SGT"


# A time without a timezone is taken as UTC (NTULearn sends UTC).
def test_naive_time_is_utc():
    assert make_deadline(due_at="2026-10-27T15:59:00").due_at.tzinfo == timezone.utc


# Only the 5 known statuses are accepted.
def test_status_values():
    for s in ("not_submitted", "submitted", "graded", "exempt", "unknown"):
        assert make_deadline(status=s).status == s
    with pytest.raises(ValidationError):
        make_deadline(status="maybe")


# Results are frozen: they can't be changed after they're built.
def test_frozen():
    d = make_deadline()
    with pytest.raises(ValidationError):
        d.status = "graded"


# Computed fields are sent to Claude and described in the output schema.
def test_computed_fields_in_json_and_schema():
    d = make_deadline()
    assert {"due_sgt", "url"} <= set(d.model_dump())
    schema = DeadlineList.model_json_schema()
    assert {"due_sgt", "url"} <= set(schema["$defs"]["Deadline"]["properties"])
    assert {"created_sgt", "modified_sgt", "edited"} <= set(Announcement.model_json_schema()["properties"])
    assert "posted_sgt" in Message.model_json_schema()["properties"]


# Text written by other people carries the UNTRUSTED label Claude sees.
def test_untrusted_labels():
    props = Announcement.model_json_schema()["properties"]
    for field in ("title", "body", "files"):
        assert "UNTRUSTED" in props[field]["description"]
    assert "UNTRUSTED" in Message.model_json_schema()["properties"]["sender_name"]["description"]


# No model has a field that could hold a grade or score.
def test_no_grade_fields():
    for model in (Deadline, DeadlineList, Announcement, Message):
        for name in model.model_json_schema().get("properties", {}):
            assert "grade" not in name.lower() and "score" not in name.lower()


# "edited" means modified more than a minute after creation.
def test_edited_threshold():
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    base = dict(id="_1_1", course_code="XX1001", course_site_ids=["_10_1"], title="t", body="b", created_at=created)
    assert not Announcement(**base, modified_at=created + timedelta(seconds=30)).edited
    assert Announcement(**base, modified_at=created + timedelta(minutes=5)).edited
    assert not Announcement(**base).edited
