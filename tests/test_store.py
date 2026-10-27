"""
Offline tests for the store: store/models.py (shapes and checks), store/db.py
(tables, versions, row conversion) and store/events.py (saving and the diary).
All events are made up; every database lives in memory.
"""

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import T0, deadline_event, post_event
from uni_copilot.ntulearn_mcp.config import SGT
from uni_copilot.store.db import (
    get_state, migrate, row_to_event, schema_version, set_state, table_names, time_from_db, time_to_db,
)
from uni_copilot.store.events import all_changes, diff, load_events, save_events
from uni_copilot.store.models import Change, shorten


# ---------- models ----------

# Times must be UTC with a timezone; ids must start with their own source.
def test_event_checks():
    good = deadline_event(1, 3)
    with pytest.raises(ValueError):
        replace(good, due_at=datetime(2026, 11, 1, 12, 0))  # no timezone
    with pytest.raises(ValueError):
        replace(good, due_at=good.due_at.astimezone(SGT))  # SGT, not UTC
    with pytest.raises(ValueError):
        replace(good, event_id="ntulearn_message:_1_1")  # wrong source
    with pytest.raises(ValueError):
        replace(good, event_id="ntulearn_gradebook:")  # no native id
    with pytest.raises(ValueError):
        Change(good.event_id, "new", datetime(2026, 11, 1))
    assert good.native_id == "_1_1" and good.is_active


# The body is kept in full; preview is a one-line cut for lists only.
def test_body_and_preview():
    e = post_event(1, 1, body="Line one.\n\n- Line two.\n" * 50)
    assert len(e.body) > 1000
    assert len(e.preview) <= 280 and e.preview.endswith("...") and "\n" not in e.preview
    assert shorten("short  text\n") == "short text"


# ---------- db ----------

# A new database gets version 2 and the five tables; migrating again changes nothing.
def test_schema(db):
    assert schema_version(db) == 2
    assert table_names(db) == ["event_changes", "events", "marked_done", "notifications_sent", "state"]
    assert migrate(db) == 2


# Saving and reading back gives the very same Event, down to the microsecond.
def test_round_trip(db):
    e = replace(deadline_event(1, 3), due_at=T0 + timedelta(microseconds=123))
    save_events(db, [e], T0)
    stored = load_events(db)[e.event_id]
    assert stored == replace(e, first_seen_at=T0, last_seen_at=T0, modified_at=T0)
    row = db.execute("SELECT due_at FROM events").fetchone()
    assert row["due_at"] == "2026-10-25T04:00:00.000123+00:00"
    assert time_from_db(time_to_db(T0)) == T0


# Times stored as text sort in time order (same fixed length every time).
def test_time_text_sorts():
    times = [T0 + timedelta(days=d, microseconds=m) for d, m in [(3, 0), (0, 5), (-2, 999999)]]
    assert sorted(time_to_db(t) for t in times) == [time_to_db(t) for t in sorted(times)]


# State notes: insert, then replace.
def test_state(db):
    assert get_state(db, "last_seen:terminal") is None
    set_state(db, "last_seen:terminal", "a")
    set_state(db, "last_seen:terminal", "b")
    assert get_state(db, "last_seen:terminal") == "b"


# A diary line for an event that doesn't exist is refused (foreign key).
def test_foreign_key(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO event_changes (event_id, change_type, changed_at) VALUES ('x:1', 'new', 't')")


# ---------- events: save_events and diff ----------

# First save: everything is new, with bookkeeping filled in.
def test_first_save(db):
    result = save_events(db, [deadline_event(1, 3), post_event(2, 1)], T0)
    assert result.counts() == {"new": 2} and result.seen == 2
    e = load_events(db)["ntulearn_gradebook:_1_1"]
    assert e.first_seen_at == e.last_seen_at == e.modified_at == T0 and e.removed_at is None


# The Phase 2 "done" test: the same data twice -> the second save writes nothing.
def test_identical_save_is_idempotent(db):
    events = [deadline_event(1, 3), post_event(2, 1)]
    save_events(db, events, T0)
    result = save_events(db, events, T0 + timedelta(hours=2))
    assert result.changes == [] and result.unchanged == 2
    e = load_events(db)["ntulearn_gradebook:_1_1"]
    assert e.last_seen_at == T0 + timedelta(hours=2)  # seen again...
    assert e.modified_at == T0  # ...but not modified
    assert len(all_changes(db)) == 2


# Due time changed -> moved, with both times; status changed -> status_changed.
def test_moved_and_status_changed(db):
    save_events(db, [deadline_event(1, 3), deadline_event(2, 5)], T0)
    later = T0 + timedelta(hours=2)
    result = save_events(db, [deadline_event(1, 4), deadline_event(2, 5, "submitted")], later)
    by_type = {c.change_type: c for c in result.changes}
    assert set(by_type) == {"moved", "status_changed"}
    assert by_type["moved"].before == time_to_db(T0 + timedelta(days=3))
    assert by_type["moved"].after == time_to_db(T0 + timedelta(days=4))
    assert (by_type["status_changed"].before, by_type["status_changed"].after) == ("open", "submitted")
    assert load_events(db)["ntulearn_gradebook:_1_1"].modified_at == later


# Hash changed -> edited. Titles shown only when the title itself changed.
def test_edited(db):
    save_events(db, [post_event(1, 1), post_event(2, 1)], T0)
    result = save_events(db, [post_event(1, 1, body="Changed body."), post_event(2, 1, title="New title")], T0)
    edits = {c.event_id: c for c in result.changes}
    assert edits["ntulearn_announcement:_1_1"].before is None
    assert (edits["ntulearn_announcement:_2_1"].before, edits["ntulearn_announcement:_2_1"].after) == \
        ("Made-up post 2", "New title")
    assert load_events(db)["ntulearn_announcement:_1_1"].body == "Changed body."


# One event can change in several ways at once.
def test_several_changes_at_once():
    old = deadline_event(1, 3)
    new = replace(old, due_at=old.due_at + timedelta(days=1), status="submitted", content_hash="other")
    assert [c.change_type for c in diff(old, new, T0)] == ["moved", "status_changed", "edited"]


# Fields that aren't content (url, found_in) are saved but aren't a change.
def test_non_content_fields_are_not_changes():
    old = deadline_event(1, 3)
    assert diff(old, replace(old, url="https://x", found_in=("gradebook",)), T0) == []


# Without `covers`, a missing event is never marked removed (the safe default).
def test_no_covers_no_removal(db):
    save_events(db, [post_event(1, 1)], T0)
    assert save_events(db, [], T0 + timedelta(hours=2)).changes == []
    assert load_events(db)["ntulearn_announcement:_1_1"].is_active


# With `covers`, a missing covered event is removed once; uncovered ones stay.
def test_removed_only_if_covered(db):
    save_events(db, [post_event(1, 1), post_event(2, 1, course="YY2002")], T0)
    covers = lambda e: e.course_code == "XX1001"  # noqa: E731 - a tiny rule for the test
    result = save_events(db, [], T0 + timedelta(hours=2), covers)
    assert [(c.change_type, c.event_id) for c in result.changes] == [("removed", "ntulearn_announcement:_1_1")]
    stored = load_events(db)
    assert not stored["ntulearn_announcement:_1_1"].is_active and stored["ntulearn_announcement:_2_1"].is_active
    assert save_events(db, [], T0 + timedelta(hours=4), covers).changes == []  # not removed twice


# A removed event that comes back is "new" again, with before = "removed".
def test_reappears(db):
    save_events(db, [post_event(1, 1)], T0)
    save_events(db, [], T0 + timedelta(hours=2), lambda e: True)
    result = save_events(db, [post_event(1, 1)], T0 + timedelta(hours=4))
    assert [(c.change_type, c.before) for c in result.changes] == [("new", "removed")]
    assert load_events(db)["ntulearn_announcement:_1_1"].is_active


# The same event_id twice in one list is an adapter bug: refused, nothing saved.
def test_duplicate_ids_refused(db):
    with pytest.raises(ValueError, match="twice"):
        save_events(db, [post_event(1, 1), post_event(1, 1)], T0)
    assert load_events(db) == {}


# A crash half-way rolls the whole save back (one transaction).
def test_crash_rolls_back(db):
    save_events(db, [post_event(1, 1)], T0)

    # A covers rule that crashes, after the new event has already been written.
    def broken(_):
        raise RuntimeError("made-up crash")

    with pytest.raises(RuntimeError):
        save_events(db, [deadline_event(9, 3)], T0 + timedelta(hours=2), broken)
    assert "ntulearn_gradebook:_9_1" not in load_events(db)
    assert len(all_changes(db)) == 1


# Every stored row reads back as a valid Event (step 1's checks run again).
def test_rows_are_valid_events(db):
    save_events(db, [deadline_event(1, 3), post_event(2, 1, kind="message")], T0)
    events = [row_to_event(r) for r in db.execute("SELECT * FROM events")]
    assert {e.kind for e in events} == {"deadline", "message"}
    assert all(e.first_seen_at.tzinfo == timezone.utc for e in events)
