# Phase 6: schedule (reading and planning)

Status (12 Nov 2026): **step 0 (the sign-in retry) built; being checked in `.data\sync.log`. Next: step 2, the calendar trial.** Phases 0–4 are done and in daily use. Phase 5 (recordings) is on hold, not cancelled. On 7 Nov the roadmap was reordered (your decision): **Phase 6 reads and plans your schedule; Phase 7 writes, with your permission** (Google Calendar events, forwarded messages). This phase first grew out of a handoff note for "Phase 7: more sources"; everything from it is in this file. A Kotlin part was discussed on 7 Nov and **dropped from this project for now** (your decision): Phase 6 stays Python and Claude. Steps 0a–0c (the sign-in retry) are done; nothing else is built. Before planning: last commit `2db3300`; `uv run --no-sync pytest` → 281 passed, 4 skipped (after step 0b). Full details of every step: `phase-6-build-log.md` in this folder.

**This repo is public:** never put secrets, course files, real timetable or exam data, calendar contents, or anything identifying you in anything committed, including this file and the build log. Real files live in `data/` (git-ignored); tests use made-up data.

## What Phase 6 builds

Phases 1–4 know what NTULearn knows: deadlines, posts, course files. They don't know **when your exams and classes are, which teaching week it is, or when you're free.** Your Google Calendar already holds your exams and other events (you, 7 Nov), so Phase 6 starts by looking at it and only builds what Claude plus the calendar can't already do:

- **First, carried over from Phase 3: the sign-in retry,** so a sync that starts as the laptop leaves standby recovers by itself.
- **The calendar trial:** you connect Claude's Google Calendar connector; I look at your calendar read-only and you ask real questions ("When are my exams?", "When am I free this week?"). That shows what works with no new code, and decides the rest of the phase.
- **Then, depending on the trial** (decisions 4–7):
  - **Calendar answers from Telegram,** if the Telegram session can be allowed the connector's read tools.
  - **Teaching weeks:** `data/academic_calendar.yaml` (typed from NTU's calendar page each semester) and a pilot tool `week_info(date)`: "Week 11", "Recess week", "Exam period".
  - **Exam reminders:** either Google Calendar's own notifications, or exams imported into the store and reminded by `uni-copilot remind` ("Exam tomorrow", "Exam in 2 hours: course, venue, what to bring", never "not submitted").
  - **Free time and study suggestions:** Claude reads your calendar and deadlines and proposes study blocks (in `weekly-actions`); writing them into the calendar is Phase 7.
  - **An Outlook check:** does any connector reach NTU email?

How it fits with Phases 1–4:

```
NTULearn ──► uni-copilot sync (every 2 h) ──► .data/uni-copilot.sqlite ──► pilot-mcp, remind, digest
                                                    ▲ (exams, if decision 6 imports them)
data/academic_calendar.yaml ──► pilot week_info (if decision 5)

Google Calendar ──(Claude connector, inside a Claude chat)──► Claude Code: exams, classes, free time
                                                              terminal; Telegram if decision 4 allows it
```

**A connector only works inside a Claude chat.** Claude can read your calendar in the terminal, but your programs can't: not `remind` (no AI), not the scheduled digest (connectors off), not the locked Telegram session (connectors off today). Anything a program needs from the calendar has to reach it another way: an `.ics` export, Google's private iCal address (a secret link), or the Google Calendar API.

Done when (final list after the trial):

1. **The calendar trial** is done and its findings are written down (shape only, no contents).
2. **Schedule questions** ("When are my exams?", "What's on Thursday?", "When am I free before <deadline>?") are answered correctly in the terminal, and from Telegram if decision 4 allows it.
3. **Exams are reminded** one way or the other (decision 6), at least a week before your first exam (about three weeks away).
4. **The running system kept running:** `.data\sync.log`, `remind.log`, `digest.log` look normal, and syncs after standby recover (step 0, checked against the logs over several days).

## Living next to the running system

The scheduled jobs (sync, remind, digest) run on your real store all day. So:

- **Changes to shared code** (`store/`, `cli.py`, `pilot_mcp/`) only add: new values, a new query, a new tool. After each such step: `uv run --no-sync pytest`, and the next lines of `.data\sync.log` and `remind.log` are checked.
- **If exams go into the store:** back it up first (`.data\uni-copilot.sqlite` → a dated copy). No migration is needed: `source` and `kind` are plain text columns and `starts_at` exists. A sync only removes events of its own source (`make_covers`), so imported exams are safe from it.
- **A new Python package** (e.g. PyYAML for the calendar file): close every Claude Code session → plain `uv sync` → `uv run pytest` → reopen and restart `scripts\start-telegram.ps1`. Or use TOML instead, which Python reads with the built-in `tomllib` (decision 5).
- **The Telegram session's lock** (`scripts\start-telegram.ps1`) changes only if decision 4 says so, and the guard test is updated with it.
- **`docs/how-it-runs.md`** changes in step 0 (the retry) and whenever something new runs.

## Steps

One step at a time, usually one file; I explain it and you test it before the next.

0. **The sign-in retry** (carried over from Phase 3; decided 7 Nov: when a hidden sign-in fails, wait about a minute and try once more).
   - 0a. `src/uni_copilot/ntulearn_mcp/auth/session.py`: `BrowserFailed(AuthError)` for a browser failure, so it can be told apart from "you must sign in" and the profile-lock timeout; a clearer error text; `tests/test_session.py`. **Done** (7 Nov: 277 passed, 4 skipped).
   - 0b. `src/uni_copilot/store/sync.py`: in `run_sync`, on `BrowserFailed`, wait 60 s (injectable, so tests don't sleep) and sign in once more; only then report the sources as failed. Placed in the sync, not in `sign_in()` (my recommendation, 7 Nov): a sync signs in once and stops if that fails, so the retry costs at most about a minute; the wait holds no browser-profile lock; and live tools still answer at once. Checked against the digest, which waits up to 3 minutes for a running sync. Tests in `tests/test_sync.py`. **Done** (12 Nov: 281 passed, 4 skipped).
   - 0c. Docs: `docs/how-it-runs.md` ("Known weak spot", "If it fails"), the Phase 3 build log's open item, the plan. Then the real results in `.data\sync.log` over the next days, compared with the standby events, before calling it fixed. **Docs done** (12 Nov); the log check is open.
1. **Housekeeping:** the plan doc and README reordered for the new Phases 6 and 7. **Done** (7 Nov, with this file).
2. **The calendar trial:** you connect Claude's Google Calendar connector (claude.ai → Settings → Connectors). I read your calendar read-only and report its shape only: how exams and classes are written (course in the title? venue? notifications set?), how regular they are. You ask 4–5 real questions and check my answers. Also tried: whether the Telegram session can use the connector's read tools (decision 4). Findings go into the build log without any calendar contents; decisions 4–7 are then answered, and steps 3+ are written here.
3. **Later steps:** written after the trial. Likely candidates: the Telegram session's connector access (`scripts\start-telegram.ps1` + guard test), the academic calendar file and `week_info` (`pilot_mcp/server.py`, a reader, tests), exam import and reminders (only if decision 6 says so), `weekly-actions` proposing study blocks, the Outlook check, docs (`CLAUDE.md`, `docs/how-it-runs.md`, `README.md`), and a final check.

## Decisions

1. **Phases 6 and 7 swapped** (your decision, 7 Nov): Phase 6 reads and plans, Phase 7 writes with your permission. The workload planner is split the same way: free time and proposed study blocks here, writing blocks into the calendar in Phase 7.
2. **No Kotlin in this project for now** (your decision, 7 Nov). Phase 6 is Python plus Claude; uni-copilot stays one language.
3. **Look at Google Calendar first** (your decision, 7 Nov), through Claude's connector, read-only, and build only what that doesn't already do.
4. **Calendar from Telegram** (after the trial): allow the connector's read tools in the Telegram session, if that's possible (not verified: the session turns all claude.ai connectors off today, with `ENABLE_CLAUDEAI_MCP_SERVERS=false`). Write tools stay out until Phase 7.
5. **Teaching weeks** (after the trial): if your calendar doesn't show NTU's teaching weeks, `data/academic_calendar.yaml` (or `.toml`, readable with no new package) and a pilot tool `week_info(date)`. Week numbers worked out in SGT.
6. **Exam reminders** (after the trial; my lean: check Google's own notifications first). Either Google Calendar's notifications on your phone (no code), or exams imported into the store (from an `.ics` export or a small `exams.csv`) and reminded by `remind`: 🟡 "Exam tomorrow", 🔴 "Exam in 2 hours", with course, time, venue and what to bring, never "not submitted", remembered in `notifications_sent` with the start time. Importing would go through `save_events`, so moved and removed exams reach the diary and the digest.
7. **Free time** (after the trial): Claude works it out from the calendar in a chat. If the trial shows slips (overlaps, repeating events), a small exact calculation in code is the fix.
8. **Every answer says where it came from:** "from your Google Calendar" (read just now), "as of <time> SGT" for the store, the file name for `data/` files.
9. **Nothing about you goes to Gemini:** calendar contents, exams and the timetable are never embedded (unchanged rule: embeddings send only course text).
10. **Outlook: a check only.** No Microsoft or Outlook connector was listed in the 7 Nov session; whether NTU allows one is an open question in the plan.

## What comes in Phase 7 (writing with permission)

Planned, decided when Phase 7 starts:

- **Google Calendar writes:** deadlines, exams and study blocks, each only after your "yes", never duplicated. Either Claude's connector with a skill (no code, but it can't work from the Telegram session today, where connectors are off and permission prompts wait at the PC), or a tool of our own using the Google Calendar API (works from Telegram; needs a Google sign-in token kept on your PC like the other secrets). Rules either way: create and update only, ideally one dedicated calendar, a hidden id on each event so a retry never makes a copy (not verified), confirmation in the chat before every write.
- **Forward-to-add:** you forward a group-chat message to the bot → Claude proposes an event → you confirm → `add_manual_event` (pilot). Text in a forwarded message can never trigger a write.
- **Open for then:** confirming from Telegram (`mark_done`'s answer: pre-allowed, and Claude asks "yes?" in the chat first).

## Things to check

Checked 7 Nov by reading the code:

- ✓ **The store's writer** uses WAL (`store/db.py`) and `BEGIN IMMEDIATE` (`store/events.py`); `save_events` already does the diary, moved, removed and one transaction.
- ✓ **`source` and `kind` are plain text columns** with no check in the table, so exams would need no migration.
- ✓ **Sync only removes events of its own source** (`make_covers`).
- ✓ **No YAML package** in `pyproject.toml` (the plan's tech stack listed PyYAML as if installed; corrected 7 Nov).
- ✓ **The Google Calendar connector is configured but not signed in** in this session (only its sign-in tools were listed).

Not verified yet; the step named checks each one:

- **Your Google Calendar:** how exams and classes are written, and whether exam notifications are set (step 2).
- **The Telegram session and connectors:** whether the connector's read tools can be allowed there (step 2).
- **NTU's academic calendar page:** where it is and what it lists (after step 2, if decision 5 needs it).
- **pilot's `kinds` filter** and **`notifications_sent`'s `due_at`** for exams (only if decision 6 imports them).

## New ideas you'll meet

- **Retry with an injectable wait:** trying again after a pause, and passing the "sleep" in so tests don't wait.
- **Claude connectors vs your own MCP servers:** what a connector can reach (only Claude, only in a chat) and what your programs can reach.
- **iCalendar (`.ics`):** the file format calendars export, and how repeating events are written in it (if used).
- **Importing a file safely:** check every row first, then save everything in one transaction or nothing (if exams are imported).

## Loose ends (not part of Phase 6; your call)

- **CC0006 Project Proposal status:** it showed "submitted" from the day it first appeared (2 Nov), so it never got a reminder. Asked on 7 Nov whether it was really submitted; the answer isn't recorded. If it wasn't, group-task status is being misread: a real bug and a missed reminder.
- **Retrieval numbers:** an informal 10-question test scored keyword-only vs hybrid search at precision@5 16% → 50% and MRR 0.55 → 0.75, judged by Claude and not confirmed by you; not quoted as settled.
- **Phase 4 done-when 5** ("When does <a quiz> open?") isn't recorded among your final checks.
- **Possible search improvement:** a weighted merge, because reciprocal rank fusion can sink a good meaning-only hit.
- **Still unverified from Phase 3:** `mark_done` from Telegram in real use, and your own comparison of the week against NTULearn and how the digests read on the phone.
- **Small items:** an extra poke after unlock for digest and remind (proposed, not built); deleting `.data\uni-copilot.v1-backup.sqlite` (yours to delete); an unused import in `cli.py`; the bot's username in public docs (harmless: it only answers you); no licence chosen, on purpose.
