"""
Typed shapes for what the pilot-mcp tools return.

Same idea as Phase 1's ntulearn_mcp/models.py: pydantic models the SDK turns
into an output schema, so Claude knows each field and its description.
The store's own shapes (store/models.py) are plain dataclasses for our code;
these are the versions made for Claude:
- every time has a ready-made SGT text next to the UTC value,
- text written by other people is labelled UNTRUSTED,
- every result carries `as_of`: how fresh the store is. Claude must say it.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, computed_field

from uni_copilot.ntulearn_mcp.models import BODY_DESCRIPTION, UNTRUSTED, UtcDatetime, sgt_text
from uni_copilot.store.db import time_from_db
from uni_copilot.store.models import Event
from uni_copilot.store.queries import EventChange, Freshness

# Friendly names for the sources, used in sync_status and failure lists.
SOURCE_LABELS = {
    "ntulearn_gradebook": "deadlines",
    "ntulearn_announcement": "announcements",
    "ntulearn_message": "messages",
}


# Base for every model below: frozen, and the schema describes the JSON as sent
# (with computed fields like due_sgt), exactly like Phase 1's _Model.
class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_mode_override="serialization")


# ---------- Freshness ("as of") ----------

# How fresh the store is. Built from store.queries.Freshness.
class AsOf(_Model):
    as_of_at: UtcDatetime | None = Field(
        description="Oldest last-sync time of the sources (UTC). None if a source never synced."
    )
    failed_sources: list[str] = Field(
        default_factory=list, description="Sources whose last sync failed; their data is older."
    )

    # The "as of" time in Singapore time, for saying in the answer.
    @computed_field
    @property
    def as_of_sgt(self) -> str | None:
        return sgt_text(self.as_of_at) if self.as_of_at else None

    # How many minutes old the data is.
    @computed_field
    @property
    def minutes_old(self) -> int | None:
        if self.as_of_at is None:
            return None
        return int((datetime.now(timezone.utc) - self.as_of_at).total_seconds() // 60)


# Turns the store's Freshness into the AsOf model.
def as_of_from(f: Freshness) -> AsOf:
    return AsOf(as_of_at=f.as_of, failed_sources=[SOURCE_LABELS.get(s, s) for s in f.errors])


# ---------- Events ----------

# One deadline, announcement or message from the store. In lists, `body` is
# None and `preview` gives the first ~280 characters; get_event and
# whats_new include the full body.
class EventItem(_Model):
    event_id: str = Field(description="Pass to get_event for the full item.")
    kind: str = Field(description="deadline, announcement or message")
    course_code: str | None
    title: str = Field(description=UNTRUSTED)
    status: str | None = Field(
        default=None,
        description="Deadlines only: open (not handed in on NTULearn), submitted, graded, done (exempt).",
    )
    due_at: UtcDatetime | None = None
    posted_at: UtcDatetime | None = None
    url: str | None = None
    preview: str | None = Field(default=None, description=UNTRUSTED + " First ~280 characters of the body.")
    body: str | None = Field(default=None, description=BODY_DESCRIPTION + " None in lists: use get_event.")
    removed: bool = Field(default=False, description="True if it has disappeared from NTULearn.")
    marked_done_at: UtcDatetime | None = Field(
        default=None,
        description="Set when the USER marked this deadline done (handed in outside NTULearn, e.g. Gradescope). "
                    "Treat it as done: not a to-do, no reminders. `status` still shows what NTULearn says.",
    )

    # When the user marked it done, in Singapore time.
    @computed_field
    @property
    def marked_done_sgt(self) -> str | None:
        return sgt_text(self.marked_done_at) if self.marked_done_at else None

    # Due time in Singapore time.
    @computed_field
    @property
    def due_sgt(self) -> str | None:
        return sgt_text(self.due_at) if self.due_at else None

    # Posting time in Singapore time.
    @computed_field
    @property
    def posted_sgt(self) -> str | None:
        return sgt_text(self.posted_at) if self.posted_at else None


# A store Event -> the EventItem Claude sees. full=True includes the whole body.
# marked_done_at: when the user marked it done (from the marked_done table), if they did.
def event_item(e: Event, full: bool = False, marked_done_at: datetime | None = None) -> EventItem:
    return EventItem(
        event_id=e.event_id,
        kind=e.kind,
        course_code=e.course_code,
        title=e.title,
        status=e.status,
        due_at=e.due_at,
        posted_at=e.posted_at,
        url=e.url,
        preview=e.preview or None,
        body=(e.body or None) if full else None,
        removed=not e.is_active,
        marked_done_at=marked_done_at,
    )


class EventList(_Model):
    as_of: AsOf
    days: int = Field(description="Deadlines due in the next `days` days; posts from the last `days` days.")
    events: list[EventItem]


class EventResult(_Model):
    as_of: AsOf
    event: EventItem


# ---------- Changes ----------

# One line of the diary: what changed about which event, and when the sync found it.
class ChangeItem(_Model):
    change_type: str = Field(description="new, edited, moved, status_changed or removed")
    found_at: UtcDatetime = Field(description="When the sync noticed it (not when it happened on NTULearn).")
    before: str | None = Field(default=None, description="moved: old due time (SGT); status_changed: old status; edited: old title if it changed.")
    after: str | None = Field(default=None, description="The new value, same rules as `before`.")
    event: EventItem

    # When it was found, in Singapore time.
    @computed_field
    @property
    def found_sgt(self) -> str:
        return sgt_text(self.found_at)


# A diary line + its event -> the ChangeItem Claude sees. Moved times are
# turned into SGT text; posts carry their full body (that's what's new).
def change_item(item: EventChange, marked_done_at: datetime | None = None) -> ChangeItem:
    c = item.change
    before, after = c.before, c.after
    if c.change_type == "moved":
        before = sgt_text(time_from_db(before)) if before else None
        after = sgt_text(time_from_db(after)) if after else None
    return ChangeItem(
        change_type=c.change_type,
        found_at=c.changed_at,
        before=before,
        after=after,
        event=event_item(item.event, full=item.event.kind != "deadline", marked_done_at=marked_done_at),
    )


class WhatsNew(_Model):
    as_of: AsOf
    channel: str
    since_at: UtcDatetime = Field(description="The channel's bookmark: changes found after this are listed.")
    checked_at: UtcDatetime = Field(description="Pass this to mark_seen as up_to after showing the changes.")
    changes: list[ChangeItem]

    # The bookmark in Singapore time.
    @computed_field
    @property
    def since_sgt(self) -> str:
        return sgt_text(self.since_at)


# ---------- Sync status and bookmark ----------

# How one source's last sync went.
class SourceStatus(_Model):
    source: str
    last_sync_at: UtcDatetime | None = Field(description="Last successful sync (UTC).")
    last_error: str | None = Field(default=None, description="Why the last try failed; None if it worked.")
    notes: list[str] = Field(default_factory=list, description="Courses that couldn't be read last time.")
    active_events: int

    # Last successful sync in Singapore time.
    @computed_field
    @property
    def last_sync_sgt(self) -> str | None:
        return sgt_text(self.last_sync_at) if self.last_sync_at else None


class SyncStatus(_Model):
    as_of: AsOf
    sources: list[SourceStatus]
    how_to_refresh: str = Field(
        default="The store is filled by `uv run uni-copilot sync` (scheduled, or run by the user). "
                "This server never fetches from NTULearn; the live ntulearn tools do.",
    )


class Seen(_Model):
    channel: str
    last_seen_at: UtcDatetime

    # The new bookmark in Singapore time.
    @computed_field
    @property
    def last_seen_sgt(self) -> str:
        return sgt_text(self.last_seen_at)


# ---------- Phase 3: marked done, Telegram ----------

# What mark_done did: the deadline as it is now, whether it's marked done now,
# and whether this call changed anything (False = it already was that way).
class DoneMark(_Model):
    event: EventItem
    marked_done: bool = Field(description="True if the deadline is now marked done by the user.")
    changed: bool = Field(description="False if it was already in that state (nothing changed).")


# What send_telegram did: how many messages it took and when.
class TelegramSent(_Model):
    parts: int = Field(description="Number of Telegram messages (long text is split).")
    sent_at: UtcDatetime
    note: str = Field(default="Sent as plain text to the user's own Telegram chat (the chat id in .env).")

    # When it was sent, in Singapore time.
    @computed_field
    @property
    def sent_sgt(self) -> str:
        return sgt_text(self.sent_at)
