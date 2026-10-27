"""
Offline tests for pilot-mcp (pilot_mcp/server.py) and the uni-copilot CLI
(cli.py): the tools Claude sees, their answers from a made-up store in a temp
file, the bookmark, the Claude Code config, and the terminal commands.
"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import T0, deadline_event, post_event
from uni_copilot import cli
from uni_copilot.ntulearn_mcp.config import PROJECT_ROOT
from uni_copilot.pilot_mcp import server
from uni_copilot.store.db import open_db, set_state, time_to_db
from uni_copilot.store.events import save_events
from uni_copilot.store.queries import SOURCES

TOOL_NAMES = {"list_events", "whats_new", "get_event", "sync_status", "mark_seen", "mark_done", "send_telegram",
              "delete_telegram_message"}
WRITES = {"mark_seen", "mark_done", "send_telegram", "delete_telegram_message"}
TELEGRAM = {"send_telegram", "delete_telegram_message"}


# Runs an async MCP call from a normal test.
def run(coro):
    return asyncio.run(coro)


# A made-up store in a temp file: a first sync (yesterday) and a second one
# (5 min ago) that finds a moved deadline and a new post. Times are relative
# to the real clock, because the pilot tools use it. Returns the file's path.
@pytest.fixture
def store_file(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    first, second = now - timedelta(days=1), now - timedelta(minutes=5)
    lab = replace(deadline_event(1, 0), due_at=now + timedelta(days=2))
    old_post = replace(post_event(2, 0), posted_at=now - timedelta(days=2))
    new_post = replace(post_event(3, 0, body="Full new body."), posted_at=now - timedelta(hours=3))

    path = tmp_path / "store.sqlite"
    conn = open_db(path)
    save_events(conn, [lab, old_post], first)
    save_events(conn, [replace(lab, due_at=now + timedelta(days=3)), old_post, new_post], second)
    for s in SOURCES:
        set_state(conn, f"last_sync:{s}", time_to_db(second))
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB", path)
    return path


# ---------- the tools Claude sees ----------

# Exactly 8 tools; read-only except the 4 writes, only the 2 Telegram tools
# reaching outside the PC, and only delete_telegram_message destructive.
def test_tool_list():
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    assert set(tools) == TOOL_NAMES
    for name, t in tools.items():
        assert t.annotations.read_only_hint is (name not in WRITES)
        assert t.annotations.open_world_hint is (name in TELEGRAM)
    for name in WRITES:
        assert tools[name].annotations.destructive_hint is (name == "delete_telegram_message")


# Every result carries as_of; lists give previews, not bodies.
def test_list_events(store_file):
    sc = run(server.mcp.call_tool("list_events", {"days": 7})).structured_content
    assert sc["as_of"]["as_of_sgt"].endswith("SGT") and sc["as_of"]["minutes_old"] >= 4
    kinds = sorted(e["kind"] for e in sc["events"])
    assert kinds == ["announcement", "announcement", "deadline"]
    assert all(e["body"] is None for e in sc["events"])
    assert next(e for e in sc["events"] if e["kind"] == "deadline")["due_sgt"].endswith("SGT")


# get_event gives the full body; an unknown id is a readable error.
def test_get_event(store_file):
    sc = run(server.mcp.call_tool("get_event", {"event_id": "ntulearn_announcement:_3_1"})).structured_content
    assert sc["event"]["body"] == "Full new body."
    with pytest.raises(ToolError, match="No event"):
        run(server.mcp.call_tool("get_event", {"event_id": "nope"}))


# whats_new: the moved deadline (as SGT text) and the new post with its body,
# not the first-sync import; mark_seen(up_to=checked_at) clears them.
def test_whats_new_and_mark_seen(store_file):
    sc = run(server.mcp.call_tool("whats_new", {"channel": "terminal"})).structured_content
    changes = {c["change_type"]: c for c in sc["changes"]}
    assert set(changes) == {"moved", "new"}
    assert changes["moved"]["after"].endswith("SGT")
    assert changes["new"]["event"]["body"] == "Full new body."
    run(server.mcp.call_tool("mark_seen", {"channel": "terminal", "up_to": sc["checked_at"]}))
    assert run(server.mcp.call_tool("whats_new", {"channel": "terminal"})).structured_content["changes"] == []
    assert len(run(server.mcp.call_tool("whats_new", {"channel": "telegram"})).structured_content["changes"]) == 2


# A bookmark in the future is capped at now.
def test_mark_seen_caps_future(store_file):
    sc = run(server.mcp.call_tool("mark_seen", {"channel": "terminal", "up_to": "2099-01-01T00:00:00Z"}))
    assert sc.structured_content["last_seen_at"] < "2099"


# sync_status: one entry per source with its last sync and count.
def test_sync_status(store_file):
    sc = run(server.mcp.call_tool("sync_status", {})).structured_content
    assert [s["source"] for s in sc["sources"]] == ["deadlines", "announcements", "messages"]
    assert [s["active_events"] for s in sc["sources"]] == [1, 2, 0]
    assert sc["as_of"]["failed_sources"] == []


# ---------- Phase 3: mark_done, send_telegram, delete_telegram_message ----------

# mark_done: only deadlines; marks, says when nothing changed, shows in
# list_events, and undo takes it back. Other kinds and unknown ids are refused.
def test_mark_done(store_file):
    lab = "ntulearn_gradebook:_1_1"
    sc = run(server.mcp.call_tool("mark_done", {"event_id": lab})).structured_content
    assert sc["marked_done"] is True and sc["changed"] is True and sc["event"]["marked_done_sgt"].endswith("SGT")
    assert run(server.mcp.call_tool("mark_done", {"event_id": lab})).structured_content["changed"] is False

    events = run(server.mcp.call_tool("list_events", {"days": 7, "kinds": ["deadline"]})).structured_content["events"]
    assert [e["marked_done_at"] is not None for e in events] == [True]
    assert events[0]["status"] == "open"  # NTULearn's word stays as it is

    sc = run(server.mcp.call_tool("mark_done", {"event_id": lab, "undo": True})).structured_content
    assert sc["marked_done"] is False and sc["changed"] is True and sc["event"]["marked_done_at"] is None
    for bad in ("ntulearn_announcement:_2_1", "nope"):
        with pytest.raises(ToolError):
            run(server.mcp.call_tool("mark_done", {"event_id": bad}))


# Replaces the server's TelegramBot with a fake that records calls (or raises).
@pytest.fixture
def fake_bot(monkeypatch):
    from uni_copilot.notify.telegram import Sent, TelegramError, TelegramSettings

    calls: list[tuple] = []
    fail: list[Exception] = []

    class FakeBot:
        def __init__(self, settings):
            pass

        # Like TelegramBot.send_message.
        def send_message(self, text: str, html: bool = False) -> Sent:
            if fail:
                raise fail[0]
            calls.append(("send", text, html))
            return Sent(parts=1, plain_fallbacks=0)

        # Like TelegramBot.delete_message.
        def delete_message(self, message_id: int) -> None:
            if fail:
                raise fail[0]
            calls.append(("delete", message_id))

    monkeypatch.setattr(server, "TelegramBot", FakeBot)
    monkeypatch.setattr(server, "load_telegram_settings", lambda: TelegramSettings("123456:FAKE", "42"))
    return calls, fail, TelegramError


# send_telegram: plain text (never HTML) to the configured chat; empty or too
# long text is refused; a Telegram error comes back as a ToolError.
def test_send_telegram(fake_bot):
    calls, fail, TelegramError = fake_bot
    sc = run(server.mcp.call_tool("send_telegram", {"text": "Hello"})).structured_content
    assert sc["parts"] == 1 and calls == [("send", "Hello", False)]
    for text in ("", "x" * (server.SEND_MAX_CHARS + 1)):
        with pytest.raises(ToolError):
            run(server.mcp.call_tool("send_telegram", {"text": text}))
    fail.append(TelegramError("sendMessage", 502, "Bad Gateway"))
    with pytest.raises(ToolError, match="Not sent"):
        run(server.mcp.call_tool("send_telegram", {"text": "Hello"}))
    assert len(calls) == 1


# delete_telegram_message: deletes by id; Telegram's refusal comes back as a ToolError.
def test_delete_telegram_message(fake_bot):
    calls, fail, TelegramError = fake_bot
    result = run(server.mcp.call_tool("delete_telegram_message", {"message_id": 28}))
    assert calls == [("delete", 28)] and "deleted (id: 28)" in str(result.structured_content)
    fail.append(TelegramError("deleteMessage", 400, "Bad Request: message can't be deleted"))
    with pytest.raises(ToolError, match="Not deleted"):
        run(server.mcp.call_tool("delete_telegram_message", {"message_id": 28}))


# ---------- Claude Code config ----------

# settings.json pre-allows exactly the pilot tools; .mcp.json starts the server.
def test_pilot_config():
    settings = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    prefix = "mcp__pilot__"
    allowed = {r.removeprefix(prefix) for r in settings["permissions"]["allow"] if r.startswith(prefix)}
    assert allowed == TOOL_NAMES
    mcp_json = json.loads((PROJECT_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_json["mcpServers"]["pilot"]["args"] == ["run", "pilot-mcp"]


# The Telegram session's extra settings: the "💭 Thinking…" hooks, and the
# reply-hook matcher names the plugin's reply tool exactly.
def test_telegram_session_hooks():
    hooks = json.loads((PROJECT_ROOT / ".claude" / "telegram-session.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse", "Stop"}
    (reply,) = hooks["PreToolUse"]
    assert reply["matcher"] == "mcp__plugin_telegram_telegram__reply"
    commands = [h["command"] for group in hooks.values() for g in group for h in g["hooks"]]
    assert all("uni_copilot.notify.thinking" in c for c in commands)


# ---------- the uni-copilot CLI (offline commands only) ----------

# No store yet: status says how to make one, exit code 1.
def test_cli_status_without_store(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "none.sqlite"), "status"]) == 1
    assert "uni-copilot sync" in capsys.readouterr().out


# status, events and changes read the store and print "as of".
def test_cli_reads_store(store_file, capsys):
    assert cli.main(["--db", str(store_file), "status"]) == 0
    out = capsys.readouterr().out
    assert "deadlines" in out and "as of" in out and "diary" in out

    assert cli.main(["--db", str(store_file), "events", "--kind", "deadline"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("As of") and "Made-up Assignment 1" in out and "Posts" not in out

    assert cli.main(["--db", str(store_file), "changes"]) == 0
    out = capsys.readouterr().out
    assert "first sync: started watching 1 deadlines" in out
    assert "moved" in out and "-> " in out and "new" in out


# "N min ago" / "N h ago" / "N days ago".
def test_ago():
    assert cli.ago(T0 - timedelta(minutes=5), T0) == "5 min ago"
    assert cli.ago(T0 - timedelta(hours=3), T0) == "3 h ago"
    assert cli.ago(T0 - timedelta(days=3), T0) == "3 days ago"
