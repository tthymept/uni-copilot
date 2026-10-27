# Phase 3: Telegram

Status (27 Oct 2026): steps 0–6 done, decisions made (below). Reminders are live: Task Scheduler runs `uni-copilot remind` every 30 minutes, in your layout; `uni-copilot done` marks things done. Next: step 7 (`send_telegram` and `mark_done` for Claude). Full details of every step: `phase-3-build-log.md` in this folder.

## What Phase 3 builds

Phase 2 gave uni-copilot a memory (the event store), but you still have to ask it. Phase 3 makes it **come to you**, on your phone, through one Telegram bot:

- **`uni-copilot notify "<text>"`:** sends a Telegram message to your own chat. A plain script, no AI. Everything else that pushes uses the same code (`notify/telegram.py`).
- **`uni-copilot remind`:** a plain script, no AI, run every 30 minutes by Task Scheduler. It looks in the store for deadlines you haven't handed in that have come within 24 hours or 2 hours of being due, sends one Telegram message, and writes down what it sent (new table `notifications_sent`) so nothing is ever sent twice. Most runs send nothing.
- **Morning digest:** Claude, every day at 07:30, reads `whats_new` for the `telegram` channel. Nothing changed → no message. Something changed → a short digest (new announcements in one line each, moved or edited deadlines, what's due today and tomorrow, logistics for tests) sent to Telegram.
- **Ask from Telegram:** the official Claude Code Telegram channel plugin connects a Claude Code session on your PC to the same bot. You type "what's due this week?" on your phone; Claude answers from `pilot` (and the `weekly-actions` skill), with "as of", like in the terminal.
- **`mark_done`:** for work handed in outside NTULearn (Gradescope, Google Forms), so its reminders stop.

How it fits with Phases 1 and 2:

```
NTULearn ──► uni-copilot sync (every 2 h) ──► SQLite event store
                                                 │
            ┌────────────────────────────────────┼─────────────────────────────────────┐
            ▼                                    ▼                                     ▼
 uni-copilot remind (every 30 min)     morning digest (07:30 daily)           pilot-mcp ◄── Claude Code session
 script, no AI                         Claude + morning-digest skill               │        + Telegram channel plugin
            │                                    │                                 │              ▲
            └──────────► notify/telegram.py ◄────┘                                 │              │ your questions
                         (sendMessage only)                                        │              │ and Claude's replies
                                │                                                  │              │
                                ▼                                                  ▼              ▼
                          Telegram Bot API ─────────────────────────────────────► your phone (one bot, one chat)
```

Done when (from the plan): **you get a correct digest and reminders for a week without opening NTULearn.** At the end of the week you compare with NTULearn: nothing was missed, nothing came twice, nothing came for work you'd already handed in.

## One bot, two directions

A Telegram **bot** is an account run by a program instead of a person. You create it by chatting with Telegram's own **BotFather** bot, which gives you a **token** (the bot's password: whoever has it can send as your bot). Programs talk to Telegram through the **Bot API**: plain HTTPS requests to `api.telegram.org`.

- **Push (our code):** `notify/telegram.py` calls `sendMessage` with the token and your **chat id** (the number of your private chat with the bot). That's one HTTPS POST to Telegram, never to NTULearn, so the "GET only on NTULearn" rule is untouched: NTULearn requests still go only through `client/http.py`, and Telegram gets its own small `requests` session.
- **Q&A (the plugin):** the Telegram channel plugin keeps asking Telegram "any new messages for my bot?" (`getUpdates`, called **long polling**) and passes your messages into a running Claude Code session. Claude's replies go back through the plugin.
- **Only one program may poll a bot.** If two programs call `getUpdates`, Telegram rejects one (error 409). So our code never polls, except one small helper run once during setup (to find your chat id) before the plugin is turned on. Sending (`sendMessage`) doesn't conflict with the plugin.

**Which part is a script and which is Claude** (the plan's rule: deterministic work as scripts, judgement in Claude):

- Reminders: script. Exact times, 48 checks a day, 0 Claude tokens, works the same every time.
- Digest: Claude. Deciding what's worth one line, summarising a long announcement, spotting "bring your calculator" in a post.
- Q&A: Claude, the same as in the terminal.

**How often reminders come.** `remind` checks every 30 minutes, but each open deadline gets at most two messages: one when it comes within 24 hours, one when it comes within 2 hours. Example, IE2108 Lab 4 due Wednesday 23:59: "due within 24 h" around Tuesday 00:10, nothing for the next ~90 checks, "due within 2 h" around Wednesday 22:10, or no second message if you've handed it in and the next sync has seen it. Only two other things send: a deadline that **moves** (reminded again for its new time), and the sync alert (once per failure streak). The 30-minute check is there so the 2-hour reminder really arrives 1.5–2 hours before; each check only reads the local database.

**Bookmarks:** Phase 2 already has a "last seen" bookmark per channel (`terminal`, `telegram`). The digest and Telegram Q&A both use `telegram`, so a change you saw in the morning digest doesn't show up again when you ask "what's new?" on Telegram; the terminal keeps its own bookmark.

## Steps

One file per step. You test each step before the next, like Phases 1 and 2.

0. Housekeeping: decisions recorded in the plan doc and here, `phase-3-build-log.md` created, `.env` confirmed git-ignored, Claude Code version checked (2.1.310). **You:** create the bot with BotFather and type its token into `.env` yourself (never into the chat with Claude), and install Bun for the plugin. **Done** (bot `unicopilot_thyme_bot`; Bun not confirmed yet, needed by step 11).
1. `src/uni_copilot/notify/telegram.py`: reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `.env` (a small standard-library reader; real environment variables win), and `send_message(text, html=False)`: link previews off, long texts split at Telegram's 4096-character limit (at line breaks), waits and retries on 429 (Telegram says how long) and 5xx. With `html=True`, Telegram's HTML formatting is on (`<b>`, `<i>`, `<a>`), and if Telegram still rejects the formatting, the message is resent as plain text with the tags removed, so it's never lost. The token is in Telegram's URL, so every error and log line has it scrubbed out. Also `find_chat_ids()`, the one-off setup helper (one `getUpdates` call). Offline self-test (splitting, scrubbing, tag removal) + a live `--check` (getMe); `--send-test` needs the chat id from step 2. **Done.**
2. `src/uni_copilot/cli.py`: `uni-copilot notify "<text>"` (or `notify -` to read the text from stdin, easier for long text in PowerShell; plain text) and `uni-copilot telegram-chat-id` (setup: you message the bot once, it prints the chat id and first name). Test: a message arrives on your phone. **Done** (26 Oct: plain and HTML messages arrived, escaping verified).
3. `src/uni_copilot/store/db.py`: schema version 2 (the Phase 2 migration system, `PRAGMA user_version`): `notifications_sent` (`event_id`, `reminder` = 24h / 2h, `due_at`, `sent_at`) and `marked_done` (`event_id`, `marked_at`), plus small helpers to read and write them. Your existing events and diary stay as they are. (Changed while building: the sync alert's "already sent for this failure streak" note goes in the `state` table, step 4, because every `notifications_sent` row points at a real deadline.) Test: self-test, then `uni-copilot status` on your real store. **Done** (real store upgraded 26 Oct, backup kept).
4. `src/uni_copilot/store/reminders.py`: **the heart of Phase 3.** Given "now", picks the reminders due: open deadlines (not submitted, not marked done, not removed) that have come within 24 h or 2 h, never twice for the same deadline, tier and due time (so a moved deadline is reminded again at its new time). No quiet hours: a reminder goes out on the first check after it's due, day or night. Formats one message per run, grouped ("Due within 2 h", "Due within 24 h"), soonest first, ending with "as of <last sync> SGT". Formatting: our own words and the course code / due time in bold; every title from NTULearn is escaped first (so `<`, `&` in a title show as written and can't break the message); a tag never spans two lines, so splitting can't cut one. Also the sync alert. No sending here, so it's fully testable with a fake clock. Offline self-test. **Done.**
5. `src/uni_copilot/cli.py`: `uni-copilot remind` (and `--dry-run`, which prints the message instead of sending): pick → send → record in `notifications_sent` **only after Telegram accepted it** (a failed send is retried next run instead of being lost). Exit code 1 if sending failed. Plus `uni-copilot done <event_id>` (and `--undo`). Test: `remind --dry-run` against your real store. **Done** (also: `done` takes words like "ie2108 lab 4"; the message got your layout with 🔴/🟡 headings and one box per deadline; `status` and `events` show reminders sent and marked-done items).
6. `scripts/schedule-remind.ps1`: Task Scheduler runs `uni-copilot remind` every 30 minutes, all day, at :10 and :40 (so during the day it runs just after the sync at :00 has finished). Same switches as `schedule-sync.ps1` (`-DryRun`, `-RunNow`, `-Wake`, `-Remove`); output in `.data\remind.log`. Sync still runs 07:00–23:00, so night-time reminders use the 23:00 data and say so. **Done** (registered and running since 27 Oct).
7. `src/uni_copilot/pilot_mcp/server.py` (+ `models.py`, `store/queries.py`): `send_telegram(text)` (plain text, only ever to your own chat id, length-checked) and `mark_done(event_id, undo=False)`. Items you marked done show as "done (marked by you)" in `list_events`. Server instructions: a question that came from Telegram uses `channel="telegram"` and gets a short plain-text answer; `mark_done` only after you asked and confirmed. Test: self-test and the MCP Inspector.
8. `.claude/settings.json`: pre-allow `send_telegram` and `mark_done`, so the digest and Telegram questions don't stall on a permission prompt nobody is there to click.
9. `.claude/skills/morning-digest/SKILL.md`: the digest workflow: `whats_new(channel="telegram")` → nothing changed → stop, send nothing; changed → a short digest (titles, course codes, times, links; one line per announcement; tests today/tomorrow with what to bring, marked as inferred, with the source post) → `send_telegram` → `mark_seen(channel="telegram", up_to=checked_at)`. Late-run rule: after 12:00, only today and tomorrow. Test: run the skill by hand in the terminal.
10. `scripts/schedule-digest.ps1`: Task Scheduler runs Claude Code headless (`claude -p`, explained in the build log) every day at 07:30 with only the pilot tools allowed; output in `.data\digest.log`. Fallback if headless runs misbehave: a Claude Desktop local routine.
11. `CLAUDE.md` + `scripts/start-telegram.ps1`: Telegram Q&A. Install the plugin (`/plugin install telegram@claude-plugins-official`), give it the token, pair your phone, set `/telegram:access policy allowlist` (only you can talk to it). The start script opens the session with `claude --channels plugin:telegram@claude-plugins-official` in the project folder; you restart it about once a day. `CLAUDE.md` gets "Answering from Telegram" (short, plain text, `telegram` bookmark, no grades). Test: ask "what's due this week?" from your phone.
12. `tests/`: notify against a fake Telegram server (splitting, 429 retry, the plain-text fallback, the token never appears in an error), migration 1 → 2 keeps your data, reminders (24 h and 2 h windows, no double sends, a moved deadline reminds again, submitted / marked done / removed items skipped, a failed send is retried, titles with `<` and `&` escaped, the sync alert once per failure streak and not for the planned night gap), `remind --dry-run`, `done`, pilot `send_telegram` and `mark_done`. All synthetic.
13. Final check (the done test): one week of digests and reminders without opening NTULearn, then compare with NTULearn. Plan doc and logs updated, and the plan's Telegram open questions answered.

Commit status: Phase 2 is committed up to `a8ec7c0`. The Phase 2 docs wrap-up (staged) and Phase 3 step 0 go in the next commit.

## Decisions

Made 26 Oct 2026: all as recommended, except no quiet hours (4) and formatting in script messages (10). Also recorded in the plan doc.

1. **The digest sends with a new `pilot` tool, `send_telegram(text)`.** It can only send to your own chat id (set in `.env`, never chosen by Claude), so even if a post tricked Claude, the worst case is a message to yourself. Pre-allowed. (Not chosen: Claude running `uv run uni-copilot notify` through the shell, which would need a broader shell allow rule.) `uni-copilot notify` still exists for scripts and testing; both use `notify/telegram.py`.
2. **Task Scheduler runs the digest with headless Claude Code (`claude -p`),** on your Claude plan, no API key. All three schedules (sync, remind, digest) live in `scripts\` and log to `.data\`, and no app has to be open. (The plan said a Claude Desktop routine; that's the fallback if headless runs misbehave in step 10.)
3. **The digest runs every day at 07:30 and sends only if something changed** since the last Telegram look. Weekends included: it's silent when nothing changed. Deadlines due today are covered by reminders.
4. **Reminders 24 h and 2 h before, with no quiet hours** (your choice): a reminder goes out on the first check after it's due, day or night. So a 23:59 deadline's 24 h reminder comes around midnight the night before, and an 08:00 deadline's 2 h reminder around 06:00. You can mute the bot on your phone. `remind` runs all day; sync still runs 07:00–23:00.
5. **One message per run,** listing everything due, grouped by tier, soonest first.
6. **Tests are reminded like any deadline;** the logistics ("bring a calculator", "venue LT2A") come from the AI digest, which reads the announcements, marks them as inferred and names the source post. (Not chosen: an evening "test tomorrow" script message found by title words, which couldn't include logistics.)
7. **`mark_done` is in Phase 3:** a separate `marked_done` table (sync never overwrites it; NTULearn's status stays as it is), `uni-copilot done <event_id>` / `--undo` in the terminal, and `pilot.mark_done` for Telegram. Pre-allowed because it only changes your local store and can be undone, but Claude must confirm with you in the chat first, and only when you asked (never because a post said so).
8. **Old data and a failing sync:** every reminder ends with "as of <last sync> SGT". When a sync attempt **fails** and the last successful one is more than 6 hours old, remind sends one alert ("sync failing since 14:00: run `uni-copilot sync` or the `login` tool"), once per failure streak. The planned night gap (no syncs 23:00–07:00) never triggers it, because it counts only failed attempts. (Changed after decision 4: with remind running at night, an age-only rule would alert every night around 05:00.) Not done: remind checking NTULearn live before a 2 h reminder, which would need a browser sign-in every 30 minutes; remind stays store-only.
9. **Token and chat id in the repo's `.env`** (git-ignored): `TELEGRAM_BOT_TOKEN=…`, `TELEGRAM_CHAT_ID=…`, read by a small standard-library reader (real environment variables win). The plugin keeps its own copy of the token in its own folder. The chat id comes from `uni-copilot telegram-chat-id` (step 2), run once before the plugin is set up. (Not chosen: reading the plugin's config file, whose location and format aren't verified; `python-dotenv`, not needed for a few lines.)
10. **What goes into pushed messages** (Telegram bot chats aren't end-to-end encrypted): titles, course codes, times in SGT, NTULearn links, one line per announcement. No sender names, no full bodies, never grades. Link previews off. When you ask on Telegram, Claude can say more, like in the terminal. **Formatting (your choice, "format now"):** script messages (reminders, the sync alert) use Telegram's HTML mode: only our code adds tags (bold, links, italics and one `<blockquote>` box per deadline, your layout from step 5), every piece of text from NTULearn is escaped first, and if Telegram still rejects the formatting, the message is resent as plain text so it's never lost. `send_telegram` (the digest, written by Claude) stays plain text: asking Claude to escape other people's text correctly every time is where mistakes would happen. Can be revisited after the week of use.
11. **You start the Telegram Q&A session when you want it** with `scripts\start-telegram.ps1` (a terminal stays open running `claude --channels …`), and restart it about once a day so its conversation stays short. Starting it automatically at Windows logon can come later, once it's proved stable.

## Things to check during Phase 3

Not verified yet; each is checked in the step named.

- **Bun on Windows and the plugin (steps 0, 11):** channels are a research preview and the plugin needs Bun. Not tested on Windows here.
- **Giving the plugin the token (step 11):** `/telegram:configure <token>` types the token into the Claude Code chat. Check whether the plugin can read it from its own `.env` file instead, so the token never passes through a conversation.
- **Permission prompts from Telegram (step 11):** what happens when a Telegram question needs a tool that isn't pre-allowed (e.g. `login`). Does the prompt reach your phone, or does the session wait at the PC?
- **Claude picks pilot and `weekly-actions` from Telegram (steps 11, 13):** verified in the terminal in Phase 2, not yet from Telegram (open question in the plan).
- **Inline buttons and the plugin's reply tool (step 11):** open questions in the plan. The design doesn't need either: confirmations are plain replies ("yes"), and pushes go through our own `sendMessage`.
- **`claude -p` from Task Scheduler (step 10):** runs without a window, loads the project's MCP servers, respects `.claude/settings.json`, and finishes within a time limit. Not tested.
- **Telegram's HTML mode with real titles (steps 1, 4):** escaping should cover everything; the plain-text fallback is there in case it doesn't. Step 13's week will show whether the fallback ever fires (it's logged).
- **Digest after sleep (steps 10, 13):** if the PC sleeps through 07:30, the digest runs late; the late-run rule handles it. `-Wake` behaviour is still an open question from Phase 2.
- **Sessions with the sync 2 hours apart (Phase 2 open question):** the sync alert (decision 8) will show if sign-in starts failing.

## New ideas you'll meet

Explained in the build log when they first appear, tied to the code:

- **Bot, BotFather, token, chat id:** what a Telegram bot is and how it's identified.
- **Bot API, `sendMessage`:** sending a message is one HTTPS request.
- **Long polling, `getUpdates`, and why only one program may poll.**
- **Parse mode and escaping:** how Telegram reads `<b>` as bold, and why other people's `<` must become `&lt;` first.
- **`.env` file:** settings and secrets kept outside the code, never committed.
- **"Send once":** a reminder table keyed by event, tier and due time, so re-running `remind` is safe (idempotent, like sync).
- **Record after success:** why `notifications_sent` is written only after Telegram accepts the message.
- **Claude Code channels and plugins:** how a phone message reaches a Claude Code session on your PC.
- **Headless Claude Code (`claude -p`):** one request, no window, for scheduled jobs.
- **Bun:** the JavaScript runtime the plugin runs on.
