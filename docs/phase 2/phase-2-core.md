# Phase 2: event store

Status (25 Oct 2026): all 14 steps (0–13) done. **Phase 2 is complete.** Two scheduled syncs in a row reported 0 changes, and plain questions in a fresh Claude Code session went to pilot and the skill by themselves. Next: Phase 3 (Telegram). Full details of every step: `phase-2-build-log.md` in this folder.

## What Phase 2 builds

A local memory for uni-copilot. Phase 1 asks NTULearn live every time (15–25 s per question) and forgets everything afterwards. Phase 2 adds:

- **An event store:** one SQLite file on your PC (`.data/uni-copilot.sqlite`, git-ignored). SQLite is a whole database in a single file, built into Python (`sqlite3`), no server to install. Every deadline, announcement and message becomes one **event** row in the same shape.
- **`uni-copilot sync`:** a plain script, no AI. It uses the Phase 1 clients (`DeadlinesClient`, `AnnouncementsClient`, `MessagesClient`) to fetch from NTULearn, turns the results into events, saves them, and records **what changed** since last time (new, edited, moved, status changed, removed).
- **`pilot-mcp`:** a second MCP server for Claude, separate from `ntulearn-mcp`. It only reads the local store, never NTULearn, so answers are instant: `list_events` ("what's due this week?") and `whats_new` ("what changed since I last looked?"). Every answer says how old the data is ("as of 14:00 SGT").
- **The `weekly_actions` workflow:** one request that turns deadlines, announcements and messages into a checklist with sources.

How it fits with Phase 1:

```
NTULearn ──► Phase 1 clients ──► uni-copilot sync ──► SQLite event store ──► pilot-mcp ──► Claude
             (live, read-only)    (script, no AI)      (.data/, local)        (instant)
                    │
                    └──────────────► ntulearn-mcp ──► Claude   (still there for live details)
```

Done when (from the plan): **two syncs in a row report only real changes**. Run `uni-copilot sync` twice with nothing changed on NTULearn: the second run reports 0 changes. When something really changes (a new announcement, a deadline moved, you submit something), the next sync reports exactly that.

## Why a database, and how fresh it is

The store isn't mainly about speed. It's **memory**: NTULearn never tells us when something changes, so the only way to notice is to compare what's there now with what was there before. That's what makes "what's new?", "deadline moved", and (in Phase 3) Telegram pushes possible.

**Freshness:** the store is only as fresh as the last sync. Sync runs on a timetable (step 12: every 2 hours, 07:00–23:00) or when you run `uni-copilot sync` yourself. Asking a question never triggers a fetch. Instead, **every answer from `pilot-mcp` says "as of <last sync time> SGT"**, so you always know how old it is. For "right now" questions ("did Prof X post anything just now?"), Claude uses the live `ntulearn-mcp` tools.

**Three tables:**

- `events`: one row per thing (deadline, announcement, message), **as it looks now**. The sync updates it in place. The current `status` lives here.
- `event_changes`: the **diary**. One row per change found (`new`, `edited`, `moved`, `status_changed`, `removed`), with when and what it was before/after. Rows are only ever added, never edited.
- `state`: small notes: last sync time per source, and a "last seen" **bookmark** per channel (terminal, telegram).

**Two different comparisons:**

1. **Sync vs the previous sync** (detecting): each sync compares NTULearn with `events` and writes what's different into `event_changes`.
2. **`whats_new` vs your bookmark** (reporting): reads `event_changes` rows written after your last look, then fetches their titles from `events`.

Example: you ask at 10:00, an announcement is posted at 11:00, syncs run at 12:00 and 14:00, you ask "what's new?" at 15:00. The 12:00 sync writes "new: announcement" in the diary; the 14:00 sync writes nothing (correct). At 15:00, `whats_new` reads the diary after your 10:00 bookmark and finds it. Answer: "as of 14:00: 1 new announcement". `mark_seen` then moves your bookmark to 15:00.

## What `content_hash` is

A **fingerprint of an item's content**: a short code computed from its text, used to answer "did this change since the last sync?". A hash function (SHA-256, built into Python as `hashlib`) turns any text into a fixed-length code:

```
text                    length   hash (first 16 of 64 characters)
"Quiz in LT1."          12       753eba7b0124e706
"Quiz in LT1."          12       753eba7b0124e706   same text → same hash, always
"Quiz in LT2."          12       4d62a86fd8cead2e   one character different → a totally different hash
"Quiz in LT1. xxxx…"    5013     0bf539691ea3cc86   long text → still a short hash
```

Three properties make it useful: same input → same hash, every time and on every computer; any change, even one comma → a different hash (two different texts sharing a hash practically never happens); always 64 characters, however long the post.

**How the store uses it:** the adapter (step 4) computes `content_hash` from the fields that count as "the content" and saves it with the event. Next sync, `diff()` (step 3) checks `old.content_hash != new.content_hash` → "edited".

**What goes in, and what's deliberately left out** (step 4):

- Deadline: the title. Not the due time or status (they have their own change types, `moved` and `status_changed`), not `found_in` (the calendar sometimes misses an item; that's not an edit).
- Announcement: title + body (the Markdown body includes links and `[file: name]`). Not which sites it was found on.
- Message: sender name + body. Not `is_read`, which flips when you open it.

A field that changes by itself must never be in the hash, or every sync would report fake edits. That's why the hash exists even though full bodies are stored: it decides exactly what counts as an edit, and it works for future sources where we won't store the content (Phase 4 files and pages).

A hash isn't encryption or a secret: it can't be turned back into the text. It's only a fingerprint for comparing.

## Steps

One file per step. You test each step before the next, like Phase 1.

0. Housekeeping: decisions recorded in the plan doc and here, `phase-2-build-log.md` created, `.data/` confirmed git-ignored, SQLite version checked. **Done.**
1. `src/uni_copilot/store/models.py`: the **event** shape (`event_id`, `source`, `kind`, `course_code`, `course_site_id`, `title`, `body`, `url`, `starts_at`, `due_at`, `posted_at`, `status`, `content_hash`, `found_in`, `first_seen_at`, `last_seen_at`, `modified_at`, `removed_at`) and the **change** shape (`event_id`, `change_type`, `changed_at`, what was before and after). Frozen dataclasses that reject non-UTC times and wrong ids. Offline self-test. **Done.**
2. `src/uni_copilot/store/db.py`: opens the SQLite file, creates the tables (`events`, `event_changes`, `state`) and indexes, and upgrades the schema with a version number (`PRAGMA user_version`) so later phases can add tables safely. Also converts Events/Changes to rows and back, and reads/writes `state`. Test: create the database and list its tables. **Done.**
3. `src/uni_copilot/store/events.py`: **the heart of Phase 2.** `upsert` (insert a new event or update an existing one, matched by `event_id`) and **change detection**: compare the new version with the stored one and record `new`, `edited`, `moved` (due time changed), `status_changed` or `removed`. All changes from one sync are saved in one **transaction** (all or nothing). Removal only for what the caller says the sync covered (`covers`). Offline self-test with five made-up syncs. **Done.**
4. `src/uni_copilot/sources/ntulearn.py`: **adapters** that turn Phase 1 results into events: `Deadline` → `ntulearn_gradebook:<column id>`, `Announcement` → `ntulearn_announcement:<id>`, `Message` → `ntulearn_message:<id>`. Also the `content_hash` (a fingerprint of the fields that matter, so a real edit changes it but re-fetching the same thing doesn't). Plus: Phase 1's body limit raised from 4000 to 20 000 characters (it cut 1 real announcement), so the store really keeps full bodies. Offline and live (counts only) self-test. **Done.**
5. `src/uni_copilot/store/sync.py`: runs each source, saves the events, and records per-source sync state. Two safety rules: a source that **failed** (or a course in its `notes`) never marks its events as removed, and an item that only **left the time window** is not "removed". Also: a deadline whose status couldn't be read keeps its known status. Small additions: `DeadlinesClient.upcoming(past_days=14)` (Phase 1) and a write lock in `save_events` (two syncs at once). Live self-test (GET only, counts only): two syncs in memory, the second reported no changes. **Done.**
6. `src/uni_copilot/cli.py`: the `uni-copilot` command: `uni-copilot sync` (prints counts per source and per change type; exit code 1 if a source failed) and `uni-copilot status` (last sync per source = "as of", errors, how much the store holds). Replaces the placeholder `uni_copilot:main` (`pyproject.toml` now points to `uni_copilot.cli:main`). Test: run sync twice. **Done.**
7. `src/uni_copilot/store/queries.py`: the read side: upcoming events by days/kind/course, changes since a time, the "last seen" bookmark per channel, and the "as of" time (last successful sync per source). Plus two CLI commands that use it: `uni-copilot events` (what's coming up) and `uni-copilot changes` (the diary, with titles). The first sync's "new" lines (everything found the first time) aren't news: `whats_new` leaves them out. Offline self-test. **Done.**
8. `src/uni_copilot/pilot_mcp/server.py`: the second MCP server with `list_events` (titles + preview, compact), `whats_new` (changed items with their full bodies), `get_event` (one item in full), `sync_status` (last sync time, failed sources) and `mark_seen`. Every result carries `as_of` (UTC + SGT), and the instructions tell Claude to state it in every answer. Output models with SGT times and UNTRUSTED labels, like Phase 1 (in `pilot_mcp/models.py`). `mark_seen(channel, up_to=checked_at)` so a sync in between isn't skipped. Test: self-test and the MCP Inspector. **Done.**
9. `pyproject.toml`, `.mcp.json`, `.claude/settings.json`: add the `pilot-mcp` command, register the `pilot` server with Claude Code, and pre-allow its tools (the read-only ones plus `mark_seen`, which only moves a local bookmark). **Done.**
10. `.claude/skills/weekly-actions/SKILL.md`: the `weekly_actions` workflow: deadlines not yet submitted + announcements (~3 weeks) + messages + a logistics check for tests this week ("matric", "calculator", "venue", "bring") → a checklist with sources and the "as of" time; inferred items marked as inferred. Plus routing: pilot is the default for study questions (server instructions + `CLAUDE.md`), so nobody has to type "using pilot", on Telegram either. **Done** (live check pending: does Claude pick the skill by itself?).
11. `tests/`: store tests (dedupe, edits, moved deadlines, status changes, removed items, failed source doesn't remove, window exit isn't removal, two identical syncs → 0 changes, `whats_new` after a bookmark), adapter tests, pilot tool tests. All synthetic. 61 new tests (139 in total); the sync failure rules use fake Phase 1 clients. **Done.**
12. Optional: Windows Task Scheduler runs `uni-copilot sync` every 2 hours (07:00–23:00), so the store stays fresh without you. Checks that silent sign-in works from a scheduled task. Built as `scripts/schedule-sync.ps1` (`-DryRun`, `-RunNow`, `-Wake`, `-Remove`) + `uni-copilot --log FILE`; runs windowless with `pythonw`, catches up after missed runs. On your PC, not GitHub Actions (sign-in cookies and course data stay local). **Done** (Task Scheduler run itself: your test).
13. Final check: two syncs in a row → only real changes; then ask Claude Code "what's due this week?" and "what's new?" and check the answers come from `pilot` instantly, say "as of", and match NTULearn. Plan doc and logs updated. **Done** (25 Oct: two scheduled runs → 0 changes; fresh-session questions routed correctly; matched NTULearn).

Commit status: steps 0–12 committed up to `a8ec7c0`; the step 13 docs wrap-up goes in the next commit.

## Decisions

Made 25 Oct 2026 (all as recommended, plus freshness). Also recorded in the plan doc.

- **How sync gets the data:** the sync script imports the Phase 1 clients directly (`DeadlinesClient`, ...), not the MCP tools. This is exactly why Phase 1 kept the work in `client/` and the door in `server.py`.
- **How much text to store:** the **full body** of every announcement and message (the light Markdown from Phase 1's `parse_html`, links and `[file: name]` included) + a hash of it to detect edits. Changed after step 1 (it was title + a 280-character cut): you want the whole text in answers, fast, without a live fetch. `preview` (a one-line ~280-character cut) is only for compact lists; the full body is always available. Safe because: it's the same text Claude would read live anyway; `pilot-mcp` labels it UNTRUSTED like Phase 1; `pilot-mcp` tools can't change anything on NTULearn; the database is local and git-ignored; edits are picked up by the next sync.
- **One event per deadline:** `event_id = ntulearn_gradebook:<column id>` for every merged deadline, with where it was found (calendar, gradebook) kept as a field. The column id is the same in both sources, so one id avoids duplicates. The plan listed `ntulearn_calendar` and `ntulearn_gradebook` as separate sources; this is a small change from it.
- **Status mapping to the plan's event statuses:** `not_submitted` → `open`, `submitted` → `submitted`, `graded` → `graded`, `exempt` → `done`, `unknown` → `open`. Never any grade, only the status word.
- **What sync covers:** deadlines from 14 days ago to 120 days ahead (so "submitted → graded" changes on recent items are still seen), announcements and messages from the last 30 days. About 1 minute per sync at ~1 request/s.
- **Freshness:** sync runs on a timetable (step 12) or by hand; asking never triggers a fetch, not even when the data is old. **Every `pilot-mcp` answer states "as of <last sync time> SGT".** For "right now" questions, Claude uses the live `ntulearn-mcp` tools.
- **`whats_new` only reads;** a separate `mark_seen(channel)` tool moves the "last seen" bookmark. Read tools stay read-only and can be pre-allowed safely.
- **Manual events and `mark_done`:** later (Phase 3/7), since they're writes that need your confirmation. Phase 2 stays read-only from Claude's side (apart from `mark_seen`, which only moves a bookmark).
- **`weekly_actions` as a Claude Code skill**, not an MCP prompt. A skill is picked by Claude automatically when your question fits ("what do I need to do this week?"), which also works from Telegram in Phase 3; an MCP prompt only runs when you type its slash command. To verify in step 10.
- **Database code:** Python's built-in `sqlite3` with plain SQL, no ORM library, so you see every query.
- **Scheduling (step 12):** in Phase 2, as an optional last step.

## Things to check during Phase 2

Not verified yet; each is checked in the step named.

- **Hash stability (steps 4–6):** re-fetching the same announcement or deadline must give the same `content_hash`. Fields that change by themselves (e.g. `is_read`, which source a deadline came from, SGT text) must not be part of it, or every sync would report fake edits. Verified 25 Oct (step 5): two live syncs in a row → no changes.
- **Sync time (step 5):** expected about 1 minute. Verified 25 Oct: ~63 s (deadlines 27 s, announcements 16 s, messages 20 s).
- **Sessions 2 hours apart (step 12):** an open question in the plan. Silent re-login on 401 (Phase 1) should cover it; the scheduled runs will show.
- **Headless sign-in from Task Scheduler (step 12):** verified from a terminal, not from a scheduled task (which may run without your desktop session).
- **Skills picked automatically (step 10):** whether Claude Code uses the skill for a plain question without a slash command.
- **Claude states "as of" every time (step 13):** the tool returns it and the instructions ask for it; check it actually does.

## New ideas you'll meet

Explained in the build log when they first appear, tied to the code:

- **SQL, table, row, column, primary key, index:** how a database stores and finds things.
- **Upsert:** "insert, or update if it already exists".
- **Transaction:** a group of changes that all happen or none do.
- **Hash (fingerprint):** a short code computed from content; same content → same hash.
- **Schema migration:** upgrading the database's structure without losing data.
- **Idempotent:** running something twice gives the same result as once; the done test checks that sync is.
- **Adapter:** code that converts one shape (Phase 1 models) into another (events).
- **Skill:** a Claude Code instruction file Claude uses when your request matches it.
- **Task Scheduler:** Windows' built-in way to run a command on a timetable.
