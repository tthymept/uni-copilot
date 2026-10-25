# Phase 2 build log

What each finished step changed, how to test it, and its commit. Unverified points from each step are collected in the "Open items" section at the bottom, and new terms in the "Concepts" section. The short overview is `phase-2-core.md` in this folder. Phase 1 (the live NTULearn tools this phase builds on) is in `docs/phase 1/`.

## How it all connects (read this first when you come back)

### The layers

```
NTULearn
  │   GET only, 1 request/s (Phase 1: client/http.py)
  ▼
Phase 1 clients ──── DeadlinesClient, AnnouncementsClient, MessagesClient (ntulearn_mcp/client/)
  │   normal Python imports and method calls, no MCP
  ▼
uni-copilot sync ─── a plain script, no AI (cli.py → store/sync.py)
  │   sources/ntulearn.py turns Phase 1 results into events (adapters)
  ▼
SQLite event store ─ .data/uni-copilot.sqlite (store/db.py, store/events.py)
  │   tables: events (now), event_changes (diary), state (sync times, bookmarks)
  ▼
pilot-mcp ────────── reads the store only, never NTULearn (pilot_mcp/server.py → store/queries.py)
  │   MCP protocol, like Phase 1
  ▼
Claude ("what's new?") ── answers "as of <last sync> SGT"
```

`ntulearn-mcp` from Phase 1 is still there next to `pilot-mcp`, for live details and "right now" questions.

Filled in step by step as files are built.

### Why a database, and how fresh it is

- **Memory, not speed:** NTULearn never tells us when something changes, so we compare what's there now with what was saved before. That needs the earlier copy saved somewhere.
- **Freshness:** the store is only as fresh as the last sync (every 2 hours once step 12 is set up, or by hand). Asking never triggers a fetch. Every `pilot-mcp` answer says "as of <last sync time> SGT".

### The three tables

- **`events`:** one row per deadline / announcement / message, **as it looks now**. Each sync updates the row in place. The current `status` (`open`, `submitted`, `graded`, `done`) lives here.
- **`event_changes`:** the **diary**. One row per change found (`new`, `edited`, `moved`, `status_changed`, `removed`), with the time and what it was before/after. Rows are only added, never changed.
- **`state`:** key → value notes, e.g. `last_sync:ntulearn_deadlines → 14:00`, `last_seen:terminal → 10:00`.

They're linked by `event_id`: each diary row says which event it's about.

### Two comparisons

1. **Sync vs the previous sync (detecting):** each sync compares NTULearn with `events` and writes the differences into `event_changes`. Two syncs with nothing new → the second writes nothing.
2. **`whats_new` vs your bookmark (reporting):** reads the diary rows written after your "last seen" time, then looks up their titles in `events`. `mark_seen` moves the bookmark.

Example: you ask at 10:00; an announcement is posted at 11:00; syncs run at 12:00 and 14:00; you ask "what's new?" at 15:00. The 12:00 sync writes "new" in the diary, the 14:00 sync writes nothing, and at 15:00 `whats_new` finds the 12:00 row because it's after your 10:00 bookmark. Answer: "as of 14:00: 1 new announcement".

## Step 0: housekeeping

- **Decisions made (25 Oct):** all as recommended in `phase-2-core.md`, plus a new one about **freshness**: sync runs on a timetable or by hand, never because you asked; when you ask, nothing is re-fetched even if the data is old, but every `pilot-mcp` answer states "as of <last sync time> SGT". "Right now" questions go to the live `ntulearn-mcp` tools.
- **`docs/phase 2/phase-2-core.md`:** decisions marked as made; added the "Why a database, and how fresh it is" section (the three tables, the two comparisons, the 10:00/15:00 example); steps 7, 8, 10, 11 and 13 now mention `as_of`; `mark_seen` is in (no longer "maybe"); a check that Claude really states "as of".
- **`docs/uni-copilot-project-plan.md`:**
  - Status line: Phase 2 started, pointing to this folder.
  - Section 5.1: the Phase 2 decisions (one event per deadline, stored text, status mapping, sync window, two comparisons, freshness, plain `sqlite3`).
  - Section 5.3: `whats_new` only reads; added `mark_seen` and `sync_status`.
  - Roadmap: Phase 2 started; `weekly_actions` is a skill (was a prompt); optional Task Scheduler sync.
- **This log:** created, in the same format as the Phase 1 log.
- **`.gitignore`:** `.data/` is already ignored (Phase 1 step 0), so the database file can never be committed.
- **SQLite:** Python 3.12.14 comes with SQLite 3.53.1 built in. Nothing to install.

Check (PowerShell):

```
git check-ignore -v .data/uni-copilot.sqlite
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Expect the first to print the `.gitignore` line for `.data/`, and the second `3.53.1` (or newer).

Commit (the Phase 1 docs move and repair, plus this step):

```
git add -A src/uni_copilot/ntulearn_mcp/client/http.py docs
git commit -m "docs: move phase 1 docs, restore overwritten files and plan phase 2"
```

## Step 1: store/models.py

**New package `src/uni_copilot/store/`** (`__init__.py` with a one-line description). Everything about the database will live here: `models.py` (shapes), `db.py` (step 2), `events.py` (step 3), `sync.py` (step 5), `queries.py` (step 7).

**`store/models.py`: the two shapes.**

- **`Event`**: one deadline, announcement or message, as it looks now. One row of the `events` table. Fields in five groups:
  - identity: `event_id` ("`<source>:<native id>`"), `source`, `kind`;
  - what it is: `course_code`, `course_site_id`, `title`, `body` (the full post as light Markdown), `url`;
  - when (UTC): `starts_at` (classes/tests, later), `due_at` (deadlines), `posted_at` (announcements/messages);
  - state: `status` (deadlines only), `content_hash`, `found_in` (calendar/gradebook);
  - bookkeeping, filled by the store in step 3, `None` from an adapter: `first_seen_at`, `last_seen_at`, `modified_at`, `removed_at`.
  - Three helpers: `native_id` (the NTULearn id after the colon), `is_active` (not removed) and `preview` (a one-line ~280-character cut of `body`, for compact lists only).
- **`Change`**: one diary line, one row of `event_changes`: `event_id`, `change_type`, `changed_at`, `before`, `after`, and `id` (the row number the database gives it).
- **Word lists** (`Literal` types): `Source` (the 3 Phase 2 sources), `Kind`, `Status` (`open`, `submitted`, `graded`, `done`), `ChangeType` (`new`, `edited`, `moved`, `status_changed`, `removed`).
- **`shorten(text)`**: squeezes text onto one line and cuts it to 280 characters with "...". Used by `preview`.
- **Checks on creation (`__post_init__`):** every `*_at` time must be UTC with a timezone (an SGT time is rejected too); `event_id` must start with its own `source` and have an id after the colon. Bad data fails where it's made, not weeks later in a query.

**Changed right after step 1: full bodies instead of a 280-character cut.** The first version stored `summary` (the first 280 characters + "..."), following the plan's "no full bodies of untrusted text". You asked for the whole text in answers, still fast and safe. Now:

- `body` holds the **full post** (Phase 1's light Markdown: links and `[file: name]` included). `content_hash` (step 4) is made from it, so any edit anywhere in the post is detected.
- `preview` is computed from `body`, never stored, and only used where a list would get too long (e.g. `list_events` with 20 items).
- `pilot-mcp` (step 8) gets `get_event(event_id)` for one item in full, and `whats_new` returns changed items with their full bodies. All instant, from the store.
- **Why it's still safe:**
  - It's the same text Claude reads live through `ntulearn-mcp` anyway.
  - `pilot-mcp` labels it UNTRUSTED exactly like Phase 1, and Claude's rule is never to follow instructions inside it.
  - Every `pilot-mcp` tool is read-only towards NTULearn (only `mark_seen` writes, and only our own bookmark).
  - The database is local in `.data/` and git-ignored.
  - Stale text isn't a risk inside the sync window: the next sync sees the edit (new hash) and updates the body.
- **Size:** about 113 announcements + 25 messages now, a few KB each: well under 1 MB.

**Why dataclasses here, not pydantic like Phase 1's `models.py`?** Same rule as Phase 1: pydantic for what Claude sees (it gives the SDK a JSON schema), dataclasses for internal hand-offs (`CalendarItem`, `DueColumn`). Events only travel between our own code: adapter → store → queries. `pilot-mcp` (step 8) will turn them into pydantic models with SGT times and UNTRUSTED labels.

**How `Event` connects to the rest (coming steps):**

```
Phase 1 Deadline / Announcement / Message
   │  sources/ntulearn.py (step 4): to_event(...) builds an Event, bookkeeping = None
   ▼
Event ──► store/events.py (step 3): compares with the stored row,
   │                               fills first_seen_at / last_seen_at / ...,
   │                               writes Change rows for what's different
   ▼
store/db.py (step 2): Event fields ↔ columns of `events`, Change ↔ `event_changes`
```

**Changes from the plan's event shape (section 5.1):**

- **Added `posted_at`** for announcements and messages. The plan only had `starts_at` and `due_at`; using `starts_at` for "posted" would mix two meanings.
- **Added `removed_at`**: a removed event keeps its row (so `whats_new` can still show its title), and `removed_at` marks it so `list_events` hides it.
- **Added `found_in`**: where a deadline was found (calendar, gradebook), from the "one event per deadline" decision.
- `modified_at` means "the latest sync that recorded a change for this event" (store bookkeeping), not NTULearn's own edit time.

Test (PowerShell):

```
uv run python -m uni_copilot.store.models
uv run pytest
```

Expect:
- Two events in the same shape (`native_id=_999_1`, `status=None` for the announcement); "body is kept in full: 640 chars, 30 lines" and a ~280-character preview ending in "...".
- The frozen copy (old `open`, new `submitted`) and a `Change(... status_changed ...)` line.
- 4 lines starting "rejected (...)". None say "NOT rejected".
- `pytest`: 78 passed, 4 skipped (unchanged; store tests come in step 11).

Commit:

```
git add src/uni_copilot/store "docs/phase 2"
git commit -m "feat(store): add event and change shapes"
```

## Step 2: store/db.py

**`src/uni_copilot/store/db.py`: the SQLite file.** It knows *how* things are stored. *What* to store (new? edited? moved?) is step 3.

- **`DB_PATH`** = `.data/uni-copilot.sqlite` (git-ignored, from `DATA_DIR` in Phase 1's `config.py`).
- **`MIGRATIONS`**: a list of SQL scripts, one per schema version. Version 1 creates the three tables:
  - `events`: one column per `Event` field, same names. `event_id` is the **primary key** (the column that identifies a row; SQLite refuses two rows with the same one, which is what makes `event_id` the dedupe key).
  - `event_changes`: `id` is a number SQLite counts up by itself (`AUTOINCREMENT`); `event_id` **references** `events`, so a diary line for an event that doesn't exist is refused (a **foreign key**).
  - `state`: `key` → `value`.
  - **Indexes** on `due_at`, `posted_at`, `course_code`, `source` and on the diary's `changed_at` and `event_id`: like a book's index, they let SQLite find "due this week" or "changes after 10:00" without reading every row.
- **`open_db(path)`**: opens (or creates) the file, then:
  - `row_factory = sqlite3.Row`: rows can be read by column name (`row["title"]`).
  - `PRAGMA foreign_keys = ON`: SQLite only checks foreign keys when asked to.
  - `PRAGMA busy_timeout = 5000`: if sync is writing while `pilot-mcp` reads, wait up to 5 s instead of failing.
  - `PRAGMA journal_mode = WAL`: readers don't block the writer. SQLite keeps two helper files next to it (`-wal`, `-shm`), also inside the ignored `.data/`.
  - `migrate(conn)`: brings the tables up to date.
- **`migrate(conn)`**: reads the file's version (`PRAGMA user_version`, 0 for a new file) and runs each migration it hasn't had, inside a **transaction** together with the new version number. If anything fails, `ROLLBACK` leaves the file exactly as it was. Running it again does nothing. Later phases add new steps at the **end** of the list (never edit an old one).
- **Times as text**: `time_to_db` / `time_from_db` use one fixed format, `2026-11-07T15:59:00.123000+00:00` (UTC, always with microseconds). Same length every time, so sorting the text sorts by time, and reading it back gives exactly the same datetime. That matters for step 3: a time that changed by a millisecond on the round trip would look like a "moved" deadline every sync.
- **Row converters**: `event_to_row` / `row_to_event` and `change_to_row` / `row_to_change`. `found_in` is stored as `"calendar,gradebook"`. `row_to_event` builds a real `Event`, so step 1's checks run again on everything read back.
- **`get_state` / `set_state`**: read and write one note in `state`. `set_state` is an **upsert** in SQL (`INSERT ... ON CONFLICT (key) DO UPDATE`): insert, or replace the value if the key exists. It doesn't commit by itself, so step 5 can save the sync time in the same transaction as the events.
- **`?` and `:name` in SQL** (placeholders): values are passed separately from the SQL text, never pasted into it. SQLite then can't mistake a title like `'); DROP TABLE events; --` for SQL (that trick is called **SQL injection**).

**No `CHECK` rules on `kind`, `source` or `status` in the table.** Later phases add new kinds and statuses, and changing a `CHECK` in SQLite means rebuilding the table. The Python side already limits them (`Literal` types, `Event.__post_init__`).

**For step 8:** a `sqlite3` connection may only be used by the thread that opened it, and MCP tools run in worker threads. So `pilot-mcp` will open a connection per tool call (cheap: a few milliseconds).

Test (PowerShell):

```
uv run python -m uni_copilot.store.db
uv run python -m uni_copilot.store.db --real
uv run pytest
```

Expect:
- In memory (nothing saved): schema version 1, the three tables with their columns; "Running the migrations again does nothing: version 1"; `due_at stored as text: 2026-11-07T15:59:00.123000+00:00`; "read back equals the original: True"; a diary row with `id=1`; "refused: FOREIGN KEY constraint failed".
- `--real`: creates `.data/uni-copilot.sqlite` and prints "Schema version: 1" and the three tables with 0 rows. Run it twice: still version 1.
- `pytest`: 78 passed, 4 skipped.

Optional: you can open the file in a viewer like "DB Browser for SQLite" or the VS Code extension "SQLite Viewer" to see the empty tables. Close it before running sync later.

Commit:

```
git add src/uni_copilot/store/db.py "docs/phase 2"
git commit -m "feat(store): add sqlite database with versioned schema"
```

## Step 3: store/events.py (the heart of Phase 2)

**`src/uni_copilot/store/events.py`: saving events and writing the diary.** Every sync hands it a list of events ("what NTULearn shows now"). It compares each with the stored copy, writes the differences to `event_changes`, and saves the new copy.

**The main function: `save_events(conn, fetched, now, covers=None) -> SaveResult`**

1. Checks no `event_id` appears twice in `fetched` (that would be an adapter bug).
2. Opens **one transaction** (`with conn:`): `COMMIT` at the end, `ROLLBACK` if anything raises.
3. `load_events(conn)`: reads every stored event into a dict by `event_id` (hundreds of rows, so reading all is simplest).
4. For each fetched event:
   - `diff(old, new, now)` → the diary lines (below).
   - `merge_bookkeeping(old, new, now, changed)` → the copy to store: the new source fields, `first_seen_at` kept from the old copy, `last_seen_at = now`, `modified_at = now` only if something changed, `removed_at = None`.
   - **Upsert** it (`INSERT ... ON CONFLICT (event_id) DO UPDATE SET ...`: insert, or overwrite every column if the id exists) and insert the diary lines.
5. **Removals**, only if `covers` was given: every stored event that's still active, missing from `fetched`, and `covers(event)` says "this sync should have included it" → `removed_at = now` and a `removed` diary line.
6. Returns `SaveResult`: the `changes` written, how many were `seen`, `counts()` (e.g. `{"new": 3}`) and `unchanged`.

**`diff(old, new, now)`, the comparison** (a **pure function**: no database, easy to test):

```
old is None                     -> new
old was removed, now it's back  -> new (before = "removed")
due_at or starts_at different   -> moved           before/after = the two times
status different                -> status_changed  before/after = the two words
content_hash different          -> edited          before/after = the titles, only if the title changed
nothing different               -> []  (no diary line)
```

One event can get several lines in one sync (e.g. moved *and* edited). Fields like `url` or `found_in` are saved but don't count as a change.

**Why `covers` is a function passed in.** `events.py` can't know whether a missing item was really deleted, or whether its course's fetch failed, or whether it just left the time window. `sync.py` (step 5) knows, so it passes a small function that answers "should this sync have included this stored event?". With no `covers`, nothing is ever removed: the safe default. Passing a function like this is the same **dependency injection** idea as `signer=sign_in` in Phase 1.

**Why two identical syncs write nothing (the "done" test).** Every check in `diff` is "different or not". Same data in → no differences → no diary rows. Only `last_seen_at` moves.

**How it connects:**

```
sync.py (step 5)
  │  adapters (step 4) turn Phase 1 results into Events
  ▼
save_events(conn, events, now, covers)
  ├─ load_events ── db.row_to_event ──────── reads `events`
  ├─ diff ───────── models.Change ────────── what changed (pure)
  ├─ merge_bookkeeping ── dataclasses.replace
  └─ conn.execute(_UPSERT_EVENT / _INSERT_CHANGE) ── db.event_to_row / change_to_row
  ▼
SaveResult (changes, counts) → printed by `uni-copilot sync` (step 6)
```

Test (PowerShell):

```
uv run python -m uni_copilot.store.events
uv run pytest
```

The self-test plays five made-up syncs in memory (nothing saved). Expect:

- 1) `new: 3`.
- 2) the same data 2 h later: `unchanged 3, changes none`. This is the done test in miniature.
- 3) `moved: 1, status_changed: 1, edited: 1`.
- 4) the announcement is gone and announcements are covered: `removed: 1`.
- 5) the deadlines are missing but nothing is covered (a failed fetch): `changes none`.
- 6) a crash half-way: "diary rows before 7, after 7; Assignment 3 stored: False". The transaction undid everything from that sync.
- 7) the diary: 7 rows, `#4 moved ... 2026-11-07T15:59 -> 2026-11-09T15:59`, `#5 status_changed open -> submitted`.
- 8) Assignment 1: first seen 04:00, last seen 08:00, modified 08:00 UTC.
- `pytest`: 78 passed, 4 skipped.

Commit:

```
git add src/uni_copilot/store/events.py "docs/phase 2"
git commit -m "feat(store): save events and record changes"
```

## Step 4: sources/ntulearn.py (adapters and content_hash)

**4a/4b. Phase 1 body limit raised: `BODY_MAX_CHARS` 4000 → 20 000** in `client/announcements.py` and `client/messages.py`. A live count (25 Oct, counts only) found the old limit cut 1 of 114 announcements (longest ~4000, median ~770; messages: longest ~3800, median ~340). Since the store now keeps full bodies (step 1 change), the limit is only a safety net against a huge pasted page. The live `ntulearn-mcp` tools also return full bodies now.

**4c. New package `src/uni_copilot/sources/`, file `ntulearn.py`: the adapters.** An **adapter** is a small function that copies fields from one shape into another, like a travel plug adapter. Pure functions: no NTULearn, no database.

- `deadline_to_event(d)` → `ntulearn_gradebook:<column id>`, kind `deadline`, `due_at`, `status` mapped by `STATUS_FROM_SUBMISSION`, `found_in` sorted (so "gradebook, calendar" and "calendar, gradebook" are the same).
- `announcement_to_event(a)` → `ntulearn_announcement:<id>`, `body`, `posted_at = created_at`. A post merged from several sites keeps the first site for its link.
- `message_to_event(m)` → `ntulearn_message:<id>`, `body`, `posted_at`. Messages have no title, so `message_title(m)` makes one: "Message from <sender>" or "Message from you".
- `deadlines_to_events`, `announcements_to_events`, `messages_to_events`: whole lists.
- `STATUS_FROM_SUBMISSION`: `not_submitted`/`unknown` → `open`, `submitted` → `submitted`, `graded` → `graded`, `exempt` → `done`.

**`content_hash(*parts)`, the fingerprint** (concept explained in `phase-2-core.md`, "What `content_hash` is"): SHA-256 of the parts joined with an invisible separator character (`\x1f`), as 64 hex characters. The separator keeps parts apart: without it, `("ab", "c")` and `("a", "bc")` would join to the same "abc".

What goes in:
- Deadline: title.
- Announcement: title + body.
- Message: sender name + body.

What's left out on purpose: due time and status (their own change types), `found_in`, the list of sites, `is_read`. These change without anyone editing anything.

**How it connects:**

```
Phase 1 clients ──► DeadlineList / AnnouncementList / MessageList
                         │  .deadlines / .announcements / .messages
                         ▼
sources/ntulearn.py ── *_to_events(...) ──► list[Event]   (content_hash filled, bookkeeping None)
                         ▼
store/events.py ────── save_events(conn, events, now, covers)   (step 5 calls this)
```

Test (PowerShell):

```
uv run python -m uni_copilot.sources.ntulearn
uv run python -m uni_copilot.sources.ntulearn --live
uv run pytest
```

Expect:
- Offline:
  - 1) three events (`ntulearn_gradebook:_999_1`, `ntulearn_announcement:_888_1`, `ntulearn_message:_777_1`), each with a hash; `found_in=('calendar', 'gradebook')`.
  - 2) the status mapping.
  - 3) True only for "one word of the body changed" and "title changed"; False for "same post fetched again", "found on a third site too", "message opened" and "deadline submitted".
  - 4) `False`.
- `--live` (GET only, about 1 minute, counts only). On 25 Oct: `{'deadline': 28, 'announcement': 31, 'message': 15}`, statuses `{'submitted': 1, 'open': 27}`, `duplicate event_ids: 0`, longest body 3839, cut by the safety limit 0.
- `pytest`: 78 passed, 4 skipped.

Duplicate ids must be 0, because `save_events` refuses a list with the same id twice.

**For step 5:** `DeadlinesClient.upcoming(days)` only looks forward from now. The decision says deadlines from 14 days ago (so "submitted → graded" on recent items is still seen), so step 5 adds a small look-back option to it.

Commit:

```
git add src/uni_copilot/sources src/uni_copilot/ntulearn_mcp/client/announcements.py src/uni_copilot/ntulearn_mcp/client/messages.py "docs/phase 2"
git commit -m "feat(sources): convert ntulearn results to events with content hashes"
```

## Open items

Not verified yet. Each is checked in the step named in `phase-2-core.md` ("Things to check during Phase 2").

- Hash stability across re-fetches (steps 4–6).
- Sync time, expected about 1 minute (step 5).
- Sessions 2 hours apart, and headless sign-in from Task Scheduler (step 12).
- Whether Claude Code picks the `weekly-actions` skill for a plain question (step 10).
- Whether Claude states "as of" in every answer (step 13).
- **A deadline moved far outside the sync window** (e.g. to +5 months) disappears from the fetch while its *stored* due date is still inside the window, so step 5 would mark it removed rather than moved. Rare; when it comes back into the window it reappears as `new` (before = "removed"). Revisit if it ever happens.
- **Which id a merged cross-post keeps.** Phase 1's `merge_duplicates` keeps the newest copy's id. If two copies have the same time, the order sites are fetched in decides. If that ever flips between syncs, it would look like "removed" + "new". Checked by the two-syncs test in step 6.
- **Later (noted 25 Oct):** a live `get_announcement(id)` tool in `ntulearn-mcp` (maybe `get_message` too): fetch one post by id instead of a whole course's list. Recorded in the plan, section 4.1 "News". Not needed in Phase 2, because `get_event` serves full bodies from the store.

## Concepts

- **Database:** a program (here a single file) that stores data in tables and finds it fast.
- **SQLite:** a whole database in one file, built into Python as `sqlite3`. No server to install or run.
- **Table / row / column:** like a spreadsheet: a table is one sheet, a row is one thing (one event), a column is one field (`title`, `due_at`).
- **Event:** our one common shape for everything with a time or a "something happened": deadlines, announcements, messages (later exams, classes, manual items).
- **Sync:** fetch from the source, save to the store, record what changed.
- **Change log (`event_changes`):** the diary of every change found, so "what's new" can be answered later from any bookmark.
- **Bookmark ("last seen"):** the time a channel (terminal, telegram) last looked; "new" means "changed after this".
- **Freshness / "as of":** how old the stored data is: the time of the last successful sync.
- **`Literal` type:** a type hint listing the only allowed words, e.g. `Literal["open", "submitted", "graded", "done"]`. Like other type hints, plain Python doesn't enforce it; editors and readers use it.
- **`__post_init__`:** a method a dataclass runs right after creating an object; we use it to reject bad data.
- **`dataclasses.replace(obj, field=new)`:** makes a changed copy of a frozen object; the original stays as it was.
- **SQL:** the language for talking to a database: `CREATE TABLE`, `INSERT`, `SELECT ... WHERE`, `UPDATE`.
- **Primary key:** the column that identifies a row; no two rows can share one (`event_id`).
- **Foreign key (`REFERENCES`):** a column that must point at an existing row of another table (`event_changes.event_id` → `events`).
- **Index:** a sorted lookup list next to a table, so finding rows by that column is fast.
- **Transaction (`BEGIN` ... `COMMIT` / `ROLLBACK`):** a group of changes that all happen or none do.
- **Schema / migration / `PRAGMA user_version`:** the tables' structure / one numbered upgrade of it / the number SQLite stores in the file to remember which upgrades it already has.
- **`PRAGMA`:** a SQLite-specific setting or question (`foreign_keys`, `journal_mode`, `user_version`).
- **WAL (write-ahead log):** a SQLite mode where readers and a writer don't block each other.
- **Placeholder (`?`, `:name`) and SQL injection:** values are sent separately from the SQL text, so text from other people can never be run as SQL.
- **Upsert in SQL:** `INSERT ... ON CONFLICT (key) DO UPDATE`.
- **`with conn:`:** Python's `sqlite3` turns the block into one transaction: commit if it finishes, roll back if it raises.
- **Idempotent:** doing it twice has the same effect as once. Two identical syncs → the second writes no diary lines.
- **Adapter:** a small function that converts one shape into another (`Deadline` → `Event`).
- **Hash / SHA-256 / `hashlib`:** a fingerprint of text: same text → same 64-character code, any change → a different one. `hashlib` is Python's built-in module for it.
- **Separator:** a character put between parts before hashing so different splits can't give the same text.
- **Callable / passing a function:** `covers` is a function handed to `save_events`, so the caller decides the rule and `events.py` just applies it.
- **Bookkeeping fields:** fields our own code fills (`first_seen_at`, ...), as opposed to fields that come from the source (`title`, `due_at`).
