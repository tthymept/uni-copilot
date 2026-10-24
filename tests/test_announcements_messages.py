"""Offline tests for client/announcements.py and client/messages.py."""

from datetime import timedelta

from conftest import NOW, iso
from uni_copilot.ntulearn_mcp.client.announcements import (
    AnnouncementsClient,
    is_recent,
    merge_duplicates,
    to_announcement,
    visible_now,
)
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError
from uni_copilot.ntulearn_mcp.client.messages import (
    MessagesClient,
    active_since,
    body_html,
    sender_name,
    to_message,
)


# Builds one raw announcement like the API returns.
def raw_ann(id, created_h, modified_h=None, title="Quiz venue", body="<p>LT99</p>", **extra):
    return {"id": id, "title": title, "body": body, "created": iso(created_h),
            "modified": iso(modified_h if modified_h is not None else created_h), "draft": False, **extra}


# ---------- Announcements ----------

# Drafts and Restricted posts outside their window are hidden; Permanent ones show.
def test_visible_now():
    assert visible_now({"availability": {"duration": {"type": "Permanent"}}}, NOW)
    assert not visible_now({"draft": True}, NOW)
    future = {"availability": {"duration": {"type": "Restricted", "start": iso(5), "end": None}}}
    expired = {"availability": {"duration": {"type": "Restricted", "start": iso(-50), "end": iso(-1)}}}
    open_now = {"availability": {"duration": {"type": "Restricted", "start": iso(-5), "end": iso(5)}}}
    assert not visible_now(future, NOW)
    assert not visible_now(expired, NOW)
    assert visible_now(open_now, NOW)


# An old post edited recently counts as recent; an untouched old post doesn't.
def test_is_recent_and_edited():
    since = NOW - timedelta(days=21)
    old_edited = to_announcement(raw_ann("_1_1", -24 * 60, -24), "XX1001", "_10_1")
    old_plain = to_announcement(raw_ann("_2_1", -24 * 60), "XX1001", "_10_1")
    assert old_edited.edited and old_edited.modified_sgt is not None
    assert not old_plain.edited and old_plain.modified_sgt is None
    assert is_recent(old_edited, since) and not is_recent(old_plain, since)


# A modified time within 1 minute of creation is not an edit.
def test_small_modified_gap_is_not_edit():
    a = to_announcement(raw_ann("_1_1", -5, -5 + 0.5 / 60), "XX1001", "_10_1")
    assert not a.edited


# The body is parsed into Markdown with links and files; broken posts are skipped.
def test_to_announcement():
    a = to_announcement(raw_ann("_1_1", -1, body='<p>See <a href="https://x.org">this</a></p>'), "XX1001", "_10_1")
    assert a.body == "See [this](https://x.org)"
    assert [(l.text, l.url) for l in a.links] == [("this", "https://x.org")]
    assert to_announcement({"id": "_9_1"}, "XX1001", "_10_1") is None  # no created date


# The same post on lecture + tutorial site becomes one item with both site ids, newest first.
def test_merge_duplicates():
    lec = to_announcement(raw_ann("_1_1", -2), "XX1001", "_10_1")
    tut = to_announcement(raw_ann("_5_1", -2), "XX1001", "_11_1")
    newer = to_announcement(raw_ann("_6_1", -1, title="Other"), "XX1001", "_10_1")
    merged = merge_duplicates([lec, tut, newer])
    assert [a.title for a in merged] == ["Other", "Quiz venue"]
    assert merged[1].course_site_ids == ["_10_1", "_11_1"]


# The full client on fake data: window filter, hidden posts, failing site -> note.
def test_announcements_client(make_courses):
    path = "/learn/api/public/v1/courses/{}/announcements"
    routes = {
        path.format("_10_1"): {"results": [raw_ann("_1_1", -2), raw_ann("_2_1", -24 * 40), raw_ann("_3_1", -1, draft=True)]},
        path.format("_11_1"): {"results": [raw_ann("_4_1", -2)]},  # cross-post of _1_1
        path.format("_20_1"): NtuLearnError("/x", 500),
    }
    courses, http = make_courses(routes)
    result = AnnouncementsClient(http, courses).recent(since_days=21)
    assert [(a.id, a.course_site_ids) for a in result.announcements] == [("_1_1", ["_10_1", "_11_1"])]
    assert any("YY2002 announcements unavailable" in n for n in result.notes)


# ---------- Messages ----------

# The name is "given family"; no sender object or no name parts -> None.
def test_sender_name():
    assert sender_name({"givenName": "A", "familyName": "B"}) == "A B"
    assert sender_name({"givenName": "A"}) == "A"
    assert sender_name(None) is None and sender_name({}) is None


# Regression: preferredDisplayName is a SETTING ("GIVEN_NAME"), never the name.
# This is the bug that showed every sender as "GIVEN_NAME".
def test_sender_name_ignores_display_setting():
    sender = {"preferredDisplayName": "GIVEN_NAME", "givenName": "A", "familyName": "B", "title": "Dr"}
    assert sender_name(sender) == "A B"
    assert sender_name({"preferredDisplayName": "GIVEN_NAME"}) is None


# The setting picks otherName (e.g. an English name) only when that field is filled.
def test_sender_name_other_name():
    base = {"givenName": "A", "familyName": "B", "otherName": "Al"}
    assert sender_name({**base, "preferredDisplayName": "OTHER_NAME"}) == "Al B"
    assert sender_name({**base, "preferredDisplayName": "BOTH"}) == "A (Al) B"
    assert sender_name({"givenName": "A", "familyName": "B", "preferredDisplayName": "OTHER_NAME"}) == "A B"


# displayText first, then rawText; a plain string body also works.
def test_body_html():
    assert body_html({"displayText": "<p>d</p>", "rawText": "<p>r</p>"}) == "<p>d</p>"
    assert body_html({"rawText": "<p>r</p>"}) == "<p>r</p>"
    assert body_html("<p>s</p>") == "<p>s</p>"
    assert body_html(None) is None


# A conversation is "active" if its newest preview message or updatedDate is in the window.
def test_active_since():
    since = NOW - timedelta(days=14)
    assert active_since({"messages": [{"postDate": iso(-1)}]}, since)
    assert active_since({"messages": [], "updatedDate": iso(-1)}, since)
    assert not active_since({"messages": [{"postDate": iso(-24 * 30)}]}, since)
    assert not active_since({}, since)


# A raw message becomes a Message: parsed body, sender, read flag, sent_by_me.
def test_to_message():
    raw = {"id": "_m_1", "postDate": iso(-1), "isRead": False, "senderId": "_u_1",
           "sender": {"givenName": "A", "familyName": "B"}, "body": {"displayText": "<p>Hi <b>all</b></p>"}}
    m = to_message(raw, "_c_1", "XX1001", "_10_1", me="_u_1")
    assert (m.body, m.sender_name, m.is_read, m.sent_by_me) == ("Hi **all**", "A B", False, True)
    assert to_message({"id": "_m_2"}, "_c_1", "XX1001", "_10_1", me="_u_1") is None


# The full client: only active threads are fetched in full; old messages are filtered out.
def test_messages_client(make_courses):
    conv = "/learn/api/v1/courses/{}/conversations"
    routes = {
        conv.format("_10_1"): {"results": [
            {"id": "_c_1", "messages": [{"postDate": iso(-1)}]},
            {"id": "_c_2", "messages": [{"postDate": iso(-24 * 60)}]},  # inactive: never fetched
        ]},
        conv.format("_11_1"): {"results": []},
        conv.format("_20_1"): {"results": []},
        conv.format("_10_1") + "/_c_1/messages": {"results": [
            {"id": "_m_1", "postDate": iso(-24 * 30), "body": {"displayText": "old"}},
            {"id": "_m_2", "postDate": iso(-1), "body": {"displayText": "new"}, "senderId": "_p_1"},
        ]},
    }
    courses, http = make_courses(routes)
    result = MessagesClient(http, courses).recent(since_days=14)
    assert [(m.id, m.body, m.sent_by_me) for m in result.messages] == [("_m_2", "new", False)]
    assert not any("_c_2/messages" in c for c in http.calls)
