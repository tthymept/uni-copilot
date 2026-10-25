"""
Adapters: Phase 1's NTULearn results -> store Events.

In plain words:
  Phase 1 gives us three different shapes (Deadline, Announcement, Message).
  The store wants one shape (Event). An adapter is a small function that
  copies the fields across, like a travel plug adapter: same electricity,
  different socket.

    Deadline     -> "ntulearn_gradebook:<column id>"    kind=deadline
    Announcement -> "ntulearn_announcement:<id>"        kind=announcement
    Message      -> "ntulearn_message:<id>"             kind=message

content_hash = a fingerprint of the fields that count as "the content".
  Same content -> same hash, every time. Any edit -> a different hash.
  store/events.py compares old and new hashes to spot "edited".
  What's IN the hash and what's deliberately LEFT OUT:

    Deadline:     title                 (not due_at/status: they have their own
                                         change types, moved/status_changed;
                                         not found_in: the calendar sometimes
                                         misses an item, that's not an edit)
    Announcement: title + body          (body = Markdown, so links and
                                         [file: name] are included;
                                         not which sites it was found on)
    Message:      sender name + body    (not is_read: it flips when you open it)

  If a field that changes by itself got into the hash, every sync would
  report fake edits. That's checked live in steps 5-6 (two syncs in a row).

Nothing here talks to NTULearn or the database: pure functions, easy to test.
"""

import argparse
import hashlib
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone

from uni_copilot.ntulearn_mcp.models import Announcement, Deadline, Message, SubmissionStatus, course_url
from uni_copilot.store.models import Event, Status

# Phase 1's submission status -> the store's status word (Phase 2 decision).
# unknown -> open: if we can't tell, it's safer to keep it on your to-do list.
STATUS_FROM_SUBMISSION: dict[SubmissionStatus, Status] = {
    "not_submitted": "open",
    "unknown": "open",
    "submitted": "submitted",
    "graded": "graded",
    "exempt": "done",
}

# Put between the parts before hashing, so ("ab", "c") and ("a", "bc") give
# different hashes. It's an invisible control character never found in posts.
_SEPARATOR = "\x1f"


# The fingerprint: SHA-256 of the parts joined together, as 64 hex characters.
# None counts as empty text.
def content_hash(*parts: str | None) -> str:
    joined = _SEPARATOR.join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# One Phase 1 Deadline -> an Event. The column id is shared by the calendar and
# the gradebook, so one event per deadline whichever list(s) it came from.
def deadline_to_event(d: Deadline) -> Event:
    return Event(
        event_id=f"ntulearn_gradebook:{d.column_id}",
        source="ntulearn_gradebook",
        kind="deadline",
        course_code=d.course_code,
        course_site_id=d.course_site_id,
        title=d.title,
        url=d.url,
        due_at=d.due_at,
        status=STATUS_FROM_SUBMISSION[d.status],
        content_hash=content_hash(d.title),
        found_in=tuple(sorted(d.sources)),
    )


# One Phase 1 Announcement -> an Event. A post merged from several sites keeps
# the first site as its home (for the link).
def announcement_to_event(a: Announcement) -> Event:
    site = a.course_site_ids[0] if a.course_site_ids else None
    return Event(
        event_id=f"ntulearn_announcement:{a.id}",
        source="ntulearn_announcement",
        kind="announcement",
        course_code=a.course_code,
        course_site_id=site,
        title=a.title,
        body=a.body,
        url=course_url(site) if site else None,
        posted_at=a.created_at,
        content_hash=content_hash(a.title, a.body),
    )


# What a message is called in lists, since messages have no title of their own.
def message_title(m: Message) -> str:
    if m.sent_by_me:
        return "Message from you"
    return f"Message from {m.sender_name}" if m.sender_name else "Message"


# One Phase 1 Message -> an Event.
def message_to_event(m: Message) -> Event:
    return Event(
        event_id=f"ntulearn_message:{m.id}",
        source="ntulearn_message",
        kind="message",
        course_code=m.course_code,
        course_site_id=m.course_site_id,
        title=message_title(m),
        body=m.body,
        url=course_url(m.course_site_id),
        posted_at=m.posted_at,
        content_hash=content_hash(m.sender_name, m.body),
    )


# Converts a whole list. Lists keep Phase 1's order.
def deadlines_to_events(items: Iterable[Deadline]) -> list[Event]:
    return [deadline_to_event(d) for d in items]


# Converts a whole list of announcements.
def announcements_to_events(items: Iterable[Announcement]) -> list[Event]:
    return [announcement_to_event(a) for a in items]


# Converts a whole list of messages.
def messages_to_events(items: Iterable[Message]) -> list[Event]:
    return [message_to_event(m) for m in items]


# Self-test (offline): uv run python -m uni_copilot.sources.ntulearn
#            (live):    uv run python -m uni_copilot.sources.ntulearn --live
# Offline builds made-up Phase 1 results. --live fetches yours (GET only) and
# prints counts only: events per kind, statuses, duplicate ids.
def _main() -> None:
    parser = argparse.ArgumentParser(description="Self-test of the NTULearn adapters.")
    parser.add_argument("--live", action="store_true", help="convert your real NTULearn data (GET only, counts only)")
    args = parser.parse_args()
    if args.live:
        _live()
        return

    posted = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
    d = Deadline(
        column_id="_999_1", course_code="XX1000", course_site_id="_123_1", title="Made-up Assignment 1",
        due_at="2026-11-07T15:59:00Z", status="not_submitted", sources=["gradebook", "calendar"],
    )
    a = Announcement(
        id="_888_1", course_code="XX1000", course_site_ids=["_123_1", "_124_1"], title="Made-up quiz venue",
        body="Quiz 2 is in **LT1**.\n\n- Bring your matric card.\n- [file: seating.pdf]", created_at=posted,
    )
    m = Message(
        id="_777_1", conversation_id="_5_1", course_code="XX1000", course_site_id="_123_1",
        sender_name="Made-up Prof", body="See you at the quiz.", posted_at=posted, is_read=False,
    )

    print("1) Three Phase 1 shapes -> one Event shape:")
    for e in (deadline_to_event(d), announcement_to_event(a), message_to_event(m)):
        print(f"   {e.event_id:<32} kind={e.kind:<12} status={e.status!s:<5} title={e.title!r}")
        print(f"   {'':<32} hash={e.content_hash[:16]}...  found_in={e.found_in}")

    print("\n2) Status mapping (Phase 1 -> store):")
    for before, after in STATUS_FROM_SUBMISSION.items():
        print(f"   {before:<14} -> {after}")

    print("\n3) What changes the hash (True = would be reported as 'edited'):")
    base = announcement_to_event(a).content_hash
    checks = [
        ("same post fetched again", a),
        ("one word of the body changed", a.model_copy(update={"body": a.body.replace("LT1", "LT2")})),
        ("title changed", a.model_copy(update={"title": "Made-up quiz venue (updated)"})),
        ("found on a third site too", a.model_copy(update={"course_site_ids": [*a.course_site_ids, "_125_1"]})),
    ]
    for label, variant in checks:
        print(f"   {label:<30} {announcement_to_event(variant).content_hash != base}")
    msg_base = message_to_event(m).content_hash
    print(f"   {'message opened (is_read)':<30} {message_to_event(m.model_copy(update={'is_read': True})).content_hash != msg_base}")
    dl_base = deadline_to_event(d).content_hash
    print(f"   {'deadline submitted':<30} {deadline_to_event(d.model_copy(update={'status': 'submitted'})).content_hash != dl_base}"
          "   (reported as status_changed instead)")

    print("\n4) Parts are kept apart: hash('ab','c') == hash('a','bc')?", content_hash("ab", "c") == content_hash("a", "bc"))


# The --live part: fetch, convert, count. Never prints titles, names or bodies.
def _live() -> None:
    import logging

    from uni_copilot.ntulearn_mcp.client.announcements import AnnouncementsClient
    from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
    from uni_copilot.ntulearn_mcp.client.deadlines import DeadlinesClient
    from uni_copilot.ntulearn_mcp.client.http import NtuLearnHttp
    from uni_copilot.ntulearn_mcp.client.messages import MessagesClient
    from uni_copilot.ntulearn_mcp.config import load_settings
    from uni_copilot.ntulearn_mcp.parsers.html import TRUNCATED

    logging.basicConfig(level=logging.WARNING)
    http = NtuLearnHttp()
    http.ensure_signed_in()
    courses = CoursesClient(http, load_settings())
    print("Fetching (about 1 minute at 1 request/s)...")
    events = (
        deadlines_to_events(DeadlinesClient(http, courses).upcoming(days=120).deadlines)
        + announcements_to_events(AnnouncementsClient(http, courses).recent(since_days=30).announcements)
        + messages_to_events(MessagesClient(http, courses).recent(since_days=30).messages)
    )
    ids = Counter(e.event_id for e in events)
    print(f"events per kind: {dict(Counter(e.kind for e in events))}")
    print(f"deadline statuses: {dict(Counter(e.status for e in events if e.kind == 'deadline'))}")
    print(f"duplicate event_ids: {sum(1 for n in ids.values() if n > 1)}")
    print(f"longest body: {max((len(e.body) for e in events), default=0)} characters, "
          f"cut by the safety limit: {sum(e.body.endswith(TRUNCATED) for e in events)}")


if __name__ == "__main__":
    _main()
