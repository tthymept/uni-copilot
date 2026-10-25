"""
Offline tests for store/queries.py: what's listed, what counts as new (and the
first-sync import that doesn't), bookmarks per channel, and the "as of" time.
"""

from datetime import timedelta

from conftest import T0, deadline_event, post_event
from uni_copilot.store.db import set_state, time_to_db
from uni_copilot.store.events import save_events
from uni_copilot.store.queries import (
    SOURCES, freshness, get_event, last_seen, list_events, mark_seen, whats_new,
)

T2 = T0 + timedelta(hours=2)


# Marks every source as synced at `when` (what sync.py writes).
def synced(db, when):
    for s in SOURCES:
        set_state(db, f"last_sync:{s}", time_to_db(when))
    db.commit()


# First sync at T0, then at T2 a new post appears and Assignment 1 gets submitted.
def two_syncs(db):
    save_events(db, [deadline_event(1, 3), deadline_event(2, 20), post_event(7, 2)], T0)
    synced(db, T0)
    save_events(db, [deadline_event(1, 3, "submitted"), deadline_event(2, 20), post_event(7, 2),
                     post_event(8, -0.04)], T2)
    synced(db, T2)


# Deadlines due in the window, posts from the window, removed ones left out.
def test_list_events_windows(db):
    save_events(db, [deadline_event(1, 3), deadline_event(2, 10), deadline_event(3, -1),
                     post_event(4, 2), post_event(5, 10), post_event(6, 1)], T0)
    save_events(db, [], T0, covers=lambda e: e.event_id == "ntulearn_announcement:_6_1")  # _6_1 removed
    ids = [e.event_id for e in list_events(db, days=7, now=T0)]
    assert ids == ["ntulearn_gradebook:_1_1", "ntulearn_announcement:_4_1"]
    assert len(list_events(db, days=14, now=T0)) == 4


# Filters: kinds, and part of a course code in any case.
def test_list_events_filters(db):
    save_events(db, [deadline_event(1, 3), deadline_event(2, 3, course="YY2002"), post_event(3, 1)], T0)
    assert [e.kind for e in list_events(db, kinds=["announcement"], now=T0)] == ["announcement"]
    assert [e.course_code for e in list_events(db, kinds=["deadline"], course="yy2", now=T0)] == ["YY2002"]
    assert list_events(db, course="ZZ", now=T0) == []


# Deadlines soonest first, then posts newest first.
def test_list_events_order(db):
    save_events(db, [deadline_event(1, 5), deadline_event(2, 1), post_event(3, 3), post_event(4, 1)], T0)
    assert [e.event_id.split(":")[1] for e in list_events(db, now=T0)] == ["_2_1", "_1_1", "_4_1", "_3_1"]


# whats_new: changes after the bookmark, without the first-sync import.
def test_whats_new_after_bookmark(db):
    two_syncs(db)
    mark_seen(db, "terminal", T0 + timedelta(hours=1))
    new = whats_new(db, "terminal", now=T2 + timedelta(hours=1))
    assert [(c.change.change_type, c.event.title) for c in new.changes] == [
        ("status_changed", "Made-up Assignment 1"), ("new", "Made-up post 8")]
    assert new.since == T0 + timedelta(hours=1) and new.freshness.as_of == T2


# A channel that never looked sees the last 7 days, still without the first-sync import.
def test_whats_new_first_look(db):
    two_syncs(db)
    new = whats_new(db, "telegram", now=T2 + timedelta(hours=1))
    assert len(new.changes) == 2
    assert new.since == T2 + timedelta(hours=1) - timedelta(days=7)


# whats_new only reads; mark_seen moves one channel's bookmark, not the other's.
def test_bookmarks(db):
    two_syncs(db)
    later = T2 + timedelta(hours=1)
    whats_new(db, "terminal", now=later)
    assert last_seen(db, "terminal") is None
    mark_seen(db, "terminal", later)
    assert whats_new(db, "terminal", now=later).changes == []
    assert len(whats_new(db, "telegram", now=later).changes) == 2


# as_of is the oldest source's last sync; None if one never synced; errors listed.
def test_freshness(db):
    assert freshness(db).as_of is None
    synced(db, T2)
    set_state(db, "last_sync:ntulearn_message", time_to_db(T0))
    set_state(db, "last_error:ntulearn_message", "NtuLearnError: made up")
    f = freshness(db)
    assert f.as_of == T0 and list(f.errors) == ["ntulearn_message"]


# get_event: the full item, or None.
def test_get_event(db):
    save_events(db, [post_event(1, 1, body="Full text here.")], T0)
    assert get_event(db, "ntulearn_announcement:_1_1").body == "Full text here."
    assert get_event(db, "ntulearn_announcement:_nope") is None
