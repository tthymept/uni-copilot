"""
Offline tests for notify/telegram.py (the door to Telegram) and notify/thinking.py
(the "💭 Thinking…" note).

In plain words:
  TelegramBot normally POSTs to api.telegram.org. Here it gets a FakeSession
  instead: same `post` method, but it records each call and hands back made-up
  answers from a list, and `sleep` is a recorder, so retries are checked
  instantly. Nothing is sent anywhere. The token in these tests is made up.
"""

from typing import Any

import pytest
import requests

from uni_copilot.notify import telegram as tg
from uni_copilot.notify import thinking
from uni_copilot.notify.telegram import (
    TelegramBot,
    TelegramError,
    TelegramNotConfigured,
    TelegramSettings,
    escape,
    load_telegram_settings,
    read_env_file,
    split_message,
    strip_html,
    telegram_length,
)

TOKEN = "123456:FAKE-token-for-tests"
SETTINGS = TelegramSettings(token=TOKEN, chat_id="42")


# A made-up Bot API response: a status code and a JSON body (None = not JSON).
class FakeResponse:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body

    # Returns the body, or raises ValueError like requests does for non-JSON.
    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


# Stands in for requests.Session. Hands out the given answers in order (an
# exception is raised instead of returned) and records (method, payload).
class FakeSession:
    def __init__(self, answers: list[Any]):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    # Same signature as requests.Session.post, as TelegramBot uses it.
    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url.rsplit("/", 1)[1], dict(json)))  # a copy: the bot reuses its dict
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# A Bot API "ok" answer with this result.
def ok(result: Any = True) -> FakeResponse:
    return FakeResponse(200, {"ok": True, "result": result})


# A Bot API error answer.
def error(status: int, description: str, retry_after: int | None = None) -> FakeResponse:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return FakeResponse(status, body)


# A TelegramBot wired to a FakeSession; returns (bot, session, waits).
def make_bot(answers: list[Any], settings: TelegramSettings = SETTINGS):
    session, waits = FakeSession(answers), []
    return TelegramBot(settings, session=session, sleep=waits.append), session, waits


# ---------- settings ----------

# .env lines: comments and blanks skipped, quotes and a byte-order mark handled.
def test_read_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text('﻿TELEGRAM_BOT_TOKEN = "1:abc"\n# a comment\n\nTELEGRAM_CHAT_ID=42\nnot a setting\n',
                    encoding="utf-8")
    assert read_env_file(path) == {"TELEGRAM_BOT_TOKEN": "1:abc", "TELEGRAM_CHAT_ID": "42"}
    assert read_env_file(tmp_path / "missing.env") == {}


# Environment variables win over .env; missing or malformed values fail clearly,
# and the message never contains the value.
def test_load_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    assert load_telegram_settings(path) == TelegramSettings("1:abc", "42")

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    assert load_telegram_settings(path).chat_id == "-100"

    path.write_text("TELEGRAM_BOT_TOKEN=1:abc\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    with pytest.raises(TelegramNotConfigured, match="TELEGRAM_CHAT_ID is missing"):
        load_telegram_settings(path)
    assert load_telegram_settings(path, need_chat_id=False).chat_id is None

    path.write_text("TELEGRAM_BOT_TOKEN=not-a-token\n", encoding="utf-8")
    with pytest.raises(TelegramNotConfigured) as e:
        load_telegram_settings(path, need_chat_id=False)
    assert "not-a-token" not in str(e.value)


# Printing the settings never shows the token.
def test_settings_repr_hides_token():
    assert TOKEN not in repr(SETTINGS) and "42" in repr(SETTINGS)


# ---------- splitting and escaping ----------

# Parts stay under the limit and are cut at line breaks; emoji count double.
def test_split_message():
    text = "\n".join(f"line {n} " + "x" * 90 for n in range(100))
    parts = split_message(text)
    assert len(parts) == 3 and all(telegram_length(p) <= 4096 for p in parts)
    assert "\n".join(parts) == text
    assert len(split_message("y" * 10_000)) == 3
    assert telegram_length("ok 👍") == 5
    assert split_message("  \n ") == []


# In HTML mode a tag that spans lines (a <blockquote> box) is never cut.
def test_split_keeps_boxes_whole():
    boxes = "<blockquote>a\nb</blockquote>\n<blockquote>c\nd</blockquote>"
    assert split_message(boxes, 45, html=True) == ["<blockquote>a\nb</blockquote>", "<blockquote>c\nd</blockquote>"]
    assert split_message(boxes, 45)[0].endswith("<blockquote>c")  # plain mode doesn't know about tags


# Other people's text is escaped for HTML mode and comes back for plain text.
def test_escape_and_strip():
    formatted = f"<b>XX1001</b> {escape('Lab <draft> & notes')}"
    assert formatted == "<b>XX1001</b> Lab &lt;draft&gt; &amp; notes"
    assert strip_html(formatted) == "XX1001 Lab <draft> & notes"


# ---------- sending ----------

# A plain message: right chat, no parse mode, link previews off.
def test_send_plain():
    bot, session, _ = make_bot([ok({"message_id": 1})])
    sent = bot.send_message("hello")
    assert (sent.parts, sent.plain_fallbacks) == (1, 0)
    method, payload = session.calls[0]
    assert method == "sendMessage" and payload["chat_id"] == "42" and payload["text"] == "hello"
    assert "parse_mode" not in payload and payload["link_preview_options"] == {"is_disabled": True}


# A long text goes out in parts, one second apart.
def test_send_in_parts():
    text = "\n".join("x" * 100 for _ in range(100))
    bot, session, waits = make_bot([ok(), ok(), ok()])
    assert bot.send_message(text).parts == 3
    assert waits == [tg.PART_INTERVAL_S, tg.PART_INTERVAL_S] and len(session.calls) == 3


# HTML rejected by Telegram -> the same part again as plain text, counted.
def test_html_falls_back_to_plain():
    bot, session, _ = make_bot([error(400, "Bad Request: can't parse entities: bad tag"), ok()])
    sent = bot.send_message("<b>XX1001</b> Lab &lt;draft&gt;", html=True)
    assert sent.plain_fallbacks == 1
    assert session.calls[0][1]["parse_mode"] == "HTML"
    assert "parse_mode" not in session.calls[1][1] and session.calls[1][1]["text"] == "XX1001 Lab <draft>"


# Other 400s in HTML mode are real errors, not a fallback.
def test_other_400_is_an_error():
    bot, _, _ = make_bot([error(400, "Bad Request: chat not found")])
    with pytest.raises(TelegramError, match="chat not found"):
        bot.send_message("<b>x</b>", html=True)


# 429 waits as long as Telegram says; 5xx and network errors wait 2, 4 s.
def test_retries():
    bot, session, waits = make_bot([error(429, "Too Many Requests", retry_after=7), error(502, "Bad Gateway"),
                                    requests.ConnectionError("down"), ok()])
    assert bot.send_message("hello").parts == 1
    assert waits == [7.0, 4.0, 8.0] and len(session.calls) == 4


# Still failing after all retries -> TelegramError with the status.
def test_gives_up_after_retries():
    bot, session, _ = make_bot([error(500, "Internal")] * 4)
    with pytest.raises(TelegramError) as e:
        bot.send_message("hello")
    assert e.value.status == 500 and len(session.calls) == 4


# The token never appears in an error, even if Telegram's description has it,
# and a network error doesn't carry the URL (which has the token in it).
def test_token_never_in_errors():
    bot, _, _ = make_bot([error(401, f"Unauthorized for bot{TOKEN}")])
    with pytest.raises(TelegramError) as e:
        bot.send_message("hello")
    assert TOKEN not in str(e.value) and "<token>" in str(e.value)

    bot, _, _ = make_bot([requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")] * 4)
    with pytest.raises(TelegramError) as e:
        bot.send_message("hello")
    assert TOKEN not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


# Sending needs a chat id; empty text is refused before any request.
def test_send_needs_chat_id_and_text():
    bot, session, _ = make_bot([], TelegramSettings(TOKEN, None))
    with pytest.raises(TelegramNotConfigured):
        bot.send_message("hello")
    bot, session, _ = make_bot([])
    with pytest.raises(ValueError):
        bot.send_message("   ")
    assert session.calls == []


# post_note returns the message id; delete_message deletes in your chat.
def test_post_note_and_delete():
    bot, session, _ = make_bot([ok({"message_id": 99}), ok()])
    assert bot.post_note("💭 Thinking…") == 99
    bot.delete_message(99)
    assert session.calls[1] == ("deleteMessage", {"chat_id": "42", "message_id": 99})


# ---------- setup helper ----------

# Only private chats are listed, once each; getUpdates doesn't mark anything read.
def test_find_chat_ids():
    updates = [
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Made"}}},
        {"message": {"chat": {"id": 42, "type": "private", "first_name": "Made"}}},
        {"message": {"chat": {"id": -100, "type": "group", "title": "A group"}}},
        {"edited_message": {}},
    ]
    bot, session, _ = make_bot([ok(updates)], TelegramSettings(TOKEN, None))
    assert bot.find_chat_ids() == [("42", "Made")]
    assert "offset" not in session.calls[0][1] and session.calls[0][1]["timeout"] == 0


# 409 = another program (the plugin) is polling: a clear hint.
def test_find_chat_ids_conflict():
    bot, _, _ = make_bot([error(409, "Conflict: terminated by other getUpdates request")])
    with pytest.raises(TelegramError, match="Telegram plugin"):
        bot.find_chat_ids()


# ---------- the "💭 Thinking…" note ----------

# Records posts and deletes instead of talking to Telegram.
class FakeBot:
    def __init__(self):
        self.next_id, self.posted, self.deleted = 100, [], []

    # Like TelegramBot.post_note: returns a new message id.
    def post_note(self, text: str) -> int:
        self.next_id += 1
        self.posted.append((self.next_id, text))
        return self.next_id

    # Like TelegramBot.delete_message.
    def delete_message(self, message_id: int) -> None:
        self.deleted.append(message_id)


# Points thinking.py's files at a temp folder and runs one hook event.
@pytest.fixture
def note_files(tmp_path, monkeypatch):
    monkeypatch.setattr(thinking, "DATA_DIR", tmp_path)
    monkeypatch.setattr(thinking, "MSG", tmp_path / "thinking.msg")
    monkeypatch.setattr(thinking, "LOG", tmp_path / "thinking.log")
    return tmp_path


# Feeds one hook event (as JSON on stdin) to thinking._hook.
def hook(bot: FakeBot, event: dict, monkeypatch) -> None:
    import io
    import json

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    thinking._hook(bot)


# A Telegram message posts the note; the reply hook deletes it; terminal
# prompts post nothing; a second message replaces a leftover note.
def test_thinking_note(note_files, monkeypatch):
    bot = FakeBot()
    telegram_prompt = {"hook_event_name": "UserPromptSubmit",
                       "prompt": '<channel source="plugin:telegram:telegram" chat_id="42">hi</channel>'}

    hook(bot, {"hook_event_name": "UserPromptSubmit", "prompt": "/telegram:access set x"}, monkeypatch)
    assert bot.posted == [] and not thinking.MSG.exists()

    hook(bot, telegram_prompt, monkeypatch)
    assert bot.posted == [(101, thinking.NOTE)] and thinking.MSG.read_text() == "101"

    hook(bot, telegram_prompt, monkeypatch)  # a leftover note is replaced, never two
    assert bot.deleted == [101] and thinking.MSG.read_text() == "102"

    thinking.clear(bot, "reply")
    assert bot.deleted == [101, 102] and not thinking.MSG.exists()

    hook(bot, {"hook_event_name": "Stop"}, monkeypatch)  # nothing left: nothing to delete
    assert bot.deleted == [101, 102]
    assert "posted 101" in thinking.LOG.read_text(encoding="utf-8")
