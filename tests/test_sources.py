"""
Offline tests for sources/ntulearn.py: Phase 1 results -> store Events, and
what changes the content_hash (a real edit) and what doesn't (re-fetching,
opening a message, the calendar missing an item). Made-up data only.
"""

from datetime import datetime, timezone

import pytest

from uni_copilot.ntulearn_mcp.models import Announcement, Deadline, Message
from uni_copilot.sources.ntulearn import (
    STATUS_FROM_SUBMISSION, announcement_to_event, content_hash, deadline_to_event, message_to_event, message_title,
)

POSTED = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
DEADLINE = Deadline(column_id="_9_1", course_code="XX1001", course_site_id="_10_1", title="Made-up Lab 1",
                    due_at="2026-11-07T15:59:00Z", status="not_submitted", sources=["gradebook", "calendar"])
ANNOUNCEMENT = Announcement(id="_8_1", course_code="XX1001", course_site_ids=["_10_1", "_11_1"],
                            title="Made-up venue", body="Quiz in **LT1**.\n\n[file: seats.pdf]", created_at=POSTED)
MESSAGE = Message(id="_7_1", conversation_id="_5_1", course_code="XX1001", course_site_id="_10_1",
                  sender_name="Made-up Prof", body="See you.", posted_at=POSTED, is_read=False)


# Ids, kinds, times and fields copied across; found_in is sorted.
def test_adapters():
    d, a, m = deadline_to_event(DEADLINE), announcement_to_event(ANNOUNCEMENT), message_to_event(MESSAGE)
    assert (d.event_id, d.kind, d.status, d.found_in) == ("ntulearn_gradebook:_9_1", "deadline", "open",
                                                          ("calendar", "gradebook"))
    assert d.due_at == datetime(2026, 11, 7, 15, 59, tzinfo=timezone.utc)
    assert (a.event_id, a.course_site_id, a.posted_at, a.body) == ("ntulearn_announcement:_8_1", "_10_1", POSTED,
                                                                   ANNOUNCEMENT.body)
    assert a.url.endswith("/ultra/courses/_10_1/outline")
    assert (m.event_id, m.title, m.body) == ("ntulearn_message:_7_1", "Message from Made-up Prof", "See you.")


# Every Phase 1 status has a store status; unknown and not_submitted stay on the to-do list.
@pytest.mark.parametrize("before,after", [("not_submitted", "open"), ("unknown", "open"),
                                          ("submitted", "submitted"), ("graded", "graded"), ("exempt", "done")])
def test_status_mapping(before, after):
    assert STATUS_FROM_SUBMISSION[before] == after
    assert deadline_to_event(DEADLINE.model_copy(update={"status": before})).status == after


# Same content -> same hash; a real edit -> a different one.
def test_hash_changes_on_edits_only():
    base = announcement_to_event(ANNOUNCEMENT).content_hash
    assert announcement_to_event(ANNOUNCEMENT.model_copy()).content_hash == base
    assert announcement_to_event(ANNOUNCEMENT.model_copy(update={"body": "Quiz in **LT2**."})).content_hash != base
    assert announcement_to_event(ANNOUNCEMENT.model_copy(update={"title": "Other"})).content_hash != base
    # Found on one more site: not an edit.
    more_sites = ANNOUNCEMENT.model_copy(update={"course_site_ids": ["_10_1", "_11_1", "_12_1"]})
    assert announcement_to_event(more_sites).content_hash == base


# Fields that change by themselves stay out of the hash.
def test_hash_ignores_self_changing_fields():
    assert message_to_event(MESSAGE.model_copy(update={"is_read": True})).content_hash == \
        message_to_event(MESSAGE).content_hash
    d = deadline_to_event(DEADLINE).content_hash
    assert deadline_to_event(DEADLINE.model_copy(update={"sources": ["gradebook"]})).content_hash == d
    assert deadline_to_event(DEADLINE.model_copy(update={"status": "submitted"})).content_hash == d


# The separator keeps parts apart; the hash is 64 hex characters.
def test_content_hash():
    assert content_hash("ab", "c") != content_hash("a", "bc")
    assert content_hash(None) == content_hash("")
    assert len(content_hash("x")) == 64


# Messages get a title: from you, from the sender, or just "Message".
def test_message_title():
    assert message_title(MESSAGE) == "Message from Made-up Prof"
    assert message_title(MESSAGE.model_copy(update={"sent_by_me": True})) == "Message from you"
    assert message_title(MESSAGE.model_copy(update={"sender_name": None})) == "Message"
