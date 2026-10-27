"""
Posts "💭 Thinking…" in your Telegram chat the moment your message arrives,
and deletes it again once Claude says something.

In plain words:
  Claude can take a while before its first reply, and Telegram's "typing…"
  status didn't show in your Telegram (tried 27 Oct: sent fine, never shown).
  So instead, our own code posts a short "💭 Thinking…" note right away and
  removes it as soon as Claude's first message (the "⏳ ..." heads-up or the
  answer) is about to go out. Claude itself never has to think about it.

Who calls it: Claude Code hooks in .claude/telegram-session.json, which only
scripts/start-telegram.ps1 loads (so only the Telegram session does this):
  your message arrives (UserPromptSubmit)   `hook` -> post "💭 Thinking…"
  before the plugin's reply tool            `clear` -> delete it first
  when Claude finishes (Stop)               `clear` (safety net)
Only prompts that came from the Telegram channel post the note: text you type
into that session's terminal (e.g. /telegram:access ...) doesn't.

State: .data/thinking.msg holds the note's message id while it's showing.
.data/thinking.cleared holds the time of the last clear (see post()).
.data/thinking.log gets one line per post/delete (ids only, never text).

Commands:
  python -m uni_copilot.notify.thinking hook    (from the hook: reads its JSON on stdin)
  python -m uni_copilot.notify.thinking clear
  python -m uni_copilot.notify.thinking demo    (posts the note, deletes it 5 s later)
"""

import json
import sys
import time
from datetime import datetime

from uni_copilot.ntulearn_mcp.config import DATA_DIR
from uni_copilot.notify.telegram import TelegramBot, load_telegram_settings

NOTE = "💭 Thinking…"
MSG = DATA_DIR / "thinking.msg"
CLEARED = DATA_DIR / "thinking.cleared"
LOG = DATA_DIR / "thinking.log"
LOG_MAX_BYTES = 200_000

# How a Telegram channel message looks in the prompt the hook receives.
CHANNEL_MARK = "plugin:telegram"


# Adds one time-stamped line to .data/thinking.log (ids and reasons only).
def _log(text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
        LOG.unlink()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%d %b %H:%M:%S}  {text}\n")


# When clear() last ran (seconds since 1970), or 0 if never.
def _cleared_at() -> float:
    try:
        return float(CLEARED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0


# Posts "💭 Thinking…" and remembers its id. A note still showing from an
# earlier message is deleted first, so there's never more than one.
# The post hook runs in the background, so a quick reply (and its clear) can
# happen while the note is still on its way, leaving it stuck under the answer
# (seen 27 Oct). If a clear ran after we started, delete the note right away.
def post(bot: TelegramBot) -> None:
    clear(bot, "replaced")
    started = time.time()
    message_id = bot.post_note(NOTE)
    MSG.write_text(str(message_id), encoding="utf-8")
    _log(f"posted {message_id}")
    if _cleared_at() > started:
        clear(bot, "late")


# Deletes the note if one is showing, and records when this ran (for post()).
# Errors are logged and ignored (it may already be gone); the id file is
# removed either way.
def clear(bot: TelegramBot, reason: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLEARED.write_text(str(time.time()), encoding="utf-8")
    try:
        message_id = int(MSG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    MSG.unlink(missing_ok=True)
    try:
        bot.delete_message(message_id)
        _log(f"deleted {message_id} ({reason})")
    except Exception as e:  # noqa: BLE001 - a status note only
        _log(f"delete {message_id} failed ({reason}): {type(e).__name__}")


# Called by the hooks with their JSON on stdin: a Telegram message arrived ->
# post; anything else (Stop) -> clear.
def _hook(bot: TelegramBot) -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if event.get("hook_event_name") == "UserPromptSubmit":
        if CHANNEL_MARK in str(event.get("prompt", "")):
            post(bot)
        else:
            _log("prompt not from Telegram: no note")
    else:
        clear(bot, str(event.get("hook_event_name") or "?"))


# The command line: hook | clear | demo. Never fails loudly: a hook that
# errors would only show a warning, and the note is a nicety.
def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    try:
        bot = TelegramBot(load_telegram_settings())
    except Exception as e:  # noqa: BLE001 - not configured: nothing to do
        _log(f"not configured ({type(e).__name__})")
        return 0
    if command == "hook":
        _hook(bot)
    elif command == "clear":
        clear(bot, argv[2] if len(argv) > 2 else "clear")
    elif command == "demo":
        post(bot)
        print('Posted "💭 Thinking…"; deleting it in 5 s...')
        time.sleep(5)
        clear(bot, "demo")
        print("Deleted.")
    else:
        print("usage: python -m uni_copilot.notify.thinking hook | clear | demo")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
