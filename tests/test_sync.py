"""
Offline tests for store/sync.py: the safety rules about "removed" (failed
courses, failed sources, items leaving the window), keeping a known status,
and a whole sync run against fake Phase 1 clients. These are the failure
cases we can't try live: we don't break NTULearn on purpose.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from conftest import T0, deadline_event, post_event
from uni_copilot.ntulearn_mcp.auth.session import LoginRequired
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError
from uni_copilot.ntulearn_mcp.models import (
    Announcement, AnnouncementList, Deadline, DeadlineList, Message, MessageList,
)
from uni_copilot.store.db import get_state, time_to_db
from uni_copilot.store.events import load_events
from uni_copilot.store.sync import (
    EDGE_MARGIN, Clients, failed_courses, is_failure, keep_known_status, make_covers, run_sync,
)

CODES = {"XX1001", "YY2002"}


# ---------- pure rules ----------

# Only "unavailable" / "couldn't be read" notes are failures.
def test_is_failure():
    assert is_failure("XX1001 gradebook unavailable on site _10_1 (403); items only in its gradebook may be missing.")
    assert is_failure("XX1001: one conversation couldn't be read (500).")
    assert not is_failure("3 item(s) from sites outside this term's courses were left out.")


# A failure about one course -> that course; about no known course -> None (whole source).
def test_failed_courses():
    assert failed_courses([], CODES) == set()
    assert failed_courses(["XX1001 announcements unavailable on site _10_1 (500)."], CODES) == {"XX1001"}
    assert failed_courses(["Calendar unavailable (503); using the gradebook only."], CODES) is None
    assert failed_courses(["2 item(s) from sites outside this term's courses were left out."], CODES) == set()
    # The longest matching code wins.
    assert failed_courses(["XX1001A messages unavailable on site _1_1 (500)."], CODES | {"XX1001A"}) == {"XX1001A"}


# covers: own source, current course, not failed, inside the window (with margin).
def test_make_covers():
    since, until = T0 - timedelta(days=30), T0
    covers = make_covers("ntulearn_announcement", "posted_at", since, until, CODES, {"YY2002"})
    assert covers(post_event(1, 5))  # inside, XX1001
    assert not covers(post_event(2, 5, course="YY2002"))  # course failed this time
    assert not covers(post_event(3, 5, course="ZZ3003"))  # not a current course
    assert not covers(post_event(4, 5, kind="message"))  # another source
    assert not covers(post_event(5, 31))  # left the window
    edge = (T0 - since - EDGE_MARGIN / 2).total_seconds() / 86400
    assert not covers(post_event(6, edge))  # within the margin of the edge
    assert make_covers("ntulearn_announcement", "posted_at", since, until, CODES, None) is None


# A deadline whose status came back "unknown" keeps the status already stored.
def test_keep_known_status():
    stored = {"ntulearn_gradebook:_1_1": deadline_event(1, 3, "submitted")}
    fetched = [deadline_event(1, 3, "open"), deadline_event(2, 3, "open")]
    kept = keep_known_status(fetched, {"ntulearn_gradebook:_1_1", "ntulearn_gradebook:_2_1"}, stored)
    assert [e.status for e in kept] == ["submitted", "open"]  # _2_1 was never stored: stays open


# ---------- a whole sync against fake clients ----------

# Fake Phase 1 clients. Each source's answer is set per test; an Exception
# instance is raised instead of answered.
@dataclass
class Fake:
    deadlines: list = field(default_factory=list)
    announcements: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)  # source name -> notes
    fail: dict = field(default_factory=dict)  # source name -> exception
    sign_in_error: Exception | None = None

    # Builds a Clients object whose methods answer from this Fake.
    def clients(self) -> Clients:
        fake = self

        # Raises the error set for `name`, if any.
        def check(name):
            if name in fake.fail:
                raise fake.fail[name]

        # Stands in for http.ensure_signed_in.
        def ensure_signed_in():
            if fake.sign_in_error:
                raise fake.sign_in_error

        # Stands in for DeadlinesClient.upcoming.
        def upcoming(days, past_days=0, course=None):
            check("deadlines")
            return DeadlineList(days=days, deadlines=fake.deadlines, notes=fake.notes.get("deadlines", []))

        # Stands in for AnnouncementsClient.recent.
        def recent_announcements(since_days, course=None):
            check("announcements")
            return AnnouncementList(since_days=since_days, announcements=fake.announcements,
                                    notes=fake.notes.get("announcements", []))

        # Stands in for MessagesClient.recent.
        def recent_messages(since_days, course=None):
            check("messages")
            return MessageList(since_days=since_days, messages=fake.messages, notes=fake.notes.get("messages", []))

        courses = SimpleNamespace(list_courses=lambda: SimpleNamespace(
            courses=[SimpleNamespace(code=c) for c in sorted(CODES)]))
        return Clients(
            http=SimpleNamespace(ensure_signed_in=ensure_signed_in),
            courses=courses,
            deadlines=SimpleNamespace(upcoming=upcoming),
            announcements=SimpleNamespace(recent=recent_announcements),
            messages=SimpleNamespace(recent=recent_messages),
        )


# A made-up Phase 1 Deadline, due `days` after T0.
def p1_deadline(n, days, status="not_submitted", course="XX1001"):
    return Deadline(column_id=f"_{n}_1", course_code=course, course_site_id="_10_1", title=f"Lab {n}",
                    due_at=T0 + timedelta(days=days), status=status, sources=["calendar"])


# A made-up Phase 1 Announcement, posted `days_ago` before T0.
def p1_announcement(n, days_ago, course="XX1001"):
    return Announcement(id=f"_{n}_1", course_code=course, course_site_ids=["_10_1"], title=f"News {n}",
                        body="Body.", created_at=T0 - timedelta(days=days_ago))


# A made-up Phase 1 Message, posted `days_ago` before T0.
def p1_message(n, days_ago):
    return Message(id=f"_{n}_1", conversation_id="_c_1", course_code="XX1001", course_site_id="_10_1",
                   sender_name="Made-up Prof", body="Hi.", posted_at=T0 - timedelta(days=days_ago))


# A Fake with one of everything.
def full_fake() -> Fake:
    return Fake(deadlines=[p1_deadline(1, 3)], announcements=[p1_announcement(2, 1)], messages=[p1_message(3, 1)])


# Counts of each change type in one sync's reports, all sources together.
def counts(reports):
    total: dict[str, int] = {}
    for r in reports:
        for k, n in r.counts.items():
            total[k] = total.get(k, 0) + n
    return total


# Two identical syncs: the second reports no changes (the Phase 2 "done" test).
def test_two_identical_syncs(db):
    fake = full_fake()
    first = run_sync(db, fake.clients(), T0)
    second = run_sync(db, fake.clients(), T0 + timedelta(hours=2))
    assert counts(first) == {"new": 3}
    assert counts(second) == {} and all(r.ok and r.removals_checked for r in second)
    assert get_state(db, "last_sync:ntulearn_message") == time_to_db(T0 + timedelta(hours=2))
    assert get_state(db, "last_error:ntulearn_message") == ""


# A post that's gone (nothing failed) is removed.
def test_gone_post_is_removed(db):
    fake = full_fake()
    run_sync(db, fake.clients(), T0)
    fake.announcements = []
    assert counts(run_sync(db, fake.clients(), T0 + timedelta(hours=2))) == {"removed": 1}


# A post missing because its course failed this time is kept.
def test_failed_course_keeps_items(db):
    fake = full_fake()
    run_sync(db, fake.clients(), T0)
    fake.announcements = []
    fake.notes["announcements"] = ["XX1001 announcements unavailable on site _10_1 (500)."]
    reports = run_sync(db, fake.clients(), T0 + timedelta(hours=2))
    assert counts(reports) == {}
    assert load_events(db)["ntulearn_announcement:_2_1"].is_active
    assert get_state(db, "last_notes:ntulearn_announcement").startswith("XX1001")


# The calendar failing (no single course) means no removals at all for deadlines.
def test_failed_calendar_skips_removals(db):
    fake = full_fake()
    run_sync(db, fake.clients(), T0)
    fake.deadlines = []
    fake.notes["deadlines"] = ["Calendar unavailable (503); using the gradebook only."]
    reports = run_sync(db, fake.clients(), T0 + timedelta(hours=2))
    deadlines = reports[0]
    assert deadlines.ok and not deadlines.removals_checked and deadlines.counts == {}
    assert load_events(db)["ntulearn_gradebook:_1_1"].is_active


# An item that only left the time window isn't removed.
def test_window_exit_is_not_removal(db):
    fake = Fake(announcements=[p1_announcement(2, 29.9)])
    run_sync(db, fake.clients(), T0)
    fake.announcements = []  # a day later it's 30.9 days old, so not fetched
    assert counts(run_sync(db, fake.clients(), T0 + timedelta(days=1))) == {}
    assert load_events(db)["ntulearn_announcement:_2_1"].is_active


# One source raising doesn't stop the others; its error and old data are kept.
def test_one_source_fails(db):
    fake = full_fake()
    run_sync(db, fake.clients(), T0)
    fake.fail["messages"] = NtuLearnError("/x", 500)
    later = T0 + timedelta(hours=2)
    reports = run_sync(db, fake.clients(), later)
    assert [r.ok for r in reports] == [True, True, False]
    assert "NtuLearnError" in reports[2].error
    assert get_state(db, "last_sync:ntulearn_message") == time_to_db(T0)  # "as of" stays at the last success
    assert get_state(db, "last_sync:ntulearn_gradebook") == time_to_db(later)
    assert "NtuLearnError" in get_state(db, "last_error:ntulearn_message")
    assert load_events(db)["ntulearn_message:_3_1"].is_active


# Not signed in: every source fails the same way and nothing is saved.
def test_sign_in_failure(db):
    fake = full_fake()
    fake.sign_in_error = LoginRequired("no session")
    reports = run_sync(db, fake.clients(), T0)
    assert all(not r.ok and "LoginRequired" in r.error for r in reports)
    assert load_events(db) == {}
    assert get_state(db, "last_sync:ntulearn_gradebook") is None


# A status that couldn't be read ("unknown") doesn't undo a known "submitted".
def test_unknown_status_keeps_known(db):
    fake = Fake(deadlines=[p1_deadline(1, 3, "submitted")])
    run_sync(db, fake.clients(), T0)
    fake.deadlines = [p1_deadline(1, 3, "unknown")]
    assert counts(run_sync(db, fake.clients(), T0 + timedelta(hours=2))) == {}
    assert load_events(db)["ntulearn_gradebook:_1_1"].status == "submitted"


# A real status change still shows up.
def test_real_status_change(db):
    fake = Fake(deadlines=[p1_deadline(1, 3)])
    run_sync(db, fake.clients(), T0)
    fake.deadlines = [p1_deadline(1, 3, "submitted")]
    assert counts(run_sync(db, fake.clients(), T0 + timedelta(hours=2))) == {"status_changed": 1}


# A sync stores each source under its own "<source>:<native id>" ids.
def test_event_ids(db):
    run_sync(db, full_fake().clients(), T0)
    assert set(load_events(db)) == {"ntulearn_gradebook:_1_1", "ntulearn_announcement:_2_1", "ntulearn_message:_3_1"}
