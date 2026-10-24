"""
Recent course Messages (NTULearn "Messages", not announcements).

In plain words:
  Messages = conversations inside a course site, like a small inbox per
  course. Some profs use them instead of announcements. Each site has a list
  of conversations; each conversation has a thread of messages.
  - The conversation list only shows the NEWEST message of each thread (a
    preview). That's enough to tell whether a thread had activity recently.
  - So this asks every site for its list (one call each), and fetches the
    full thread only for conversations active in the last N days, then keeps
    the messages from that window, newest first.

Verified facts this builds on (plan, Appendix A; checked 24 Oct):
- GET /learn/api/v1/courses/{id}/conversations (internal, undocumented):
  200 on all 16 sites, 9 conversations. Each has `messages` with exactly one
  item, the newest; `totalCount` is the real thread length (25 in total).
- GET /learn/api/v1/courses/{id}/conversations/{conversationId}/messages:
  the full thread, oldest first, paged like other lists.
- Message: `id`, `postDate`, `isRead`, `senderId`, `sender` (givenName,
  familyName, title, userName, and preferredDisplayName, which is a display
  SETTING like "GIVEN_NAME", not a name), `type` (NORMAL), and `body` as an
  object whose `displayText` and `rawText` are both HTML. They gave the same
  text after parsing for all 25 messages; displayText is used.

Not verified: whether reading a thread through the API marks it as read
(you had 0 unread when tested). These are GET requests, so it shouldn't.

Untrusted: bodies and sender names are written by other people. They're
labelled UNTRUSTED in the models; this code never acts on what they say.
"""

import argparse
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from uni_copilot.ntulearn_mcp.auth.session import AuthError
from uni_copilot.ntulearn_mcp.client.calendar import parse_time
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError, NtuLearnHttp
from uni_copilot.ntulearn_mcp.models import Link, Message, MessageList
from uni_copilot.ntulearn_mcp.parsers.html import parse_html

log = logging.getLogger(__name__)

MAX_DAYS = 180
# Longest body passed to Claude per message; longer ones are cut and marked.
BODY_MAX_CHARS = 4000


# Fetches course messages for the tool. No cache, like announcements.
class MessagesClient:
    # Keeps the shared HTTP and courses clients.
    def __init__(self, http: NtuLearnHttp, courses: CoursesClient):
        self._http = http
        self._courses = courses

    # The main entry point: messages posted in the last `since_days` days, for
    # this term's courses (or one, e.g. "IE2106"), newest first. A site or
    # thread that fails becomes a note; the rest still come back.
    def recent(self, since_days: int = 14, course: str | None = None) -> MessageList:
        if not 1 <= since_days <= MAX_DAYS:
            raise ValueError(f"since_days must be between 1 and {MAX_DAYS}")
        subjects = self._courses.pick(course)
        me = self._courses.user_id()
        since = datetime.now(timezone.utc) - timedelta(days=since_days)
        messages: list[Message] = []
        notes: list[str] = []
        for subject in subjects:
            for site in subject.sites:
                base = f"/learn/api/v1/courses/{site.id}/conversations"
                try:
                    conversations = self._http.get_all(base, {"limit": 100})
                except NtuLearnError as e:
                    notes.append(f"{subject.code} messages unavailable on site {site.id} ({e.status}).")
                    continue
                for conv in conversations:
                    if not active_since(conv, since):
                        continue
                    try:
                        thread = self._http.get_all(f"{base}/{conv['id']}/messages", {"limit": 100})
                    except NtuLearnError as e:
                        notes.append(f"{subject.code}: one conversation couldn't be read ({e.status}).")
                        continue
                    for raw in thread:
                        m = to_message(raw, conv["id"], subject.code, site.id, me)
                        if m is not None and m.posted_at >= since:
                            messages.append(m)
        messages.sort(key=lambda m: m.posted_at, reverse=True)
        log.info("messages: %d in %d day(s)", len(messages), since_days)
        return MessageList(since_days=since_days, messages=messages, notes=notes)


# ---------- Pure functions (no network; tested offline in step 15) ----------

# True if the conversation's newest message (the preview in the list, or its
# updatedDate) is on or after `since`, i.e. its full thread is worth fetching.
def active_since(conv: dict[str, Any], since: datetime) -> bool:
    times = [parse_time(m.get("postDate")) for m in conv.get("messages") or []]
    times.append(parse_time(conv.get("updatedDate")))
    newest = max((t for t in times if t is not None), default=None)
    return newest is not None and newest >= since


# The sender's name: "given family". None if the sender object is missing
# (e.g. a deleted user) or has no name parts.
# Careful: `preferredDisplayName` is NOT a name. It's a Blackboard setting
# saying WHICH name to show (verified 24 Oct: "GIVEN_NAME" on all 25 messages).
# An earlier version returned it as the name, so every sender showed up as
# "GIVEN_NAME". It's only used here to pick otherName (e.g. an English name)
# when the setting asks for it and the field is filled in (never seen yet).
def sender_name(sender: Any) -> str | None:
    if not isinstance(sender, dict):
        return None
    given = (sender.get("givenName") or "").strip()
    family = (sender.get("familyName") or "").strip()
    other = (sender.get("otherName") or "").strip()
    mode = (sender.get("preferredDisplayName") or "").strip().upper()
    if other and mode == "OTHER_NAME":
        given = other
    elif other and mode == "BOTH":
        given = f"{given} ({other})" if given else other
    return f"{given} {family}".strip() or None


# The HTML of a message body: displayText (what the page shows), else rawText.
# Also accepts a plain string body, in case some messages use one.
def body_html(body: Any) -> str | None:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        return body.get("displayText") or body.get("rawText")
    return None


# Turns one raw message into the Message model. Returns None if it has no id
# or no post date.
def to_message(raw: dict[str, Any], conversation_id: str, course_code: str, site_id: str, me: str) -> Message | None:
    posted = parse_time(raw.get("postDate"))
    if not raw.get("id") or posted is None:
        return None
    parsed = parse_html(body_html(raw.get("body")), BODY_MAX_CHARS)
    return Message(
        id=raw["id"],
        conversation_id=conversation_id,
        course_code=course_code,
        course_site_id=site_id,
        sender_name=sender_name(raw.get("sender")),
        body=parsed.text,
        links=[Link(text=t, url=u) for t, u in parsed.links],
        files=parsed.files,
        posted_at=posted,
        is_read=raw.get("isRead"),
        sent_by_me=raw.get("senderId") == me,
    )


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.messages
# Live, GET only. Prints counts, codes and dates; never names or bodies.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of course messages (GET only).").parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    from uni_copilot.ntulearn_mcp.config import load_settings

    http = NtuLearnHttp()
    client = MessagesClient(http, CoursesClient(http, load_settings()))
    try:
        http.ensure_signed_in()
    except AuthError as e:
        print("Not signed in:", e)
        return

    t0 = time.monotonic()
    r = client.recent(since_days=14)
    print(f"1) last 14 days: {len(r.messages)} messages ({time.monotonic() - t0:.0f} s)")
    for m in r.messages:
        print(f"   {m.course_code:7} {m.posted_sgt}  read={m.is_read!s:5} by_me={m.sent_by_me!s:5} "
              f"has_sender={m.sender_name is not None} lines={m.body.count(chr(10)) + 1} links={len(m.links)} files={len(m.files)}")
    print(f"   notes: {r.notes or 'none'}")

    t0 = time.monotonic()
    r = client.recent(since_days=MAX_DAYS)
    ms = r.messages
    print(f"2) last {MAX_DAYS} days: {len(ms)} messages in {len({m.conversation_id for m in ms})} conversations "
          f"({time.monotonic() - t0:.0f} s), per course {dict(Counter(m.course_code for m in ms))}")
    # Distinct names, not just "has a name": the GIVEN_NAME bug passed a has-a-name check.
    print(f"   by me: {sum(m.sent_by_me for m in ms)}, unread: {sum(m.is_read is False for m in ms)}, "
          f"with sender name: {sum(m.sender_name is not None for m in ms)}, "
          f"distinct sender names: {len({m.sender_name for m in ms})}, with links: {sum(bool(m.links) for m in ms)}, "
          f"with files: {sum(bool(m.files) for m in ms)}, empty body: {sum(not m.body for m in ms)}")
    try:
        client.recent(since_days=7, course="zzzz")
    except ValueError as e:
        print("3) course='zzzz' ->", str(e).split(".")[0])


if __name__ == "__main__":
    _main()
