"""
Offline tests for sending a course file to Telegram: TelegramBot.send_document
(notify/telegram.py) and the send_course_file tool (ntulearn_mcp/server.py).

In plain words:
  A fake Telegram records each upload (form fields and the file) instead of
  sending it; the course and its files come from fake_ntulearn.py. The token
  and chat id are made up. Checks: only your chat, size limits, the file name
  cleaned for display, the token never in an error, no signed link in what
  Telegram gets, and plain errors when something's missing.
"""

import asyncio

import pytest
import requests
from mcp.server.mcpserver.exceptions import ToolError

from fake_ntulearn import NOW, FakeContentHttp, FakeCourses
from uni_copilot.notify.telegram import (
    MAX_DOCUMENT_BYTES,
    TelegramBot,
    TelegramError,
    TelegramNotConfigured,
    TelegramSettings,
    document_name,
)
from uni_copilot.ntulearn_mcp import server
from uni_copilot.ntulearn_mcp.cache import ContentCache
from uni_copilot.ntulearn_mcp.client.contents import ContentsClient

TOKEN = "123456:FAKE-token-for-tests"
SETTINGS = TelegramSettings(token=TOKEN, chat_id="42")


# Stands in for requests.Session: records uploads, answers with `status` and `body`.
class FakeTelegram:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true, "result": {"message_id": 7}}'):
        self.status, self.body = status, body
        self.sent: list[dict] = []

    def post(self, url, json=None, data=None, files=None, timeout=None):
        self.sent.append({"method": url.rsplit("/", 1)[-1], "data": data, "files": files, "timeout": timeout})
        r = requests.Response()
        r.status_code, r._content = self.status, self.body
        return r


# ---------- send_document ----------

# A multipart upload to your chat: chat id and caption as form fields, the file as a part.
def test_send_document():
    tg = FakeTelegram()
    message_id = TelegramBot(SETTINGS, tg, sleep=lambda s: None).send_document("T10.pdf", b"%PDF-1.4", "XX1000: T10.pdf")
    (call,) = tg.sent
    assert message_id == 7 and call["method"] == "sendDocument"
    assert call["data"] == {"chat_id": "42", "caption": "XX1000: T10.pdf"}
    assert call["files"]["document"] == ("T10.pdf", b"%PDF-1.4")


@pytest.mark.parametrize("size, words", [(0, "empty"), (MAX_DOCUMENT_BYTES + 1, "at most 50 MB")],
                         ids=["empty", "over-50MB"])
def test_send_document_limits(size, words):
    tg = FakeTelegram()
    with pytest.raises(ValueError, match=words):
        TelegramBot(SETTINGS, tg, sleep=lambda s: None).send_document("a.pdf", b"x" * size)
    assert tg.sent == []


def test_document_name():
    assert document_name("Lec 9\nAVL\t.pdf") == "Lec 9 AVL .pdf"
    assert document_name("") == "file"
    long = document_name("x" * 300 + ".pdf")
    assert long.endswith(".pdf") and len(long) <= 120


# Telegram's error comes back with the token scrubbed out.
def test_send_document_error_scrubs_token():
    tg = FakeTelegram(400, f'{{"ok": false, "description": "Bad Request: bot{TOKEN} file too big"}}'.encode())
    with pytest.raises(TelegramError) as caught:
        TelegramBot(SETTINGS, tg, sleep=lambda s: None).send_document("a.pdf", b"%PDF-")
    assert TOKEN not in str(caught.value) and "<token>" in str(caught.value)


# ---------- The send_course_file tool ----------

# The tool with the made-up course and a fake Telegram behind it.
@pytest.fixture
def tool(tmp_path, monkeypatch):
    http = FakeContentHttp()
    contents = ContentsClient(http, FakeCourses(), ContentCache(tmp_path / "content.sqlite"), now=lambda: NOW)
    tg = FakeTelegram()
    monkeypatch.setattr(server, "_contents", contents)
    monkeypatch.setattr(server, "load_telegram_settings", lambda: SETTINGS)
    monkeypatch.setattr(server, "TelegramBot", lambda settings: TelegramBot(settings, tg, sleep=lambda s: None))

    def call(args):
        return asyncio.run(server.mcp.call_tool("send_course_file", args)).structured_content
    return call, tg, http, monkeypatch


# A file item and a file embedded in a page both go out as real PDFs, with
# the course in the caption and no signed link anywhere.
def test_sends_course_files(tool):
    call, tg, _, _ = tool
    result = call({"course": "XX1000", "content_id": "_13_1"})
    assert (result["file_name"], result["file_type"], result["course_code"]) == ("T10.pdf", "pdf", "XX1000")
    assert tg.sent[0]["data"]["caption"] == "XX1000: T10.pdf"
    assert tg.sent[0]["files"]["document"][1].startswith(b"%PDF-")
    call({"course": "XX1000", "content_id": "_11_1", "file_ref": "xid-555_1"})
    assert tg.sent[1]["files"]["document"][0] == "week10-slides.pdf"
    assert "SECRET" not in str(tg.sent)


# Each problem is a plain error for Claude, and nothing is sent.
def test_send_errors_are_plain(tool):
    call, tg, http, monkeypatch = tool
    with pytest.raises(ToolError, match="only files"):
        call({"course": "XX1000", "content_id": "_14_1"})              # a quiz
    http.missing = True
    with pytest.raises(ToolError, match="404"):
        call({"course": "XX1000", "content_id": "_13_1"})
    http.missing = False

    def not_set_up():
        raise TelegramNotConfigured("TELEGRAM_CHAT_ID is missing: run `uv run uni-copilot telegram-chat-id`.")
    monkeypatch.setattr(server, "load_telegram_settings", not_set_up)
    with pytest.raises(ToolError, match="Telegram isn't set up"):
        call({"course": "XX1000", "content_id": "_13_1"})
    assert tg.sent == []
