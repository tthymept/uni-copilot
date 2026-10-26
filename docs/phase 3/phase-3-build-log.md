# Phase 3 build log

What each finished step changed, how to test it, and its commit. Unverified points from each step are collected in the "Open items" section at the bottom, and new terms in the "Concepts" section. The short overview is `phase-3-core.md` in this folder. Phase 2 (the event store this phase reads from) is in `docs/phase 2/`.

## How it all connects (read this first when you come back)

### The layers

```
Task Scheduler
  ├─ every 2 h, 07:00–23:00: uni-copilot sync ──► SQLite event store (Phase 2)
  ├─ every 30 min, all day:  uni-copilot remind ──► store/reminders.py ──┐
  │                           (script, no AI)       reads the store,     │
  │                                                 notifications_sent   │
  └─ daily 07:30:            claude -p ──► morning-digest skill          │
                              (headless)    └─ pilot.whats_new ──► pilot.send_telegram
                                                                         │
                                                                         ▼
                                                   notify/telegram.py ── sendMessage ──► Telegram ──► your phone
                                                                                            ▲
you on your phone ──► Telegram ──► channel plugin ──► Claude Code session (start-telegram.ps1)
                                                        └─ pilot tools (+ weekly-actions skill) ──► reply
```

Restaurant version, continuing Phase 2's: `remind` is a **kitchen timer** that rings 24 h and 2 h before each order is due, once each, and never for an order already served. The digest is the **manager's morning note**: only written when something changed overnight. Telegram is the **phone line** between you and the restaurant: you can call in with questions, and the restaurant can text you.

### One bot, two directions

- **Push:** our code sends with `sendMessage` (one HTTPS POST to `api.telegram.org`, never to NTULearn).
- **Q&A:** the plugin polls with `getUpdates` and feeds your messages into a Claude Code session. Only one program may poll a bot, so our code never polls (except the one-off chat-id helper, before the plugin is set up).

## Step 0: housekeeping

- **Decisions made (26 Oct):** all as recommended in `phase-3-core.md`, except:
  - **4: no quiet hours.** Reminders go out day or night; `remind` runs every 30 minutes all day. Sync still runs 07:00–23:00, so night-time reminders use the 23:00 data and say "as of 23:00".
  - **10: formatting now.** Script messages (reminders, the sync alert) use Telegram's HTML mode with bold for the course code and due time. Only our code adds tags, every title from NTULearn is escaped first, and a rejected message is resent as plain text. The digest (`send_telegram`, written by Claude) stays plain text.
  - **Follow-on change to 8:** the sync alert counts only **failed** sync attempts (last attempt failed and the last success is over 6 h old). With remind running at night, a rule based only on the data's age would alert every night around 05:00, during the planned gap with no syncs.
- **`docs/phase 3/phase-3-core.md`:** decisions marked as made; steps 1, 4, 6, 7, 9 and 12 updated for no quiet hours and HTML formatting; "How often reminders come" added (your question: `remind` checks every 30 minutes, but sends at most two messages per deadline).
- **`docs/uni-copilot-project-plan.md`:**
  - Status line: Phase 3 started, pointing to this folder.
  - Section 5.1: `notifications_sent` (key: event, tier, due time; written after Telegram accepts) and the new `marked_done` table.
  - Section 5.2: `remind` (all day, no quiet hours, sync alert, store-only), `notify` (plain text, stdin), `done`, `telegram-chat-id`.
  - Section 5.3: `mark_done` and `send_telegram` marked Phase 3 with their rules.
  - Section 6: the digest runs by Task Scheduler + headless `claude -p` (was a Claude Desktop routine, now the fallback), daily; reminders day or night. "Test tomorrow: bring matric card" moved from the script to the digest (logistics need reading posts).
  - Section 7: repo layout (three new scripts, `phase 3/` docs), wiring (`.env` names, message format, schedules, the Q&A session), the digest request flow.
  - Roadmap and open questions: Phase 3 started; plugin on Windows, the token, permission prompts, headless `claude -p`.
- **This log:** created, in the same format as the Phase 2 log.
- **`.gitignore`:** `.env` is already ignored (Phase 1 step 0), so the bot token can never be committed. `.env` doesn't exist yet.
- **Claude Code:** 2.1.310. **Bun:** not installed yet (your part, below).

**Your part (before step 1):**

1. **Create the bot.** In Telegram, open a chat with **@BotFather** (the blue tick one), send `/newbot`, pick a display name (e.g. "uni-copilot") and a username ending in `bot` (e.g. `thyme_unicopilot_bot`; it must be unique). BotFather replies with the token: a line like `123456789:AA...`. Treat it like a password.
2. **Put the token in `.env`.** Create a file named `.env` in the project folder (next to `pyproject.toml`) with one line: `TELEGRAM_BOT_TOKEN=<the token>`. Type or paste it there yourself; don't paste it into Claude. `TELEGRAM_CHAT_ID` is added in step 2, with the helper.
3. **Send your bot one message** ("hi"): open `t.me/<your bot's username>` and press Start. A bot can't message you until you've messaged it first. Step 2's helper reads that message to find your chat id.
4. **Install Bun** (needed in step 11 for the plugin; doing it now finds problems early). In PowerShell: `powershell -c "irm bun.sh/install.ps1 | iex"`, then open a new terminal.

Check (PowerShell):

```
git check-ignore -v .env
bun --version
```

Expect the first to print the `.gitignore` line for `.env`, and the second a version number. Tell me the bot's **username** (not the token) when you're done.

Commit (the Phase 2 docs wrap-up, already staged, plus this step):

```
git add docs
git commit -m "docs: wrap up phase 2 and plan phase 3"
```

## Open items

Everything not yet verified in Phase 3. Updated as steps finish.

- **Bun and the plugin on Windows** (step 11).
- **Giving the plugin the token without typing it into a chat** (step 11).
- **Permission prompts from Telegram** (step 11): does a prompt reach the phone?
- **Claude picks pilot and `weekly-actions` from Telegram** (steps 11, 13).
- **Headless `claude -p` from Task Scheduler** (step 10).
- **Telegram's HTML mode with real titles** (steps 1, 4, 13): does the plain-text fallback ever fire?
- **Digest after sleep** (steps 10, 13).

## Concepts

Added as steps introduce them.
