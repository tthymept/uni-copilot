"""
The only door to Telegram. Everything that pushes a message to you goes through TelegramBot.

In plain words:
  - A Telegram bot is an account run by a program. Programs talk to Telegram
    through the Bot API: HTTPS requests to https://api.telegram.org/bot<token>/<method>.
    The token is the bot's password, and it is part of every URL, so this file
    never logs a URL and scrubs the token out of every error message.
  - We only SEND (`sendMessage`). Reading messages sent to the bot is the
    Telegram plugin's job (Phase 3 step 11): only one program may poll a bot.
    The one exception is find_chat_ids(), a one-off setup helper.
  - This is Telegram, not NTULearn: NTULearn is still reached only through
    ntulearn_mcp/client/http.py, GET only. Telegram gets its own session here.

Settings come from the project's `.env` file (git-ignored), one KEY=VALUE per line:
  TELEGRAM_BOT_TOKEN=123456789:AA...   from BotFather
  TELEGRAM_CHAT_ID=123456789           your private chat with the bot (step 2 finds it)
Real environment variables with the same names win over the file.

Formatting: send_message(text, html=True) turns on Telegram's HTML mode, where
<b>bold</b>, <i>italic</i> and <a href="...">links</a> work. Text written by
other people must go through escape() first, so a title like "Lab <draft> & notes"
shows as written instead of breaking the message. If Telegram still rejects the
formatting, the message is resent as plain text with the tags removed, so it's never lost.
"""

import argparse
import html
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from uni_copilot.ntulearn_mcp.config import PROJECT_ROOT

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
ENV_FILE = PROJECT_ROOT / ".env"

# Telegram rejects messages longer than 4096 characters. It counts in UTF-16
# units (an emoji counts as 2), so we measure the same way, tags included,
# which keeps every part safely under the limit.
MAX_MESSAGE_CHARS = 4096
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3            # for 429, 5xx and network errors, with growing waits
PART_INTERVAL_S = 1.0      # Telegram allows about 1 message per second per chat

TAG = re.compile(r"<[^>]*>")
# An opening (<b>, <a href=...>) or closing (</b>) tag; group 1 is "/" for closing.
TAG_EDGE = re.compile(r"<(/?)[a-zA-Z][^>]*>")


# Raised when Telegram answers with an error we can't fix by retrying
# (e.g. 400 bad request, 401 wrong token, 409 someone else is polling).
# Carries the method name, the status and Telegram's own description, never the token.
class TelegramError(Exception):
    def __init__(self, method: str, status: int | None, reason: str = ""):
        self.method = method
        self.status = status
        self.reason = reason
        super().__init__(f"Telegram {status or 'no response'} on {method}" + (f": {reason}" if reason else ""))


# Raised when .env (or the environment) is missing the token or the chat id,
# or they don't look right. The message says what to fix, never the value.
class TelegramNotConfigured(Exception):
    pass


# The two settings. repr=False keeps the token out of print() and tracebacks
# that show the object (a dataclass prints all its fields by default).
@dataclass(frozen=True)
class TelegramSettings:
    token: str = field(repr=False)
    chat_id: str | None


# What one send_message call did: how many messages it took (long texts are
# split), and how many of them had to be resent as plain text.
@dataclass(frozen=True)
class Sent:
    parts: int
    plain_fallbacks: int


# Reads KEY=VALUE lines from a .env file into a dict. Skips blank lines and
# lines starting with #, strips spaces and one pair of surrounding quotes.
# "utf-8-sig" also accepts files saved with a byte-order mark (some Windows
# editors add one), which would otherwise glue itself to the first key.
# A missing file is just an empty dict.
def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


# Loads the token and chat id: environment variables first, then .env.
# The token is required; the chat id only when `need_chat_id` (setup runs
# before you have one). Checks the shapes so a typo fails here with a clear
# message instead of as a confusing Telegram error later.
def load_telegram_settings(env_path: Path = ENV_FILE, need_chat_id: bool = True) -> TelegramSettings:
    file_values = read_env_file(env_path)

    def setting(name: str) -> str:
        return (os.environ.get(name) or file_values.get(name) or "").strip()

    token = setting("TELEGRAM_BOT_TOKEN")
    chat_id = setting("TELEGRAM_CHAT_ID")
    if not token:
        raise TelegramNotConfigured(f"TELEGRAM_BOT_TOKEN is missing: add it to {env_path.name} (from BotFather).")
    bot_id, _, secret = token.partition(":")
    if not (bot_id.isdigit() and secret):
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN doesn't look like a bot token (expected digits:letters).")
    if chat_id and not chat_id.lstrip("-").isdigit():
        raise TelegramNotConfigured("TELEGRAM_CHAT_ID should be a number (run: uv run uni-copilot telegram-chat-id).")
    if need_chat_id and not chat_id:
        raise TelegramNotConfigured(
            f"TELEGRAM_CHAT_ID is missing: run `uv run uni-copilot telegram-chat-id` and add it to {env_path.name}."
        )
    return TelegramSettings(token=token, chat_id=chat_id or None)


# Makes text from other people safe inside an HTML-mode message:
# < > & become &lt; &gt; &amp;, so Telegram shows them instead of reading tags.
# Quotes don't need escaping outside tags.
def escape(text: str) -> str:
    return html.escape(text, quote=False)


# The reverse, for the plain-text fallback: drops the tags and turns
# &lt; &amp; ... back into < & ..., so the plain message reads normally.
def strip_html(text: str) -> str:
    return html.unescape(TAG.sub("", text))


# Replaces the token with "<token>" wherever it appears in `text`.
# Used on every error description before it leaves this file.
def scrub(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


# Length the way Telegram counts it: UTF-16 units (most characters 1, emoji 2).
def telegram_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# How many characters from the start of `line` fit into `limit` UTF-16 units.
def _fit(line: str, limit: int) -> int:
    used = 0
    for i, ch in enumerate(line):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > limit:
            return i
    return len(line)


# Groups lines so that a tag spanning several lines (a <blockquote> box, an
# <i> footer) stays in one unit: a unit ends only when every tag opened in it
# is closed again. Used in HTML mode only; in plain text "<" is just a character.
def _tag_units(lines: list[str]) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    depth = 0
    for line in lines:
        current.append(line)
        for m in TAG_EDGE.finditer(line):
            depth += -1 if m.group(1) else 1
        if depth <= 0:
            units.append("\n".join(current))
            current, depth = [], 0
    if current:
        units.append("\n".join(current))
    return units


# Splits a long text into parts Telegram accepts, cutting only at line breaks,
# and with html=True never inside a tag that spans lines (see _tag_units), so
# no tag is cut in half. A single unit longer than the limit (rare: our own
# units are short) is cut where it has to be; the plain-text fallback in
# send_message covers that case. Whitespace-only parts are dropped (Telegram
# rejects empty text).
def split_message(text: str, limit: int = MAX_MESSAGE_CHARS, html: bool = False) -> list[str]:
    lines = text.split("\n")
    pieces: list[str] = []
    for line in _tag_units(lines) if html else lines:
        while telegram_length(line) > limit:
            cut = _fit(line, limit)
            pieces.append(line[:cut])
            line = line[cut:]
        pieces.append(line)

    parts: list[str] = []
    current: str | None = None
    for piece in pieces:
        joined = piece if current is None else f"{current}\n{piece}"
        if telegram_length(joined) <= limit:
            current = joined
        else:
            parts.append(current)
            current = piece
    if current is not None:
        parts.append(current)
    return [part for part in parts if part.strip()]


class TelegramBot:
    # One requests.Session for all calls (reuses the connection).
    # `session` and `sleep` can be swapped for fakes in tests, so no real
    # request is sent and no test really waits.
    def __init__(
        self,
        settings: TelegramSettings,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._settings = settings
        self._session = session or requests.Session()
        self._sleep = sleep

    # Sends `text` to your chat, split into several messages if it's long.
    # html=True turns on Telegram's HTML formatting; if Telegram rejects the
    # formatting of a part ("can't parse entities"), that part is resent as
    # plain text. Link previews are off, so links don't add big cards.
    # Raises TelegramError if a part can't be sent; parts before it were sent.
    def send_message(self, text: str, html: bool = False) -> Sent:
        if not self._settings.chat_id:
            raise TelegramNotConfigured("TELEGRAM_CHAT_ID is missing: run `uv run uni-copilot telegram-chat-id`.")
        parts = split_message(text, html=html)
        if not parts:
            raise ValueError("nothing to send: the message is empty")

        fallbacks = 0
        for number, part in enumerate(parts):
            if number:
                self._sleep(PART_INTERVAL_S)
            payload: dict[str, Any] = {
                "chat_id": self._settings.chat_id,
                "text": part,
                "link_preview_options": {"is_disabled": True},
            }
            if html:
                payload["parse_mode"] = "HTML"
            try:
                self._call("sendMessage", payload)
            except TelegramError as e:
                if not (html and e.status == 400 and "can't parse entities" in e.reason):
                    raise
                log.warning("sendMessage: Telegram rejected the formatting, resending part %d as plain text", number + 1)
                payload.pop("parse_mode")
                payload["text"] = strip_html(part)
                self._call("sendMessage", payload)
                fallbacks += 1
        log.info("sendMessage: sent %d part(s), %d as plain text", len(parts), fallbacks)
        return Sent(parts=len(parts), plain_fallbacks=fallbacks)

    # Sends one short plain-text note (e.g. "💭 Thinking…") and returns its
    # message id, so it can be deleted again with delete_message.
    def post_note(self, text: str) -> int:
        if not self._settings.chat_id:
            raise TelegramNotConfigured("TELEGRAM_CHAT_ID is missing: run `uv run uni-copilot telegram-chat-id`.")
        sent = self._call("sendMessage", {"chat_id": self._settings.chat_id, "text": text,
                                          "link_preview_options": {"is_disabled": True}})
        return int(sent["message_id"])

    # Deletes one of the bot's own messages from your chat (e.g. a "⏳ ..."
    # heads-up once the answer is ready). Telegram only lets a bot delete its
    # own messages in a private chat, and only within 48 hours.
    def delete_message(self, message_id: int) -> None:
        if not self._settings.chat_id:
            raise TelegramNotConfigured("TELEGRAM_CHAT_ID is missing: run `uv run uni-copilot telegram-chat-id`.")
        self._call("deleteMessage", {"chat_id": self._settings.chat_id, "message_id": message_id})

    # Asks Telegram who this bot is (`getMe`). Confirms the token works and
    # returns the bot's public username, e.g. "unicopilot_thyme_bot".
    def get_me(self) -> str:
        return self._call("getMe", {})["username"]

    # Setup helper: the private chats that recently messaged the bot, as
    # (chat id, first name) pairs. One `getUpdates` call that returns at once
    # (timeout 0) and, without an `offset`, doesn't mark anything as read,
    # so the plugin still gets the same messages later.
    # 409 means another program (the plugin) is polling: stop it first.
    def find_chat_ids(self) -> list[tuple[str, str]]:
        try:
            updates = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        except TelegramError as e:
            if e.status == 409:
                raise TelegramError(
                    "getUpdates", 409, "another program is reading this bot's messages (is the Telegram plugin running?)"
                ) from None
            raise
        chats: dict[str, str] = {}
        for update in updates:
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                chats[str(chat["id"])] = chat.get("first_name", "")
        return list(chats.items())

    # Sends one Bot API call (a POST with a JSON body) and returns its "result".
    # Retries 429 (waiting as long as Telegram says), 5xx and network errors.
    # Never logs the URL (it contains the token); errors carry Telegram's
    # description with the token scrubbed out, and `from None` stops Python
    # from attaching the original exception, whose text includes the URL.
    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{API_BASE}/bot{self._settings.token}/{method}"
        retries = 0
        while True:
            try:
                r = self._session.post(url, json=payload, timeout=REQUEST_TIMEOUT_S)
            except requests.RequestException as e:
                if retries < MAX_RETRIES:
                    retries += 1
                    self._backoff(method, type(e).__name__, retries, None)
                    continue
                raise TelegramError(method, None, type(e).__name__) from None

            body = self._json(r)
            if body.get("ok"):
                log.debug("%s -> %s", method, r.status_code)
                return body.get("result")

            retry_after = (body.get("parameters") or {}).get("retry_after")
            if (r.status_code == 429 or r.status_code >= 500) and retries < MAX_RETRIES:
                retries += 1
                self._backoff(method, str(r.status_code), retries, retry_after)
                continue
            reason = scrub(str(body.get("description") or ""), self._settings.token)
            raise TelegramError(method, r.status_code, reason)

    # The JSON body of a response, or {} if it isn't JSON (e.g. an HTML error
    # page from a proxy), so the caller still sees the status code.
    @staticmethod
    def _json(r: requests.Response) -> dict[str, Any]:
        try:
            body = r.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    # Waits before a retry: as long as Telegram asked (retry_after), else 2, 4, 8 s.
    def _backoff(self, method: str, why: str, attempt: int, retry_after: Any) -> None:
        wait = float(retry_after) if isinstance(retry_after, (int, float)) else 2.0**attempt
        log.info("%s -> %s, retry %d/%d in %.0f s", method, why, attempt, MAX_RETRIES, wait)
        self._sleep(wait)


# Self-test: uv run python -m uni_copilot.notify.telegram               (offline, nothing sent)
#            uv run python -m uni_copilot.notify.telegram --check       (live: getMe, prints the bot's username)
#            uv run python -m uni_copilot.notify.telegram --send-test   (live: one message to your chat)
# Never prints the token or the chat id.
def _main() -> None:
    parser = argparse.ArgumentParser(description="Self-test of the Telegram sender.")
    parser.add_argument("--check", action="store_true", help="live: check the token with getMe")
    parser.add_argument("--send-test", action="store_true", help="live: send one test message to your chat")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.check or args.send_test:
        settings = load_telegram_settings(need_chat_id=args.send_test)
        bot = TelegramBot(settings)
        print("Bot username:", bot.get_me())
        if args.send_test:
            title = escape("Made-up Lab <draft> & notes")
            sent = bot.send_message(f"<b>uni-copilot test</b>\nA title with odd characters: {title}", html=True)
            print(f"Sent: {sent.parts} message(s), {sent.plain_fallbacks} resent as plain text. Check your phone.")
        return

    print("1) Settings in .env (values never shown):")
    try:
        settings = load_telegram_settings(need_chat_id=False)
        print("   token: found and looks right")
        print("   chat id:", "found" if settings.chat_id else "not set yet (step 2)")
    except TelegramNotConfigured as e:
        print("   problem:", e)

    print("\n2) Splitting a long message (limit 4096):")
    lines = [f"line {n}: " + "x" * 90 for n in range(100)]
    parts = split_message("\n".join(lines))
    print(f"   100 lines of ~100 characters -> {len(parts)} parts, sizes {[telegram_length(p) for p in parts]}")
    print(f"   one 10 000-character line -> {len(split_message('y' * 10_000))} parts")
    print(f"   emoji count double: telegram_length('ok 👍') = {telegram_length('ok 👍')}")
    print(f"   empty text -> {split_message('  \n ')}")
    boxes = "<blockquote>a\nb</blockquote>\n<blockquote>c\nd</blockquote>"
    print(f"   two 2-line boxes, limit 45, html=True:  {split_message(boxes, 45, html=True)}")
    print(f"   the same as plain text (cuts a box):    {split_message(boxes, 45)}")

    print("\n3) Escaping other people's text, and the plain-text fallback:")
    title = "Lab <draft> & notes"
    formatted = f"<b>IE2108</b> {escape(title)}"
    print(f"   escaped:   {formatted}")
    print(f"   plain:     {strip_html(formatted)}")

    print("\n4) Scrubbing the token out of an error:")
    fake = "123456:FAKE-token"
    print(f"   {scrub(f'error at https://api.telegram.org/bot{fake}/sendMessage', fake)}")
    print(f"   settings repr: {TelegramSettings(token=fake, chat_id='42')}")


if __name__ == "__main__":
    _main()
