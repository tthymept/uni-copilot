"""
The two shapes the event store keeps: an Event and a Change.

In plain words:
  Event  = "one thing, as it looks now": a deadline, an announcement or a
           message, all in the same shape. One row in the `events` table.
  Change = "one line in the diary": something about an event changed
           (new, edited, moved, status changed, removed) and when we noticed.
           One row in the `event_changes` table.

Who fills which fields:
  - The source fields (title, due_at, status, content_hash, ...) come from an
    adapter (step 4) that turns a Phase 1 result into an Event.
  - The bookkeeping fields (first_seen_at, last_seen_at, modified_at,
    removed_at) are filled by the store itself (step 3). An adapter leaves them
    as None.

These are plain dataclasses, not pydantic models: they're internal hand-offs
between our own code (adapter -> store -> queries). What Claude sees is built
from them later, as pydantic models in pilot_mcp (step 8), like Phase 1 did.

Rules baked in here:
- Every time is timezone-aware UTC. A time without a timezone is a bug, so it's
  rejected straight away instead of being saved wrong.
- event_id = "<source>:<native id>", the dedupe key. Checked on creation.
- Status is only a word (open, submitted, graded, done). No grade field exists.
- Text from other people (title, body) is stored in full, as the light Markdown
  Phase 1's parse_html makes (links and [file: name] included). It's labelled
  UNTRUSTED wherever Claude sees it, exactly like the live tools. `preview` is
  a short one-line cut, only for compact lists; the full body is always there.
"""

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Literal

from uni_copilot.ntulearn_mcp.config import SGT

# Where an event came from. Phase 2 has three; later phases add more
# (exam_timetable, manual, ...). Deadlines use one source for both the calendar
# and the gradebook, because they share the column id (Phase 2 decision).
Source = Literal["ntulearn_gradebook", "ntulearn_announcement", "ntulearn_message"]

# What kind of thing it is. Later phases add test, exam, class, ...
Kind = Literal["deadline", "announcement", "message"]

# Where a deadline stands. Announcements and messages have no status (None).
# Mapping from Phase 1's SubmissionStatus is done by the adapter (step 4):
# not_submitted/unknown -> open, submitted -> submitted, graded -> graded,
# exempt -> done.
Status = Literal["open", "submitted", "graded", "done"]

# The five things the diary can say about an event.
ChangeType = Literal["new", "edited", "moved", "status_changed", "removed"]

# The two reminders a deadline can get (Phase 3): when it comes within 24 hours
# of being due, and within 2 hours. Saved in the notifications_sent table.
Reminder = Literal["24h", "2h"]

PREVIEW_MAX_CHARS = 280


# Raises an error if a time has no timezone or isn't UTC. Used by both shapes
# below so a wrong time is caught where it's created, not weeks later in a query.
def _check_utc(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be a UTC time with a timezone, got {value!r}")


# Cuts text down to the preview length, ending with "..." if anything was cut.
def shorten(text: str, max_chars: int = PREVIEW_MAX_CHARS) -> str:
    text = " ".join(text.split())  # one line, single spaces
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


# One deadline, announcement or message, in the shape every source shares.
# frozen=True: can't be changed after creation; make a changed copy with
# dataclasses.replace(event, status="graded") instead.
@dataclass(frozen=True)
class Event:
    # --- identity ---
    event_id: str  # "<source>:<native id>", e.g. "ntulearn_gradebook:_123_1"
    source: Source
    kind: Kind

    # --- what it is (from the source) ---
    course_code: str | None
    course_site_id: str | None
    title: str  # UNTRUSTED: written by someone else
    body: str = ""  # UNTRUSTED: the full post as light Markdown; "" for deadlines
    url: str | None = None

    # --- when (UTC) ---
    starts_at: datetime | None = None  # classes, tests (later phases)
    due_at: datetime | None = None  # deadlines
    posted_at: datetime | None = None  # announcements, messages

    # --- state ---
    status: Status | None = None  # deadlines only
    content_hash: str = ""  # fingerprint of the content (step 4)
    found_in: tuple[str, ...] = ()  # deadlines: ("calendar", "gradebook")

    # --- bookkeeping (filled by the store, step 3) ---
    first_seen_at: datetime | None = None  # first sync that saw it
    last_seen_at: datetime | None = None  # latest sync that saw it
    modified_at: datetime | None = None  # latest sync that recorded a change for it
    removed_at: datetime | None = None  # set when it disappeared from NTULearn

    # Runs right after the dataclass builds the object: checks the id and times.
    def __post_init__(self) -> None:
        prefix, sep, native_id = self.event_id.partition(":")
        if prefix != self.source or not sep or not native_id:
            raise ValueError(f"event_id must look like '{self.source}:<id>', got {self.event_id!r}")
        for f in fields(self):
            if f.name.endswith("_at"):
                _check_utc(f.name, getattr(self, f.name))

    # The id NTULearn uses, e.g. "_123_1" from "ntulearn_gradebook:_123_1".
    @property
    def native_id(self) -> str:
        return self.event_id.partition(":")[2]

    # A one-line cut of the body (~280 characters), for compact lists only,
    # e.g. a list of 20 announcements. Never the only text: `body` has it all.
    @property
    def preview(self) -> str:
        return shorten(self.body)

    # True while the event still exists on NTULearn.
    @property
    def is_active(self) -> bool:
        return self.removed_at is None


# One line in the diary: what changed about which event, and when we noticed.
# before/after are short texts for showing to you, e.g. "open" -> "submitted",
# or the old and new due times as ISO text. For "new" and "removed" they're None.
@dataclass(frozen=True)
class Change:
    event_id: str
    change_type: ChangeType
    changed_at: datetime  # time of the sync that found it (UTC)
    before: str | None = None
    after: str | None = None
    id: int | None = None  # row number, given by the database when saved

    # Checks the time is UTC, like Event does.
    def __post_init__(self) -> None:
        _check_utc("changed_at", self.changed_at)


# Self-test: uv run python -m uni_copilot.store.models
# Builds made-up events and changes and shows the checks working. No network,
# no database.
def _main() -> None:
    now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)  # 14:00 SGT

    deadline = Event(
        event_id="ntulearn_gradebook:_999_1",
        source="ntulearn_gradebook",
        kind="deadline",
        course_code="XX1000",
        course_site_id="_123_1",
        title="Made-up Assignment 1",
        due_at=datetime(2026, 11, 7, 15, 59, tzinfo=timezone.utc),
        status="open",
        content_hash="abc123",
        found_in=("calendar", "gradebook"),
    )
    announcement = Event(
        event_id="ntulearn_announcement:_888_1",
        source="ntulearn_announcement",
        kind="announcement",
        course_code="XX1000",
        course_site_id="_123_1",
        title="Made-up venue change",
        body="The quiz on Friday moves to **LT1**.\n\n- Bring your matric card.\n" * 10,
        posted_at=now,
        content_hash="def456",
    )
    print("1) Two events in the same shape:")
    for e in (deadline, announcement):
        print(f"   {e.event_id}  kind={e.kind}  native_id={e.native_id}  status={e.status}  active={e.is_active}")
    print(f"   body is kept in full: {len(announcement.body)} chars, {announcement.body.count(chr(10))} lines")
    print(f"   preview (lists only): {len(announcement.preview)} chars, ends with {announcement.preview[-10:]!r}")

    print("\n2) Frozen: a change makes a new copy, the old one stays as it was:")
    submitted = replace(deadline, status="submitted")
    print(f"   old: {deadline.status}   new copy: {submitted.status}")

    print("\n3) A diary line for that change:")
    change = Change(deadline.event_id, "status_changed", now, before="open", after="submitted")
    print(f"   {change}")

    print("\n4) Bad data is rejected:")
    bad = [
        ("time with no timezone", lambda: replace(deadline, due_at=datetime(2026, 11, 7, 15, 59))),
        ("id with the wrong source", lambda: replace(deadline, event_id="ntulearn_message:_999_1")),
        ("id with no native id", lambda: replace(deadline, event_id="ntulearn_gradebook:")),
        ("time in SGT instead of UTC", lambda: replace(announcement, posted_at=now.astimezone(SGT))),
    ]
    for label, make in bad:
        try:
            make()
            print(f"   NOT rejected: {label}")
        except ValueError as e:
            print(f"   rejected ({label}): {e}")


if __name__ == "__main__":
    _main()
