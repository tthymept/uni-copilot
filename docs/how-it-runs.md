# How uni-copilot runs: what happens when, and when the PC is off

Status: 30 Oct 2026 (Phase 3 test week; Phase 4 indexer added, run by hand). All times are Singapore time (the PC's clock).

## The short version

- **Three jobs run by themselves** from Windows Task Scheduler: **sync** (fetch NTULearn), **remind** (deadline reminders) and **digest** (the morning summary). No Claude Code window needs to be open for them.
- **One thing needs a window open:** asking questions from Telegram (the session started by `scripts\start-telegram.ps1`).
- **Nothing runs while the PC is off, asleep or signed out.** When it's back, each job catches up at its next run. No reminder is sent twice and the digest comes at most once a day.
- **The scheduler only pokes; the code decides.** Each job checks the store ("what did I already do?") and acts only if something is due. That's why missed runs don't break anything.

## The jobs

### 1. sync: fetch NTULearn into the store

- **Runs:** every 2 hours from **07:00 to 23:00**: 07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00, 23:00 (9 a day). **No syncs between 23:00 and 07:00** (by design).
- **Takes:** about 1 minute (about 60 read-only requests to NTULearn).
- **Does:** fetches deadlines, announcements and messages, saves them, writes what changed in the diary.
- **No AI**, no Claude tokens.
- **One at a time:** if another sync is already running (e.g. the digest's), this one skips.
- **Log:** `.data\sync.log`
- **If it fails** (e.g. signed out): old data is kept. After 6 hours of failed tries you get a ⚠️ sync alert (see remind).

### 2. remind: deadline reminders

- **Runs (is poked):** every 30 minutes, **all day and night**, at **:10 and :40** (00:10, 00:40, … 23:40; 48 a day).
- **Takes:** 1–2 seconds. Reads the store only; **never contacts NTULearn**. No AI.
- **Sends**, at most one Telegram message per run, only if something is due:
  - **🟡 within 24 hours:** an open deadline (not handed in on NTULearn, not marked done by you) that came within 24 h of being due.
  - **🔴 within 2 hours:** the same, within 2 h.
  - **⚠️ sync failing:** the last sync try failed and the last success is over 6 hours old. Once per failure streak.
- **Each reminder only once** (remembered in the store). A deadline that **moves** gets reminded again for its new time.
- **No quiet hours:** a reminder that becomes due at 03:00 is sent at 03:10.
- **Log:** `.data\remind.log` (most lines say `Nothing to send.`)

### 3. digest: the morning summary

- **Is poked:** every 30 minutes at **:05 and :35** (00:05 … 23:35; 48 a day). Almost every poke does nothing.
- **Acts once a day, at the first poke after 07:30**, so normally **07:35**:
  1. If the store is **older than 3 hours** (the PC was asleep), it **syncs first** (about 1 minute), or waits for a sync that's already running.
  2. Claude Code runs headless (no window, locked to the 5 pilot tools) with the `morning-digest` skill.
  3. The skill sends a digest **only if something changed** since you last looked on Telegram: new or edited announcements and messages, new, moved or removed deadlines. If nothing changed, **no message** (the log says `Nothing new ... no digest sent.`).
- **Takes:** about 30–60 seconds (plus about 1 minute if it syncs first). Uses a little of your Claude plan, once a day.
- **"Done for today"** is saved after a successful run, including "nothing new". A failed run is tried again at the next poke, **at most 3 times a day**.
- **Log:** `.data\digest.log` (`Not yet: the digest runs after 07:30.` before 07:30, `Already done today.` after)

### 4. Telegram Q&A (not scheduled)

- **Runs:** only while the window from `scripts\start-telegram.ps1` is open. Restart it about once a day.
- **Closed?** Messages you send wait at Telegram for up to 24 hours and are answered when you start it again.
- Locked to study questions: no shell, files, web or sign-in from Telegram.

### 5. The content indexer (Phase 4, by hand, not scheduled)

- **Runs:** only when you start it: `uv run python -m uni_copilot.ntulearn_mcp.indexer` (`--dry-run` first shows what it would read and how long). It reads every course's slides, PDFs, notebooks and text files into `.data\cache\content.sqlite`, which `search` uses. Data files (`.csv`) are left out.
- **First run** ~1 hour (1 request every 2 s); later runs only read what's new or changed, usually a few minutes. Safe to stop with Ctrl+C or to sleep the laptop: everything read is kept, and the next run carries on.
- **Not run?** Search still works on what was read before; its answers say how many files are covered and when the indexer last finished. Files you open with `read_file_text` are added on the way.
- Whether to schedule it (poke often, decide in code) is decided after the test week.

## A normal day with the PC on all day

| Time | What happens |
|---|---|
| 00:05 – 07:05 | digest pokes: `Not yet` |
| 00:10 – 06:40 | remind every 30 min (reminders use the 23:00 data) |
| **07:00** | **sync** (first of the day, ~1 min) |
| 07:05 | digest poke: `Not yet` (before 07:30) |
| 07:10 | remind |
| **07:35** | **digest**: store is fresh from 07:00 → Claude → digest on your phone, or nothing if nothing changed |
| 07:40 | remind |
| 08:05, 08:35, … | digest pokes: `Already done today` |
| 09:00, 11:00, … 23:00 | sync every 2 h |
| every :10 / :40 | remind (sends only when a deadline enters 24 h or 2 h, or the sync alert is due) |
| 23:00 → 07:00 | no syncs; reminders keep running on the 23:00 data |

## When the PC is off or asleep

**While it's off:** nothing runs. Nothing is lost either: NTULearn keeps everything, and the store remembers what was already done.

**When you open it**, two things can start the jobs again:

1. **Windows' catch-up:** each task has "run as soon as possible after a missed start", so Windows starts a missed run after waking, sometimes at once, sometimes a few minutes later (29 Oct: at 10:53 and again at 10:59:58). A late catch-up **takes the place of the next regular poke** (29 Oct: the 10:59:58 runs replaced remind's 11:10 and digest's 11:05).
   - **Known weak spot:** on 28 and 29 Oct the first catch-up started while the laptop was still flickering between standby and awake, and Windows cut off the sync and digest runs (`0x41306`).
2. **The next regular poke:** within 30 minutes for remind and digest; the next odd hour for sync.

**Example: you open the laptop at 10:54 (29 Oct):**

| Time | What happens |
|---|---|
| 10:53 | Windows' catch-up starts sync and digest, but they're cut off by the standby flicker |
| 10:54 | remind's catch-up runs: `Nothing to send` |
| 10:59:58 | a second, late catch-up of remind and digest (it replaces their 11:05 / 11:10 pokes). The digest sees an old store and waits for the sync... |
| **11:00** | **regular sync** (sync runs at odd hours: 07, 09, 11, …): fresh data, 0 changes. The digest reuses it instead of syncing twice |
| 11:01 | ...then can't start Claude: Claude Code was updating itself at that moment. Counted as try 1 of 3 |
| **11:35** | **next digest poke**: retries → digest, or nothing if nothing changed |
| 11:40 | remind, on fresh data |

**Why "11:00":** sync is scheduled at fixed odd hours (07:00, 09:00, 11:00, …). After waking at 10:54, the first regular sync is 11:00. If Windows' catch-up had worked, it would have synced at 10:53.

**Does sync catch up by itself?** Yes, in three ways:
- Windows' catch-up right after waking (when it isn't cut off);
- the next regular sync (at most 2 hours later, or 07:00 after a night);
- **the digest syncs first** whenever the store is more than 3 hours old, so a late digest is never built on yesterday's data.

Remind never syncs; it uses whatever the last sync saved.

## What happens to reminders you "missed"

A reminder isn't tied to an exact minute. It's **"the first remind run after the deadline came within 24 h (or 2 h)"**. So:

| Situation | What you get |
|---|---|
| PC on | 🟡 about 24 h before, 🔴 about 2 h before (within 30 min of the exact time) |
| PC asleep when the 24 h mark passed, back on more than 2 h before the deadline | 🟡 at the first remind run after you open it, then 🔴 at about 2 h before |
| PC back on less than 2 h before the deadline | only 🔴 (the 🟡 would say the same) |
| PC off until after the deadline | nothing: past deadlines aren't reminded |
| You handed it in on NTULearn | no more reminders **after the next sync** sees it (a reminder in between may still come; it says "Updated …" with the data's time) |
| You handed it in elsewhere (Gradescope) | reminders keep coming until you mark it done: `uv run uni-copilot done "<course> <title>"`, or reply on Telegram |
| The deadline moved | reminded again for the new time |

## Will there be a morning digest tomorrow?

- **If something changed** since your last Telegram look (new or edited posts, new, moved or removed deadlines): **yes**:
  - PC on: at **07:35**;
  - PC asleep: at the **first digest poke after you open it** (:05 or :35), after a sync if the store is old.
- **If nothing changed:** **no message.** That's on purpose (decision 3): a quiet day stays quiet. `digest.log` still shows that it ran (`Nothing new ... no digest sent.`).
- **Deadlines due today aren't a reason for a digest on their own:** the 🟡/🔴 reminders cover them.
- Status changes (submitted, graded) never appear in the digest.

## When is it "down"?

| Situation | sync | remind | digest | Telegram Q&A |
|---|---|---|---|---|
| PC on, you signed in | ✓ | ✓ | ✓ | ✓ if the window is open |
| 23:00 – 07:00, PC on | — (by design) | ✓ (23:00 data) | pokes only | ✓ if open |
| PC asleep / lid closed (not set to stay on) | ✗ | ✗ | ✗ | ✗ |
| PC on but signed out of Windows | ✗ | ✗ | ✗ | ✗ |
| PC on, no internet | fails (data kept; ⚠️ alert after 6 h) | ✓ but can't send | can't send (retried, 3 a day) | ✗ |
| NTULearn sign-in expired | fails (⚠️ alert after 6 h); fix: `login` tool at the PC | ✓ (old data) | ✓ (old data) | ✓ |
| Claude Code updating itself (seen 29 Oct 11:00: `claude.exe` briefly missing) | ✓ | ✓ | that try fails (`couldn't start`), retried at the next poke | restart the window afterwards |

## Keeping it on (optional)

With the charger in and two power settings, the laptop keeps running with the lid closed, so everything happens on time:

- Control Panel → Power Options → **Choose what closing the lid does** → **Plugged in: Do nothing**.
- Settings → System → Power & battery → **Make my device sleep after (plugged in): Never**.

On battery it still sleeps as usual. Don't put it in a bag while it's running.

**Proposed, not built yet:** an extra poke **1 minute after you unlock or sign in**, for digest and remind, so after a night asleep the digest arrives 1–3 minutes after you open the laptop, instead of at the next :05/:35, and Windows' flaky catch-up doesn't matter.

## All commands (the CLI)

A **CLI** (command-line interface) is a program you use by typing a command in a terminal (PowerShell, or the terminal in VS Code) instead of clicking. Task Scheduler runs the same commands by itself on a timer. Run them **in the project folder** (`D:\Thyme\Project\uni-copilot`).

How to read a command:

```
uv run uni-copilot remind --dry-run
│      │           │      └─ an option: "--" plus a word, adjusts the command
│      │           └─ the command: which job
│      └─ the program (src/uni_copilot/cli.py)
└─ "run it with the project's own Python"
```

- Options in `[brackets]` below are optional; `<angle brackets>` means "put your own value here".
- **Help on screen:** `uv run uni-copilot --help`, or for one command, e.g. `uv run uni-copilot events --help`.
- If `uv run` says a file is "used by another process" (a Claude Code session is running a server), use `uv run --no-sync ...` instead.

### Where the commands live in the code

**`src/uni_copilot/cli.py` is the front door, not where the work happens.** It reads what you typed (with Python's `argparse`, which turns typed words into options), calls the matching function (e.g. `cmd_remind`), prints the result, and handles `--log` and the exit code. The real work is in other files:

| Command | Real work in |
|---|---|
| `sync` | `store/sync.py` (uses the NTULearn clients in `ntulearn_mcp/client/`), saved by `store/events.py` |
| `status`, `events`, `changes` | `store/queries.py` and `store/db.py` |
| `remind` | `store/reminders.py` (who's due, the message) + `notify/telegram.py` (sending) |
| `done` | `store/db.py` (`set_marked_done`, `clear_marked_done`); the word matching is a small helper in `cli.py` |
| `notify`, `telegram-chat-id` | `notify/telegram.py` |
| `digest` | the decisions (after 07:30? done today? sync first?) in `cli.py`; the digest itself is written by **Claude** with `.claude/skills/morning-digest/SKILL.md`, using the `pilot` tools |
| the "one at a time" locks | `filelock.py` |

**Why it's split this way:** the same code serves several front doors. `store/queries.py` answers both `uni-copilot events` (you, typing) and `pilot.list_events` (Claude, through MCP); `notify/telegram.py` sends for `remind`, `notify` and Claude's `send_telegram`.

```
you typing / Task Scheduler ──► uni-copilot <command>  (cli.py) ──┐
                                                                 ├──► store/, notify/, filelock.py (the real work)
Claude ──► MCP tools (pilot_mcp/server.py, ntulearn_mcp/server.py)┘
```

**How typing `uni-copilot` reaches `cli.py`:** `pyproject.toml` has `uni-copilot = "uni_copilot.cli:main"`. `uv sync` made a small `uni-copilot.exe` in `.venv\Scripts` that calls `main()` in `cli.py`. The same file defines `ntulearn-mcp` and `pilot-mcp`, the two servers Claude Code starts.

**Not in `cli.py`:**

- **Self-tests** (`python -m uni_copilot.notify.telegram --check`, `... store.db --real`, ...): each file has its own small `_main()` at the bottom; `python -m <module>` runs that file directly.
- **Scheduler scripts and the Telegram start script:** PowerShell files in `scripts\`. They do no work themselves: they tell Windows when to run `uni-copilot ...`, or start Claude Code with the right settings.

### `uni-copilot` commands

**Options for any command** (they go *before* the command):

- `--log <file>`: add everything the command prints to the end of that file instead of the screen, with a time stamp. Task Scheduler uses it, e.g. `uv run uni-copilot --log .data\test.log status`.
- (`--db <file>`: use another database file. Hidden; used by the tests.)

**Read the store** (instant, never contacts NTULearn, changes nothing):

- **`uni-copilot status`:** how fresh the data is per source ("as of"), errors, how many events, diary lines, reminders sent and items marked done.
- **`uni-copilot events [--days N] [--kind K] [--course C]`:** deadlines due in the next N days and posts from the last N days.
  - `--days N`: how many days (default 7).
  - `--kind deadline` / `announcement` / `message`: only that kind; repeat it for several (`--kind deadline --kind message`).
  - `--course C`: part of a course code, any case (`--course ie2108`, `--course DA3`).
  - Deadlines you marked done show as `done*`.
  - Example: `uv run uni-copilot events --days 14 --kind deadline`
- **`uni-copilot changes [--days N]`:** the diary: what each sync found new, edited, moved, status changed or removed, in the last N days (default 7).

**Fetch from NTULearn:**

- **`uni-copilot sync`:** one sync now (GET only, about 1 minute). If another sync is already running, it skips. Exit code 1 if a source failed.

**Telegram:**

- **`uni-copilot notify "<text>"`:** send yourself a plain-text message. Use `-` instead of the text to read it from a pipe: `Get-Content note.txt | uv run uni-copilot notify -` (non-English text through a pipe may turn into `?` in Windows PowerShell).
- **`uni-copilot telegram-chat-id`:** setup only: shows the chat id of whoever messaged the bot recently. Doesn't work while the Telegram session is running (only one program may read the bot).

**Reminders:**

- **`uni-copilot remind [--dry-run]`:** send the reminders that are due right now (🟡 24 h, 🔴 2 h, ⚠️ sync failing), and remember them.
  - `--dry-run`: only show what it would send; sends and remembers nothing. Safe to run any time.
- **`uni-copilot done "<words or id>" [--undo]`:** mark a deadline done by you (handed in outside NTULearn), so its reminders stop.
  - Words must all appear in the course code + title: `uv run uni-copilot done "ie2108 lab 4"`. Several matches → it lists them with ids and marks nothing; then use the id: `uv run uni-copilot done "_1183012_1"`.
  - `--undo`: take your mark back.
  - Only upcoming deadlines; changes only your local store, never NTULearn.

**Digest:**

- **`uni-copilot digest [--force] [--claude <path>]`:** the morning digest. Normally Task Scheduler pokes it; it acts once a day after 07:30, syncs first if the store is over 3 h old, then runs Claude headless and sends only if something changed.
  - `--force`: run it now, even before 07:30 or if it already ran today.
  - `--claude <path>`: where `claude.exe` is (normally found automatically).

### Scheduler scripts (PowerShell)

Run with `powershell -ExecutionPolicy Bypass -File scripts\<name>.ps1 [switch]`. PowerShell switches start with one `-`.

- **`schedule-sync.ps1`:** the "uni-copilot sync" task (every 2 h, 07:00–23:00).
- **`schedule-remind.ps1`:** the "uni-copilot remind" task (every 30 min, :10/:40).
- **`schedule-digest.ps1`:** the "uni-copilot digest" task (every 30 min, :05/:35).

Each takes the same switches:

- (none): create the task, or update it.
- `-DryRun`: show what it would register; change nothing.
- `-RunNow`: create or update, then run it once right away.
- `-Wake`: also wake the PC from sleep for a run (plugged in; not every laptop supports it).
- `-Remove`: delete the task.

**`start-telegram.ps1`:** opens the Telegram Q&A session (keep the window open). Extra words are passed to Claude Code, e.g. `... -File scripts\start-telegram.ps1 --model sonnet`. Inside that session: `/exit` to close it, `/mcp` to see the servers, `/telegram:access ...` for the bot's access settings.

### Checks and self-tests

These are for checking that a part works. Most only read; the ones that send or fetch say so.

- **All offline tests:** `uv run pytest` (made-up data, nothing sent; expect "186 passed, 4 skipped").
- **Live smoke tests** (your account, GET only, counts only): `$env:NTULEARN_LIVE='1'; uv run pytest tests/test_live.py -s; Remove-Item Env:NTULEARN_LIVE`
- **Telegram sender:** `uv run python -m uni_copilot.notify.telegram`; add `--check` (asks Telegram who the bot is, sends nothing) or `--send-test` (sends one test message).
- **"💭 Thinking…" note:** `uv run python -m uni_copilot.notify.thinking demo` (posts the note, deletes it 5 s later).
- **pilot server:** `uv run python -m uni_copilot.pilot_mcp.server --self-test` (lists and calls its tools; nothing is marked or sent). `--db <file>` uses another store.
- **ntulearn server:** `uv run python -m uni_copilot.ntulearn_mcp.server --self-test` (live NTULearn, GET only, counts only).
- **Sign-in:** `uv run python -m uni_copilot.ntulearn_mcp.auth.session`; add `--window` to sign in in a visible window.
- **Store:** `uv run python -m uni_copilot.store.db` (in memory) or `... store.db --real` (your real file: version and row counts); `... store.reminders` (reminder logic on a made-up clock); `... store.sync` (a live sync into memory, or `--real` into your store).
- **MCP Inspector** (click through a server's tools in a browser): `uv run mcp dev src/uni_copilot/ntulearn_mcp/server.py`

## Where to look

| Question | Command or file |
|---|---|
| How fresh is the data? | `uv run uni-copilot status` |
| What would remind send right now? | `uv run uni-copilot remind --dry-run` |
| Send the digest now | `uv run uni-copilot digest --force` |
| Did a job run, and what did it say? | `.data\sync.log`, `.data\remind.log`, `.data\digest.log` |
| When does each task run next? | Task Scheduler app → Task Scheduler Library → `uni-copilot sync` / `remind` / `digest` |
| Re-register a task | `powershell -ExecutionPolicy Bypass -File scripts\schedule-<sync\|remind\|digest>.ps1 -RunNow` |
