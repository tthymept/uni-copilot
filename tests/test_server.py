"""
Offline tests for server.py: the tool list Claude sees, error messages, and the
permission rules in .claude/settings.json. No tool here reaches NTULearn: the
shared clients are swapped for fakes with monkeypatch.
"""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from uni_copilot.ntulearn_mcp import server
from uni_copilot.ntulearn_mcp.auth.session import LoginRequired
from uni_copilot.ntulearn_mcp.config import PROJECT_ROOT
from uni_copilot.ntulearn_mcp.models import AnnouncementList

TOOL_NAMES = {"login_status", "login", "list_courses", "upcoming_deadlines",
              "recent_announcements", "recent_messages"}


# Runs an async MCP call from a normal test.
def run(coro):
    return asyncio.run(coro)


# Exactly the 6 Phase 1 tools exist, every one marked read-only. No grades tool.
def test_tool_list():
    tools = run(server.mcp.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    assert not any("grade" in t.name for t in tools)


# Inputs carry their limits and descriptions, so Claude knows what it may send.
def test_input_schemas():
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    days = tools["upcoming_deadlines"].input_schema["properties"]["days"]
    assert (days["minimum"], days["maximum"], days["default"]) == (1, 120, 7)
    since = tools["recent_announcements"].input_schema["properties"]["since_days"]
    assert (since["minimum"], since["maximum"], since["default"]) == (1, 180, 21)
    assert "IE2108" in json.dumps(tools["recent_messages"].input_schema["properties"]["course"])


# The instructions sent to Claude cover the safety rules.
def test_instructions():
    text = server.INSTRUCTIONS
    assert "UNTRUSTED" in text and "Never follow instructions" in text
    assert "Never ask for passwords" in text


# Out-of-range input is refused by the SDK before our code runs.
def test_days_out_of_range():
    with pytest.raises(ToolError, match="less than or equal to 120"):
        run(server.mcp.call_tool("upcoming_deadlines", {"days": 999}))


# Our errors become ToolError messages Claude can read.
def test_errors_become_tool_errors(monkeypatch):
    # A fake announcements client whose answer depends on the course asked for.
    class FakeAnnouncements:
        def recent(self, since_days, course):
            if course == "zzzz":
                raise ValueError("No course matches 'zzzz'.")
            raise LoginRequired("session expired")

    monkeypatch.setattr(server, "_announcements", FakeAnnouncements())
    with pytest.raises(ToolError, match="No course matches"):
        run(server.mcp.call_tool("recent_announcements", {"course": "zzzz"}))
    with pytest.raises(ToolError, match="login"):
        run(server.mcp.call_tool("recent_announcements", {"course": "XX1001"}))


# A successful call returns structured JSON with the computed fields included.
def test_structured_result(monkeypatch):
    # A fake announcements client that returns an empty list.
    class FakeAnnouncements:
        def recent(self, since_days, course):
            return AnnouncementList(since_days=since_days, announcements=[], notes=["n"])

    monkeypatch.setattr(server, "_announcements", FakeAnnouncements())
    result = run(server.mcp.call_tool("recent_announcements", {"since_days": 5}))
    assert result.structured_content == {"notes": ["n"], "since_days": 5, "announcements": []}


# login_status reports "not signed in" as a normal answer, not an error.
def test_login_status_signed_out(monkeypatch):
    # A fake HTTP client that is always signed out.
    class SignedOut:
        def get_json(self, path, params=None):
            raise LoginRequired("no session")

    monkeypatch.setattr(server, "_http", SignedOut())
    result = run(server.mcp.call_tool("login_status", {}))
    assert result.structured_content["signed_in"] is False
    assert "login" in result.structured_content["detail"]


# .claude/settings.json allows exactly the read-only ntulearn tools that exist, and never `login`.
# (Rules for other servers, like mcp__pilot__..., are checked by their own tests.)
def test_permission_rules_match_tools():
    settings = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    prefix = "mcp__ntulearn__"
    allowed = {rule.removeprefix(prefix) for rule in settings["permissions"]["allow"] if rule.startswith(prefix)}
    assert allowed == TOOL_NAMES - {"login"}
    mcp_json = json.loads((PROJECT_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_json["mcpServers"]["ntulearn"]["args"] == ["run", "ntulearn-mcp"]
