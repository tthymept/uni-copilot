# uni-copilot: project plan

A personal academic agent for NTU students. It reads NTULearn (and later other sources), keeps track of what changed, and tells you what to do, in Telegram or Claude Code. It runs on your own computer with your own login, and never stores passwords.

Status as of 25 Oct 2026: Phase 0 spike passed. **Phase 1 (`ntulearn-mcp` core) done** (24 Oct): 6 read-only tools in Claude Code; details `docs/phase 1/`. **Phase 2 (event store) done** (25 Oct): local SQLite store, `uni-copilot sync` every 2 h via Task Scheduler, change detection, `pilot-mcp` (5 tools, "as of" in every answer, the default for study questions), `weekly-actions` skill; 139 offline tests + 4 opt-in live tests; details `docs/phase 2/phase-2-build-log.md` (overview: `docs/phase 2/phase-2-core.md`). **Phase 3 (Telegram) started** (26 Oct): decisions made, details `docs/phase 3/`; test week until ~3 Nov. **Phase 4 (content and search) started** (29 Oct, in parallel with Phase 3's test week, without touching its pieces): decisions made, details `docs/phase 4/`. "Verified" below means checked on my own account (GET only); "Not verified" means designed but not yet tested.

---

## 1. Goals and non-goals

**Goals**

1. Answer "what do I need to do this week?" in one message, with every item citing its source.
2. Push what matters without being asked: a morning digest when something changed, reminders before unfinished deadlines, alerts for new announcements.
3. Turn lecture recordings and slides into study notes.
4. Put deadlines into Google Calendar, with my confirmation for every write.
5. Be a clean, well-tested project I can explain end to end in an internship interview (event pipeline, local state, agent design, security).

**Non-goals**

- Writing anything to NTULearn (no submissions, posts, settings).
- Storing passwords, or hosting anyone's NTU session on a server.
- Other students' data.
- A web app.
- A paid AI API for answers. Everything Claude does runs through my existing Claude plan via Claude Code. (Changed 29 Oct, Phase 4 decision 13: search uses an outside **embedding** API, Google's Gemini, free tier for now, for course content and search words only; never posts, messages or grades. Swappable for OpenRouter or a free local model.)
- OCR of scanned PDFs, downloading video, speech-to-text for videos without transcripts.

---

## 2. Architecture at a glance

```text
 UPSTREAM (sources)                 CORE (on my PC)                         DOWNSTREAM (channels)
 ───────────────────                ───────────────                         ─────────────────────
 NTULearn (Blackboard)  ─┐
 NTULearnVideo (Kaltura) ┤──► ntulearn-mcp ──┐
                         │   (live, read-only) │
 Google Calendar  ───────┤  (Claude connector) │
 NTU Outlook email  ─────┤  (Claude connector) │      ┌──────────────┐
 Academic calendar  ─────┤                     ├────► │  Claude Code │ ──► Telegram (Q&A, via channels plugin)
 Exam timetable  ────────┤──► pilot-mcp ◄──────┘      │  (the agent) │ ──► Telegram push (digest, reminders)
 Class timetable  ───────┤   (event store,            │  skills +    │ ──► Google Calendar (confirmed writes)
 Forwarded messages  ────┘    whats_new, notify)      │  prompts     │ ──► Notes folder (lecture notes)
                                    ▲                 └──────────────┘ ──► Claude Code terminal / Remote Control
                                    │
                           uni-copilot sync / remind
                           (scheduled scripts, no AI)
```

Three layers:

1. **Sources.** Each source is reached through an MCP server or a Claude connector. My own server, `ntulearn-mcp`, does live read-only calls to NTULearn and NTULearnVideo.
2. **Core.** A local SQLite **event store** that every source feeds into, in one common format. A `sync` job fills it; `whats_new` compares runs. A second MCP server, `pilot-mcp`, exposes the store, manual events and notifications to Claude.
3. **Channels.** Claude Code is the agent. Telegram is the main way I talk to it. Scheduled jobs push digests and reminders.

Rule of thumb: **deterministic work runs as plain scripts (no AI usage); judgement work runs in Claude Code.** Syncing and reminders are scripts. Summaries, checklists and notes are Claude.

---

## 3. Upstream: sources

| # | Source | How it's reached | What it gives | Status | Phase |
|---|---|---|---|---|---|
| 1 | **NTULearn** (Blackboard Learn Ultra) | `ntulearn-mcp`, cookies from a Playwright login | Courses, deadlines, announcements, messages, content, files, grades (opt-in) | Mostly verified | 1 |
| 2 | **NTULearnVideo** (Kaltura) | `ntulearn-mcp`, session from an LTI launch | Recordings, transcripts (JSON), module videos | Verified | 5 |
| 3 | **Google Calendar** | Claude's built-in Google Calendar connector | Free/busy time (read), deadline events (write, confirmed) | Not set up | 6 |
| 4 | **NTU Outlook email** | Claude Microsoft 365 / Outlook connector, if available | Emails from profs and school offices | Not verified; NTU may block third-party app access | 7 |
| 5 | **NTU academic calendar** | Public page → a YAML file I update each semester | Teaching week number ↔ dates, recess, holidays | Not verified | 7 |
| 6 | **Exam timetable** | Official NTU source → a small CSV I import each semester | My exam dates, times, venues | Not verified | 7 |
| 7 | **Class timetable** | Import from my calendar, or a YAML/ICS I maintain | Class times and venues | Not verified | 7 |
| 8 | **Group project tools** (Google Drive, Notion, GitHub) | Claude connectors, read-only | Group docs and tasks | Optional | 8 |
| 9 | **Forwarded messages** | I forward a Telegram message to my bot | Info from course group chats | Design only | 7 |

Notes:
- Telegram course group chats are **not** read automatically (that would mean logging in as me and reading other people's messages). Forwarding is the opt-in alternative.
- Deadlines inside external tools (Gradescope, MS Forms, Google Forms) aren't reachable; only the link inside NTULearn is.

---

## 4. Core component A: `ntulearn-mcp` (live, read-only)

Python MCP server over stdio. Every tool is read-only on NTULearn (`read_only_hint=True`).

### 4.1 Tools

Phase 1 tools are built as described below (✓). Every `course` argument accepts a code ("IE2108"), part of a code ("DA3") or words from the name; 0 or 2+ matches return an error listing this term's codes. Every result has a `notes` list for courses that couldn't be read.

**Session**
- ✓ `login_status()`: one `users/me` call; signs in silently (hidden browser) if needed, never opens a window. Returns `signed_in`, a detail, and cookie names + expiry.
- ✓ `login()`: hidden browser first; if a real sign-in is needed, opens a visible window and waits up to 5 min (handles MFA). Not pre-allowed in `.claude/settings.json`, so Claude Code asks first.

**Courses**
- ✓ `list_courses(term=None, include_organizations=False)`: current-term subjects, each with its sites (e.g. IE2108 → lecture site + my tutorial site). `include_organizations` also lists Non-curriculum sites (NTU marks nothing as an organization). A personal alias file ("DSA" → IE2108) comes later.

**Deadlines and time**
- ✓ `upcoming_deadlines(days=7, course=None)` (`days` 1–120): calendar API (≤16-week windows) ∪ gradebook due dates, deduped by column id, filtered by exact due time, soonest first. Each item: title, `due_at` (UTC) + `due_sgt`, `status` (not_submitted / submitted / graded / exempt / unknown), `sources`, course link. Status comes from my gradebook row (`status` only; grade fields are never read). Gradebook columns cached 30 min; statuses always fresh. "Opens" and "closes" entries were planned for Phase 4 but dropped after its probe (29 Oct: almost no date rules); release conditions are shown per item by the Phase 4 content tools instead.
- `get_assessment(course, content_id | column_id)`: instructions, due date, attempts allowed, password needed, secure browser / webcam required, late attempts allowed, my status, release condition. Takes a gradebook column id too, so a deadline leads straight to it. Never score or points. (Phase 4)
- ~~`upcoming_windows`~~: dropped 29 Oct after the Phase 4 probe (8 date rules in 966 items, none still to come; 134 "after another item"). Each item shows its release condition instead (open / conditional, open to me / held back until another item is done / opens-closes on a date).

**News**
- ✓ `recent_announcements(since_days=21, course=None)` (1–180): posted or edited in the window, newest first; cross-posts merged; body as light Markdown plus `links` and `files`; `edited` + `modified_sgt`.
- ✓ `recent_messages(since_days=14, course=None)` (1–180): course Messages (some profs use these instead of announcements); only threads active in the window are fetched in full; `sender_name`, `sent_by_me`, `is_read`, Markdown body plus `links` and `files`.
- `get_announcement(announcement_id)` (later, noted 25 Oct; maybe also `get_message`): fetch one item live by id, the freshest copy of a single post, instead of a whole course's recent list. Not needed while `pilot-mcp` stores full bodies (`get_event`); useful for posts older than the sync window or when the store is stale. Needs the course site id too (the endpoint is per course), which the event already has.

**Content** (Phase 4; decisions 29 Oct in `docs/phase 4/phase-4-core.md`)
- `list_course_content(course, folder_id=None)`: one folder level, with the files embedded in pages; each item's release condition (from the internal folder list, which carries the rules). Cached 6 h (`refresh=true` checks live).
- `read_page(course, content_id)`: text of an Ultra document page (Phase 1's HTML parser).
- `read_file_text(course, content_id, attachment_id, pages=None)`: PDF (pypdf), PPTX (python-pptx), `.ipynb`; a few pages per call; no OCR. Text cached until the file changes; the bytes are dropped.
- `search(query, course=None)`: **hybrid**: SQLite FTS5 keyword search plus Gemini embeddings, merged by reciprocal rank fusion, over page text, file text, titles, and announcements and messages (copied read-only from the event store; keyword-only, never sent out). Falls back to keyword-only with a plain reason when embeddings aren't available (limit reached, no key, offline).
- `download_file(course, content_id, attachment_id)`: saves to `~/Downloads/uni-copilot/<COURSE>/` only (was `dest_dir`; dropped so text can't pick the folder); safe names, never overwrites, no videos, ≤ 200 MB, Mark of the Web. Not pre-allowed (asks every time); denied in the Telegram session. Changes nothing on NTULearn.
- `send_course_file(...)`: sends one course file to my own Telegram chat after I say yes; never saved on the PC; PDF/PPTX ≤ 50 MB. After Phase 3's test week.

**Recordings**
- `list_recordings(course)`: Media Gallery recordings and videos embedded in module pages together, each with `source` ("gallery" / "module"), title, module name, duration, `has_transcript`.
- `get_transcript(course, entry_id)`: JSON transcript as timestamped paragraphs plus "exam signal" segments (keywords like exam, final, quiz, tested, important).

**Later**
- `list_discussions(course)`: due date, have I posted.
- `list_groups(course)`: sign-up sheets and their deadlines.
- `module_progress(course)`: unfinished items in forced-order modules.

**Opt-in (off by default)**
- `recent_grades(since_days=14, course=None)`: only registered when `NTULEARN_ENABLE_GRADES=1`.

### 4.2 Prompts (one-command workflows, work in any MCP client)
- ✓ `weekly_actions` (built in Phase 2 as a Claude Code **skill**, `.claude/skills/weekly-actions/`, using the pilot store, not an MCP prompt; opening/closing windows were dropped in Phase 4, see `get_assessment`): deadlines not yet submitted + announcements (~3 weeks) + messages + a logistics search for each test this week ("matric", "passport", "calculator", "venue", "bring") → checklist with sources; inferred items marked as inferred.
- `whats_new`: what changed since the last check.
- `prep_for_lecture`: this week's slides + transcript summary for one course.

### 4.3 Internal layout
- `auth/session.py`: Playwright persistent profile, login, silent re-login (hidden browser first, visible window only if I need to sign in), cookie export. Leaves the Ultra page as soon as signed in.
- `client/http.py`: requests session, base URL, ~1 req/s, max 2 concurrent, retries with backoff on 429/5xx, 401 → silent re-login. GET only: `get_json` and `get_all` (follows paging) are its only request methods.
- `client/*.py`: one module per area. Built: courses, calendar, gradebook, deadlines (merges calendar + gradebook), announcements, messages. Later: contents, video.
- `parsers/`: built: `html.py` (HTML → light Markdown + links + files). Later: Ultra document bodies, embedded-video detection, transcript paragraphs.
- `cache.py`: `.data/cache/content.sqlite`: folder listings and page text (6 h), file text (until the file changes), the FTS5 index, chunks and their vectors (Phase 4). Never file bytes, download URLs, grades or keys. Safe to delete. Phase 1 only has small in-memory caches inside the clients (memberships 1 h, gradebook columns 30 min).
- Phase 4 also adds: `client/http.py` `get_file` (streamed GET, size cap, checked redirects; `get_json`/`get_all` unchanged), `client/contents.py`, `parsers/content.py`, `parsers/files.py`, `search.py` (hybrid ranking), `embeddings.py` (chunking + Gemini, swappable), `indexer.py` (run by hand, 1 request / 2 s, `.data/index.lock`, resumable), `probe_content.py` (live probe, counts only).
- `models.py`: typed results (pydantic), times in UTC with SGT text fields, UNTRUSTED labels on other people's text, no grade fields.
- `server.py`: the MCP server and tools; `ntulearn-mcp` command. `.mcp.json` registers it with Claude Code; `.claude/settings.json` pre-allows the read-only tools except `login`.

---

## 5. Core component B: event store and `pilot-mcp`

### 5.1 Event store (SQLite, local, git-ignored)

Built in Phase 2 (✓): `.data/uni-copilot.sqlite`, code in `src/uni_copilot/store/` (`models.py`, `db.py`, `events.py`, `sync.py`, `queries.py`) and `src/uni_copilot/sources/ntulearn.py`. Phase 2 uses the sources `ntulearn_gradebook`, `ntulearn_announcement`, `ntulearn_message`, the kinds `deadline`, `announcement`, `message`, and the statuses `open`, `submitted`, `graded`, `done`; the rest of the lists below are for later phases.

Every source is converted into one **event** shape:

| Field | Meaning |
|---|---|
| `event_id` | `<source>:<native id>` (e.g. `ntulearn_gradebook:_1183007_1`), the dedupe key |
| `source` | ntulearn_calendar, ntulearn_gradebook, ntulearn_announcement, ntulearn_message, ntulearn_window, kaltura_recording, outlook, academic_calendar, exam_timetable, timetable, manual |
| `kind` | deadline, test, exam, class, announcement, message, opens, closes, grade_released, recording_available, holiday |
| `course_code` / `course_site_id` | e.g. IE2108 / `_2706657_1` |
| `title`, `body` | full post as light Markdown, labelled UNTRUSTED when shown (changed in Phase 2: was short text only) |
| `starts_at`, `due_at`, `posted_at` | stored in UTC (fixed-format text with microseconds), shown in SGT; `posted_at` added in Phase 2 for announcements and messages |
| `status` | open, submitted, graded, locked, done, cancelled |
| `url` | link back to the source |
| `content_hash` | detects edits: SHA-256 of title (deadlines), title + body (announcements), sender + body (messages) |
| `found_in` | deadlines: calendar, gradebook (added in Phase 2) |
| `first_seen_at`, `last_seen_at`, `modified_at`, `removed_at` | change tracking (`removed_at` added in Phase 2: removed events keep their row) |

Other tables:
- ✓ `event_changes`: what changed and when (new, edited, moved, status_changed, removed), with before/after. Powers `whats_new` and the digest.
- `notifications_sent`: which reminder went out for which event, so nothing is sent twice. Key: event, tier (24h / 2h) and due time, so a moved deadline is reminded again. (Sync alerts are noted in `state`, since they aren't about one event.) Written only after Telegram accepted the message. (Phase 3, schema version 2)
- `marked_done`: items I handled outside NTULearn (Gradescope, Google Forms). Separate from `status`, so sync never overwrites it; reminders skip these. (Phase 3)
- `calendar_links`: `event_id` ↔ Google Calendar event id. (Phase 6)
- ✓ `state`: last sync time, last attempt, last error and notes per source; last "seen" time per channel.

Phase 2 decisions and how they were built (25 Oct):
- One event per merged deadline: `ntulearn_gradebook:<column id>` (calendar and gradebook share the column id), with where it was found kept as a field. `ntulearn_calendar` is not used as a separate source.
- Stored text: title + the full body (light Markdown) + `content_hash`, so answers have the whole text without a live fetch. A ~280-character `preview` is used only in compact lists. Bodies are labelled UNTRUSTED wherever Claude sees them; the database is local and git-ignored.
- `pilot-mcp` adds `get_event(event_id)` (one item in full); `whats_new` returns changed items with full bodies.
- Status mapping: `not_submitted`/`unknown` → open, `submitted` → submitted, `graded` → graded, `exempt` → done. Never a grade.
- Sync window: deadlines −14 to +120 days, announcements and messages the last 30 days.
- Two comparisons: sync compares with the stored events and writes `event_changes` (detecting); `whats_new` reads `event_changes` after the channel's "last seen" bookmark (reporting).
- Freshness: the store is only as fresh as the last sync. Asking never triggers a fetch; every `pilot-mcp` answer states "as of <last sync time> SGT". "Right now" questions use the live `ntulearn-mcp` tools.
- Database code: built-in `sqlite3`, plain SQL, schema version in `PRAGMA user_version`; WAL mode; `save_events` runs in one transaction and takes the write lock first (`BEGIN IMMEDIATE`), so two syncs at once can't double-record.
- Removal safety (in `store/sync.py`): a stored event missing from a fetch is marked removed only if that sync covered it: same source, a current-term course, the course didn't fail this time (from Phase 1's `notes`), and inside the window by more than 1 hour. A failure not about one course (e.g. the calendar) means no removals for that source. Leaving the window isn't removal.
- A deadline whose status comes back `unknown` keeps the status already stored.
- The first sync of a source writes a `new` line for everything it finds; `whats_new` leaves that import out (it's "started watching", not news).
- Each sync takes ~1 min (~60 GET requests at ~1/s) and uses no Claude tokens.

### 5.2 `uni-copilot` CLI (scripts, no AI)
- ✓ `uni-copilot sync`: pull sources → upsert events → record changes; prints counts per source; exit code 1 if a source failed. Run by Windows Task Scheduler every 2 hours, 07:00–23:00 (✓ `scripts/schedule-sync.ps1`: windowless `pythonw`, catches up after a missed run, optional wake from sleep; output in `.data/sync.log` via `--log`).
- ✓ `uni-copilot status` ("as of" per source, errors, counts), `uni-copilot events` (deadlines and posts from the store), `uni-copilot changes` (the diary with titles). Phase 2.
- `uni-copilot remind`: find open deadlines (not submitted, not marked done) that have come within 24 h / 2 h → one Telegram message per run, grouped, soonest first, ending with "as of" → record in `notifications_sent`. Every 30 min, all day, at :10 and :40 (no quiet hours, decided 26 Oct). Tests are reminded like any deadline; their logistics come from the AI digest. Also one alert per failure streak when a sync attempt fails and the last success is over 6 h old. Store-only: never contacts NTULearn. `--dry-run` prints instead of sending. (Phase 3)
- `uni-copilot notify "<text>"` (or `-` for stdin): send a plain-text Telegram message, for scripts and testing. (Phase 3)
- `uni-copilot done <event_id>` (`--undo`): mark an item done locally. `uni-copilot telegram-chat-id`: one-off setup helper, before the plugin polls. (Phase 3)

### 5.3 `pilot-mcp` tools (local state only; never touches NTULearn)

Built in Phase 2 (✓): `src/uni_copilot/pilot_mcp/`, registered in `.mcp.json`, all five pre-allowed. Every result carries `as_of` (oldest last sync, SGT, minutes old, failed sources), and the server instructions make it **the default for study questions** (Claude says "as of", and uses the live `ntulearn` tools only for "right now" checks or what the store doesn't have).

- ✓ `list_events(days=7, kinds=None, course=None)`: deadlines due in the next N days + posts from the last N days, with previews.
- ✓ `whats_new(channel="terminal")`: changes since the channel last looked, with full bodies of new/edited posts, plus `checked_at`. Only reads.
- ✓ `get_event(event_id)`: one item with its full body.
- ✓ `mark_seen(channel, up_to=checked_at)`: moves the channel's "last seen" bookmark (only writes our own state).
- ✓ `sync_status()`: last sync, last error and notes per source, and counts.
- `add_manual_event(...)`: for forwarded messages or things I tell the bot; always confirmed first.
- `mark_done(event_id, undo=False)`: an item I've handled outside NTULearn; shown as "done (marked by you)", no more reminders. Local only, undoable, pre-allowed, but Claude confirms in the chat first and only when I asked. (Phase 3)
- `send_telegram(text)`: push a plain-text message, only ever to my own chat id from `.env`. Used by the morning digest. Pre-allowed. (Phase 3)
- `week_info(date)`: teaching week number, recess, holidays (from the academic calendar file).
- `link_calendar_event(event_id, gcal_event_id)`: bookkeeping for calendar sync.

---

## 6. Downstream: what I get

| # | Feature | How it runs | Uses AI? | Phase |
|---|---|---|---|---|
| 1 | **Ask from Telegram** ("what's due this week?", "what did the IE2108 prof say about the quiz?") | Claude Code with the Telegram channel plugin, running on my PC | Yes | 3 |
| 2 | **Morning digest** (only if something changed): today/tomorrow actions, new announcements in one line each, anything moved or edited, tests today/tomorrow with what to bring (inferred, with the source post) | Task Scheduler runs headless `claude -p` with the `morning-digest` skill, daily 07:30 → `pilot.send_telegram` (changed 26 Oct from a Desktop routine, which is the fallback) | Yes | 3 |
| 3 | **Deadline reminders**: 24 h and 2 h before unfinished items, day or night (no quiet hours) | `uni-copilot remind` via Task Scheduler, every 30 min | No | 3 |
| 4 | **Weekly actions checklist** with sources | `weekly-actions` skill (✓ Phase 2 in Claude Code; Telegram in Phase 3) | Yes | 2–3 |
| 5 | **Lecture notes**: transcript (corrected using slides) → notes, revision sheet, practice questions in my notes folder; Telegram "Week 10 notes ready" | Claude Code skill | Yes | 5 |
| 6 | **Calendar sync**: proposes deadlines for Google Calendar; I confirm by replying; dedupe via `[uni-copilot:<event_id>]` marker in the event description | Claude Code skill + Google Calendar connector | Yes | 6 |
| 7 | **Workload planner**: counts back from deadlines/exams, checks free time, proposes study blocks | Claude Code skill | Yes | 6 |
| 8 | **Weekly review** (Sunday): done, slipped, next week's load | Scheduled task | Yes | 8 |
| 9 | **Forward-to-add**: forward a group-chat message to the bot → proposed event → confirm | Telegram channel + `add_manual_event` | Yes | 7 |
| 10 | **Remote Control fallback**: continue a Claude Code session from the Claude phone app | `/remote-control` | Yes | any |

**Stretch: custom "NTULearn watcher" channel.** Claude Code lets you build your own channel (an MCP server that pushes events into a running session). The watcher would detect a new announcement or moved deadline and push it into the session; Claude decides whether it's worth a Telegram message. During the research preview, custom channels need `--dangerously-load-development-channels`. Phase 8.

---

## 7. How everything connects

### 7.1 Repo layout

```text
uni-copilot/
├── CLAUDE.md                      # short rules for Claude Code
├── docs/
│   ├── uni-copilot-project-plan.md
│   ├── how-it-runs.md             # what runs when, PC off / asleep, catch-up (Phase 3)
│   ├── phase 1/                   # phase-1-core.md (overview), phase-1-build-log.md (details)
│   ├── phase 2/                   # phase-2-core.md, phase-2-build-log.md
│   ├── phase 3/                   # phase-3-core.md, phase-3-build-log.md
│   └── phase 4/                   # phase-4-core.md, phase-4-build-log.md
├── scripts/
│   ├── schedule-sync.ps1          # registers the "uni-copilot sync" scheduled task (Phase 2)
│   ├── schedule-remind.ps1        # "uni-copilot remind" every 30 min (Phase 3)
│   ├── schedule-digest.ps1        # headless claude -p morning digest, 07:30 (Phase 3)
│   └── start-telegram.ps1         # opens the Telegram Q&A session (Phase 3)
├── src/uni_copilot/
│   ├── ntulearn_mcp/              # MCP server 1: live NTULearn + NTULearnVideo (read-only)
│   │   ├── server.py              # tools, prompts
│   │   ├── auth/session.py
│   │   ├── client/                # http.py, courses.py, calendar.py, gradebook.py,
│   │   │                          # announcements.py, messages.py, contents.py, video.py
│   │   ├── parsers/               # html.py; Phase 4: content.py, files.py
│   │   ├── cache.py               # Phase 4: .data/cache/content.sqlite
│   │   ├── search.py, embeddings.py, indexer.py, probe_content.py   # Phase 4
│   │   └── models.py
│   ├── store/                     # models.py, db.py (schema), events.py (upsert + change detection),
│   │                              # sync.py (one sync, removal rules), queries.py (read side)
│   ├── sources/                   # ntulearn.py (adapters + content_hash); later academic calendar, exams, timetable
│   ├── pilot_mcp/                 # MCP server 2: server.py + models.py (store; later manual events, notify)
│   ├── notify/telegram.py         # Bot API sendMessage, .env reader (Phase 3)
│   └── cli.py                     # uni-copilot sync | status | events | changes (Phase 3: remind | notify | done)
├── .claude/
│   ├── settings.json              # allow rules for read-only tools (+ pilot mark_seen)
│   └── skills/                    # weekly-actions (✓), morning-digest, calendar-sync,
│                                  # lecture-notes, workload-planner, weekly-review
├── data/                          # academic_calendar.yaml, exams.csv, timetable.yaml (my own, git-ignored)
├── tests/
│   └── fixtures/                  # synthetic only
├── spike_session_check.py         # login spike (keep until phase 1 is done)
├── .mcp.json
├── .gitignore                     # .auth/, .data/, .env, data/*
└── pyproject.toml
```

Local, never committed: `.auth/profile` (browser profile with login cookies), `.data/uni-copilot.sqlite`, `.data/cache/` (Phase 4 content cache and search index), `.env` (Telegram bot token, chat id, Phase 4: `GEMINI_API_KEY`), my notes folder (outside the repo), `~/Downloads/uni-copilot/` (files saved by `download_file`).

### 7.2 Wiring

1. **MCP servers** in `.mcp.json` (project scope):
   ```json
   {
     "mcpServers": {
       "ntulearn": { "command": "uv", "args": ["run", "ntulearn-mcp"] },
       "pilot":    { "command": "uv", "args": ["run", "pilot-mcp"] }
     }
   }
   ```
2. **Google Calendar**: claude.ai → Settings → Connectors → Google Calendar → Connect. Block "delete event". Check `/mcp` in Claude Code.
3. **Telegram bot**: create with BotFather. Same bot for both directions:
   - Q&A: `/plugin install telegram@claude-plugins-official`, `/telegram:configure <token>`, start with `claude --channels plugin:telegram@claude-plugins-official`, pair, then `/telegram:access policy allowlist`. Needs Bun.
   - Push: `notify/telegram.py` calls `sendMessage` with the same token and my chat id from `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; I type them in myself, never into a Claude chat). Sending doesn't conflict with the plugin's polling; never run a second poller (the one-off `telegram-chat-id` helper runs before the plugin is set up).
   - Format (decided 26 Oct): script messages use Telegram's HTML mode, tags added only by our code, all NTULearn text escaped, plain-text resend if Telegram rejects the formatting. The digest (`send_telegram`, written by Claude) is plain text. Link previews off.
4. **Permissions**: allow rules in `.claude/settings.json` for the `ntulearn` tools (except `login`) and the `pilot` tools (✓ Phase 2: the 4 read-only ones plus `mark_seen`, which only moves a local bookmark), so Telegram questions don't stall on prompts. Writes (`add_manual_event`, calendar writes) always ask. Never `--dangerously-skip-permissions`.
5. **Scheduling**:
   - Windows Task Scheduler: `uni-copilot sync` (every 2 h, 07:00–23:00; ✓ Phase 2 via `scripts/schedule-sync.ps1`), `uni-copilot remind` (every 30 min all day, at :10 and :40, Phase 3), `uni-copilot digest` (poked every 30 min at :05/:35; runs once a day after 07:30, syncing first if the store is over 3 h old; headless `claude -p`, no window; Phase 3, changed 28 Oct).
   - **Rule: poke often, decide in code** (28 Oct, after a 07:30 digest was skipped while the laptop slept). Schedulers only poke; Python decides from the store's state, so missed runs catch up and the scheduling glue stays thin and replaceable (cron/systemd on another host). One sign-in and one sync at a time via lock files (`src/uni_copilot/filelock.py`).
   - Hosting options considered 28 Oct: serverless (Vercel etc.) can't run this; a cloud VM might, but Microsoft sign-in from a data-centre IP is unverified and the session would live on a rented server. For now: this laptop plugged in (lid "do nothing", no sleep). Later options: an always-on device at home, or a cloud relay for reminders only (deadline titles and times, no credentials). Chosen over GitHub Actions: a cloud runner would need the Microsoft sign-in cookies and would sign in from data-centre IPs.
   - Digest prompt handles late runs ("if after noon, only today and tomorrow"). Fallback if headless runs misbehave: Claude Desktop → Code → Routines → New routine → Local. Weekly review (Sunday, Phase 8) uses whichever works.
   - "Keep computer awake" on; PC must be on for anything to run.
6. **Session for Telegram Q&A**: a terminal left open running `claude --channels ...` in the repo folder, started by `scripts/start-telegram.ps1` when I want it and restarted about once a day (Phase 3). Starting at logon later, once stable. **Locked to study Q&A** (27 Oct): `--tools Skill,ToolSearch` (no shell, file or web tools), claude.ai connectors and Chrome off (`ENABLE_CLAUDEAI_MCP_SERVERS=false`, `--no-chrome`), `ntulearn` `login` denied; it keeps pilot, the read-only ntulearn tools and the plugin's tools. Coding happens in normal sessions only.

### 7.3 Request flow examples
- **"What do I need to do this week?" (Telegram)** → channel plugin → Claude Code → `weekly-actions` skill → `pilot.list_events` (deadlines, recent posts) + `pilot.get_event` (full text of every post from the last 7 days) + `pilot.whats_new` → checklist with sources and "as of" → reply in Telegram. (✓ in the terminal, Phase 2; `ntulearn.search` for older material comes in Phase 4.)
- **Morning digest** → Task Scheduler → `claude -p` → `morning-digest` skill → `pilot.whats_new(channel="telegram")` → nothing changed: stop, no message; changed: Claude writes digest → `pilot.send_telegram` → `pilot.mark_seen(channel="telegram")`.
- **Reminder** → `uni-copilot remind` → store query → Telegram message. No AI.

---

## 8. Security and privacy rules

1. **Never store passwords.** I sign in myself in the Playwright window. Cookies stay in `.auth/profile` on my PC.
2. **Read-only on NTULearn.** GET only.
3. **Never print or store secrets:** cookie values, the Kaltura `ks` token in download links (fetch fresh links every time), the Telegram bot token, the Gemini API key, signed file links (Phase 4: the download redirects, and the `/bbcswebdav/...` links inside page bodies; read fresh each time, never cached).
   - **What may leave the PC for embeddings** (Phase 4): course content (page text, file text, titles) and search words only, to the embedding service chosen in `.env`. Never announcements, messages, grades or anything from the store about me. On Gemini's free tier, Google may use that text to improve its products (accepted 29 Oct for course content; billing turns it off).
   - **Files from NTULearn are untrusted:** saved only under the downloads folder with cleaned names, never overwritten, opened or run, and marked as from the internet. Not saved at all from Telegram.
4. **Grades are opt-in**, off by default, never cached to disk, never sent to Telegram, never used in workflows that write.
5. **Untrusted text.** Announcements, messages, file names, transcripts, emails and forwarded messages are written by other people. The agent never follows instructions inside them.
6. **Every write is confirmed** (calendar events, manual events). Text the agent reads can never trigger a write by itself.
7. **Telegram isn't end-to-end encrypted for bots.** Keep digests short: titles and dates, no grades, no other people's names where avoidable.
8. **Polite access:** ~1 request/s, max 2 concurrent, backoff, caching.
9. **Tests use synthetic fixtures only.** Real transcripts, grades and course files never go in the repo.
10. **Before sharing publicly:** check NTU's acceptable IT usage policy (Student Intranet / IT Service Desk).

---

## 9. Roadmap

Each phase ends with something usable.

**Phase 0: spike** (done)
- Playwright login; copied cookies work with plain `requests` for NTULearn and the video site.
- Done (24 Oct): silent sign-in from the saved profile on a later day.
- Done (24 Oct): 4-hour watch run. Both sessions stayed valid with calls every 10 min; Cloudflare didn't block plain `requests`. **Phase 0 complete.**

**Phase 1: `ntulearn-mcp` core** (done 24 Oct 2026)
- `login_status`, `login`, `list_courses`, `upcoming_deadlines` (with status), `recent_announcements`, `recent_messages`.
- Opens/closes windows moved to Phase 4 (need the content tree); dropped there after the probe (29 Oct).
- Done when: Claude Code answers "what's due this week?" correctly for all my courses. ✓ Checked against NTULearn on 24 Oct; one bug found and fixed (message senders showed as "GIVEN_NAME").

**Phase 2: event store** (done 25 Oct 2026)
- SQLite schema, `uni-copilot sync` (+ `status`, `events`, `changes`), change detection, `pilot-mcp` (`list_events`, `whats_new`, `get_event`, `mark_seen`, `sync_status`), `weekly-actions` skill (changed from prompt), Task Scheduler sync every 2 h.
- Done when: two syncs in a row report only real changes. ✓ 25 Oct: two scheduled runs in a row (22:03, 22:06) reported 0 changes; plain questions in a fresh Claude Code session went to pilot and the skill by themselves, and matched NTULearn.

**Phase 3: Telegram** (started 26 Oct 2026; steps and decisions in `docs/phase 3/phase-3-core.md`)
- Channel plugin for Q&A, `uni-copilot notify`, `uni-copilot remind`, morning digest (headless `claude -p`), `mark_done`, `send_telegram`.
- Done when: I get a correct digest and reminders for a week without opening NTULearn.

**Phase 4: content and search** (started 29 Oct 2026, in parallel with Phase 3's test week; steps and decisions in `docs/phase 4/phase-4-core.md`)
- Live probe first (done 29 Oct); `list_course_content`, `read_page`, `read_file_text` (file items and files embedded in pages), `get_assessment` (with release conditions; `upcoming_windows` dropped after the probe), hybrid `search` (FTS5 + Gemini embeddings, keyword-only fallback), indexer run by hand, `download_file` (terminal only), caching; `send_course_file` to Telegram after the test week.
- Done when: content questions ("what does this week's lecture cover?", "where did they say what the quiz covers?") are answered from the files with sources; meaning search finds what keywords miss, and still answers keyword-only when Gemini is unavailable; Phase 3 keeps running untouched.

**Phase 5: recordings and study pipeline**
- `list_recordings`, `get_transcript`, lecture-notes skill (slides correct the transcript).

**Phase 6: Google Calendar**
- Calendar-sync skill with confirmation and dedupe; workload planner.

**Phase 7: more sources**
- Academic calendar, exam timetable, class timetable, forward-to-add, Outlook (if the connector works with NTU).
- Exams get their own reminder in `remind` (noted 27 Oct): `kind = exam` with `starts_at`, worded "Exam in 2 hours: course, venue, what to bring", never "not submitted". Until then, only NTULearn gradebook items are reminded; on 27 Oct all test-like ones were online quizzes taken on NTULearn (4 of 35), where "not submitted" is right. A paper test that appears as a gradebook deadline can be silenced with `uni-copilot done`.

**Phase 8: stretch**
- Custom NTULearn watcher channel, discussions, groups, module progress, weekly review.

**Phase 9: share**
- README, install guide, policy check, packaging.

---

## 10. Testing

- Offline unit tests with **synthetic** fixtures (made-up courses, announcements, a small fake transcript JSON).
- Parsers tested on fixture HTML: Ultra document bodies, embedded-video links, locked items.
- Store tests: dedupe, edits, moved deadlines, removed items, reminder de-duplication. ✓ Phase 2: `tests/test_store.py`, `test_sources.py`, `test_sync.py` (failure rules with fake clients), `test_queries.py`, `test_pilot.py`. ✓ Phase 3: `test_reminders.py` (tiers, send once, moved, sync-alert streaks, message layout, `remind` / `done`), `test_telegram.py` (fake Bot API session: splitting, escaping, fallback, retries, token never in errors, "💭 Thinking…" hooks), pilot `mark_done` / `send_telegram` / `delete_telegram_message`. 176 offline tests.
- Opt-in live smoke tests (my account, GET only) that print counts and status codes only.
- MCP Inspector (`uv run mcp dev ...`) for manual tool checks.

---

## 11. Open questions

- Session: how long a session survives with no calls, or with calls 2 h apart (the sync interval). Silent sign-in, 10-min keepalive and Cloudflare were verified 24 Oct. Each sync is a new process that signs in silently from the profile anyway, so this matters less now; `.data/sync.log` over the next days will show any sign-in failures.
- Whether the laptop wakes from sleep for a scheduled sync (`schedule-sync.ps1 -Wake`).
- NTU's rules on automated access to NTULearn (read-only, paced, own account): not checked yet; Phase 9 policy check.
- ✓ Claude picks the `weekly-actions` skill and pilot by itself from Telegram too (27 Oct, Phase 3 step 11).
- ✓ Discussions, Groups, content availability windows, recursive listing, attachment fields, the download redirect, assessment fields, "viewed" state: Phase 4 probe, 29 Oct (Appendix A). ✓ Follow-up (29 Oct): `PartiallyVisible` is not locked; pages answer 400 on the attachment list (their files are embedded in the body: 283, 126 PDFs); release rules with start/end live on the internal item. ✓ Second follow-up: release criteria are 134 "after another item" and 8 date ranges (none still to come); the internal folder list carries the rules; embedded files with a `resourceUrl` (39 of 283) download like attachments. ✓ Third follow-up: `viewerUrl` is a signed `/bbcswebdav/` file link too, and downloads the same way. Still open: 38 embedded files with no link in their JSON (maybe in the `<a href>`), checked in Phase 4 step 9. Whether NTULearn's server-side activity logs record API reads can't be seen from the API.
- Gemini embeddings (Phase 4): how batch calls count toward the daily limit, the per-minute vs daily limit errors, when the daily limit resets, the real token count of my files.
- Whether reading `conversations` via the API marks messages as read (I had 0 unread when tested).
- Past-due gradebook columns with no row for me: missed work, or items with nothing to submit? (2 cases on 24 Oct.)
- The visible-window `login()` path: same code as the spike's first sign-in, but never needed since the build (silent sign-in always worked). Test it when the Microsoft "stay signed in" cookie expires.
- Whether NTULearn sends students announcements that are drafts or outside their Restricted window (`visible_now` filters them anyway).
- Calendar items that aren't gradebook columns (course events, personal entries): none seen yet; they would show status `unknown`.
- Why IE2106 "Assignment 1" is missing from the calendar.
- Whether the grades endpoint ever shows unreleased grades.
- What the other `x-bb-blti-link` items are (other external tools).
- Whether other courses embed videos with transcripts.
- Telegram channel plugin: does it support inline buttons? Can its reply tool be used from a scheduled run? (Design assumes reply-to-confirm and our own `sendMessage`, so neither is needed.) ✓ 27 Oct (Phase 3 step 11): runs on Windows with Bun (the plugin's `bun install` had to succeed first); takes `TELEGRAM_BOT_TOKEN` from the environment, so `scripts/start-telegram.ps1` passes it from `.env` and `/telegram:configure` isn't needed; picks pilot and `weekly-actions` from Telegram. Permission relay isn't documented for this plugin: prompts wait at the PC.
- ✓ Headless `claude -p` from Task Scheduler (Phase 3 step 10, 27 Oct): loads the project MCP servers, `.claude/settings.json` and skills; per-run `--allowedTools` / `--disallowedTools`; uses the Claude plan. A PowerShell window may flash briefly.
- Outlook connector availability with NTU's Microsoft tenant.
- Official sources and formats for the academic calendar and exam timetable.

---

## Appendix A: Verified NTULearn facts (23–25 Oct 2026, my account, GET only)

**Platform and login**
- NTULearn = Blackboard Learn Ultra at `https://ntulearn.ntu.edu.sg`. Login via Microsoft Entra ID (SAML). "Stay signed in" makes re-login silent in my everyday Chrome.
- Cookies: `BbRouter` (session cookie, gone when the browser closes), `JSESSIONID`, `AWSALB` (~7 days), `samlCookie`.
- Idle timeout ~3 h (`GET /learn/api/v1/utilities/timeUntilBbSessionInactive`). Reading it doesn't reset the timer. API calls 1–2 min after a reset didn't reset it either, but one after ~23 min idle did, so "last activity" seems to update only every so often.
- Login check: `GET /learn/api/v1/users/me` → 200 / 401.
- Playwright spike: first run (23 Oct) needed manual sign-in (37 s); cookies copied into `requests` worked for NTULearn and the video site. Video site sets `__cf_bm` (Cloudflare, ~30 min).
- Always use the full base URL; relative fetches hit a CDN host and 404.
- **Silent sign-in verified (24 Oct, ~13 h after the first run):** the saved profile signed in by itself in 3 s with no input. Copied cookies again worked for both sites.
- **4-hour watch run (24 Oct, 10:12–14:12 SGT):** cookies copied once into `requests`, browser parked on a blank page, one `users/me` + one transcript-list call every 10 min. All 24 checks returned 200 for both sites. So calls every 10 min keep the NTULearn session alive past the 3-hour idle limit, and the video site kept working well after `__cf_bm` expired (10:42). Likely because `requests.Session` stores refreshed cookies from responses; not checked.
- Not tested: how long a session survives with **no** calls (assume ~3 h), or with calls spaced 2 h apart.
- Loading `/ultra/course` makes NTULearn's own page send `PUT /learn/api/v1/utilities/batch` (the web app's request batching, not ours). The login flow leaves the page as soon as `users/me` is 200.
- 24 Oct: silent sign-in also works in a **headless** browser with the same profile: 6 s typical (19 s on a cold first start). Cookies exported: `AWSALB`, `BbRouter`, `samlCookie`.

**Courses and terms**
- Memberships: `/learn/api/v1/users/{userId}/memberships?expand=course&limit=100`. Response has `paging` (`nextPage`). Course has `displayId`, `displayName`, `id`, `isOrganization`, `isAvailable`, `isClosed`, embedded `term`.
- Terms have names (`26S1`, `25S2`, `Non-curriculum`, …). The `term` object has `startDate`/`endDate` fields but they are empty (`durationType` CONTINUOUS). Current term = highest `YYS[1|2]` name (with override).
- `displayId` = `26S1-<CODE>-<SECTION>`. 16 sites this term = 8 subjects (1–3 sites each), no organizations in 26S1.
- NTU marks no site as `isOrganization` (0 of 57). Extra sites sit in a term named `Non-curriculum` (18 sites, 14 available, assorted `displayId` formats); 1 site has no term. Course names are all upper case.

**Deadlines**
- `/learn/api/public/v1/calendars/items?since=&until=` matched NTULearn's Due dates view exactly (34/34). Max range 16 weeks (17 weeks → 400). Item fields: `calendarId`, `calendarName`, `id`, `type`, `title`, `start`, `end`, `modified`, `dynamicCalendarItemProps`. 24 Oct: all items in ±14 days had `type` = `GradebookColumn`.
- Calendar item `id` = gradebook column `id`; `end` = `grading.due`.
- 24 Oct: `calendarId` = the course site id (30/30 items in the next 20 weeks); `start` = `end` for every GradebookColumn item. A 20-week range split into 16 + 4 week windows returned 30 items, matching the 30 future-due gradebook columns from the probe.
- 24 Oct: the calendar returns items due earlier on the `since` day too (since = 18:46 SGT returned items due 11:17 and 15:30 SGT the same day), so filter by exact due time after fetching. With that filter, calendar and gradebook agreed on all 28 items in the next 20 weeks (0 gradebook-only, 0 calendar-only).
- Gradebook statuses 24 Oct (16 sites): future-due 29 not submitted; past-due 44 graded, 13 submitted (NeedsGrading), 2 no row. My-row entries can also have `exempt: true` (mapped to status `exempt`; not seen yet).
- Gradebook columns: `/learn/api/public/v2/courses/{id}/gradebook/columns` (`grading.due`, `grading.type`, `availability`). 200 on all 16 sites, 88 columns with a due date. One item (IE2106 Assignment 1) had a due date but wasn't in the calendar → merge both sources.
- Submission status: my gradebook row (`/learn/api/public/v2/courses/{id}/gradebook/users/{userId}`) `status` is `NeedsGrading` (submitted, not marked) or `Graded`; no row = nothing submitted. 24 Oct, future-due columns: 29 no row, 1 NeedsGrading.
- Attempts also readable by me: `/learn/api/public/v1/courses/{id}/gradebook/columns/{columnId}/attempts` → `id`, `status`, `created`, `exempt`, `userId` (no score). One request per column, so only a fallback.

**Announcements and messages**
- Announcements: `/learn/api/public/v1/courses/{id}/announcements` → `id`, `title`, `body` (HTML), `created`, `modified`, `availability`, `draft`, `creator`, `position`.
- 24 Oct (16 sites, 113 posts): newest first by `created` (`position` 1..n). Default page size 10, `nextPage` = `.../announcements?offset=10`; `limit=100` fits every site in one call (max 27). `sort=created(desc)` → 400. `creator` is a user id string (no name). No drafts. `availability.duration.type` = `Permanent` (73) or `Restricted` with `start`/`end` (10). `modified - created` < 1 min for 63 of 83; 20 edited later; some posts created months ago were edited recently. No post duplicated across sites.
- Announcement HTML: mostly `p`, `td`, `span`, `br`, `strong`, `tr`, `li`, `a`, `ul`, `em`; tables common, often with `<p>` in cells. Attached files: `<a data-bbtype="attachment" data-bbfile='{...}'>`, JSON keys `linkName` (26/26), `displayName`, `fileName` (22/26), `mimeType`, `fileSize`, `resourceUrl`, `alternativeText`, `render`. The internal `/learn/api/v1/courses/{id}/announcements` returns the same items plus `readStatus`, `startDateRestriction`, `endDateRestriction`.
- Course Messages: `/learn/api/v1/courses/{id}/conversations` (internal, undocumented) → conversations with `id`, `createdDate`, `updatedDate`, `unreadCount`, `totalCount`, `messages[]` (`body`, `postDate`, `sender`, `senderId`, `isRead`, `type`). `/courses/{id}/messages` → 404; public v1 → 404.
- `/learn/api/v1/messages/summary`: per-course `numUnreadMessages`, `isCourseMessagesEnabled`, `termName`.
- 24 Oct (16 sites): `conversations` returned 200 everywhere, 9 conversations, no paging. Each conversation's `messages` holds only the newest message (a preview); `totalCount` is the thread length (25 in total). Full thread: `GET /learn/api/v1/courses/{id}/conversations/{conversationId}/messages` (list with `paging`, oldest first); `GET .../conversations/{conversationId}` also returns the full `messages`. `recipientCategory` is a list.
- Message `body` is an object: `rawText`, `displayText` (both HTML; same text after parsing for 25/25), `webLocation`, `fileLocation`. `sender`: `id`, `userName`, `givenName`, `familyName`, `preferredDisplayName`, `title`, `avatar`. `preferredDisplayName` is a display setting, not a name (`GIVEN_NAME` on all 25 messages and on `users/me`); the name is `givenName` + `familyName` (filled on 25/25, 6 distinct senders). `type`: `NORMAL`. Attachments use the same `data-bbfile` markup as announcements.

**Content**
- Children: `/learn/api/public/v1/courses/{courseId}/contents/{contentId}/children`. Attachments and download under `.../contents/{contentId}/attachments[/{id}/download]`.
- Ultra "Document" page = `x-bb-folder` with one `x-bb-document` child titled `ultraDocumentBody`; its `body` is the page HTML.
- Locked items: availability `PartiallyVisible`, empty body.
- Survey of 16 sites: PDF 156, mp4 9, PPTX 7, docx 4, ipynb 4; 109 quiz links; large trees (cache).

**Content, Phase 4 probe (29 Oct, 16 sites, 108 GETs in 106 s, counts and field names only; `probe_content.py`)**
- **Whole tree in one request:** `GET /learn/api/public/v1/courses/{id}/contents?recursive=true` → 200 on 16/16; every item has `parentId`; no site needed more than 10 pages of 200. 11 sites returned more than their top level (930 items), 5 had only a top level. 966 items in total.
- Item types: `x-bb-folder` 283, `x-bb-file` 222, `x-bb-document` 221, `x-bb-asmt-test-link` 119, `x-bb-lesson` 48 (learning modules: `forcedSequence`), `x-bb-blti-link` 35 (external tools, incl. Turnitin settings in `customParameters`), `x-bb-externallink` 29, `x-bb-bltiplacement-NewUCVGalleryLTI_1_1` 5, `x-bb-asmt-survey-link` 4. No discussion items.
- Item fields: `id`, `parentId`, `title`, `created`, `modified`, `position`, `availability` (`available`, `adaptiveRelease`, `allowGuests`, `allowObservers`), `contentHandler`, `links[]`, `launchInNewWindow`, `reviewable` (939), `hasChildren` (331), `hasGradebookColumns` (140), `description` (178), `body` (212). Files: `contentHandler.file.fileName` / `mimeType` (222/222). Tests: `contentHandler.assessmentId`, `gradeColumnId` (119/119), `password.enabled`, `proctoring.secureBrowserRequiredToTake` / `webcamRequired`, `isLateAttemptCreationDisallowed`; `contentHandler.instructions` on 10 items; `grading.due` on 12.
- Ultra pages: 136 folders with exactly one `x-bb-document` child titled `ultraDocumentBody`; 212 of 221 documents have a body.
- **No date windows:** `availability.adaptiveRelease` is present on all 966 items but has no `start`/`end` on any. `available`: `Yes` 745, `PartiallyVisible` 221 (151 of them with an empty body). The internal item shows no start/end fields either (only `permissions.adaptiveRelease.*`, what the viewer may do). 4 Turnitin items carry `customParameters.startdate` / `duedate` / `available_from` / `available_to`.
- Attachments (`.../contents/{id}/attachments`): 200 on 19 items, 400 on 4 (which kind: follow-up). Fields: `id`, `fileName`, `mimeType` only (no size, no modified time). Mime types: pdf 15, zip 2, `application/octet-stream` 2.
- **File download:** `.../attachments/{id}/download` → 302 to `ntulearn.ntu.edu.sg` (signed query string; with cookies) → 302 to `alt-<id>.blackboard.com` (signed; works **without** cookies) → 302 on the same host → 200 `application/pdf`, **no `Content-Length`**. 4 requests per file.
- **Reading doesn't mark as viewed** (as far as the API shows): the internal item's tracking-looking fields (`isReviewable`, `permissions.*ReviewState`, `viewerUrl`) were the same before and after a download.
- Assessments: the public gradebook column (`/learn/api/public/v2/courses/{id}/gradebook/columns/{columnId}`) gives `grading.attemptsAllowed`, `grading.due`, `availability.available`. The internal item (`/learn/api/v1/courses/{id}/contents/{contentId}`) gives `contentDetail.<handler>.test.deploymentSettings` (`attemptCount`, `timerCompletion`, `isDueDateEnforced`, `isLateAttemptCreationDisallowed`), `test.gradingColumn.dueDate`, `genericReadOnlyData.dueDate`. No time-limit field on the 3 probed.
- **Follow-up (29 Oct, 43 GETs):**
  - `PartiallyVisible` = "visible with release conditions", **not locked**: folder 80, document 79, test link 54, file 3, survey 2, LTI 2, external link 1. 3 of 3 such folders opened (children 200), 2 of 2 such files listed their attachments (200). 79 of the 85 items inside such folders are the `ultraDocumentBody` documents of Ultra pages.
  - **Release rules exist, on the internal item only:** `/learn/api/v1/courses/{id}/contents/{contentId}` has `adaptiveReleaseRules.rules[]` (`id`, `title`, `type`, `criteria[]` with `id`, `type`, `negate`, `start`, `end`), plus `visibility`, and on some kinds `genericReadOnlyData.restrictions.contentVisibility.*.reasonCodes`. Folders also have `stateCompletion.started`. One request per item; what the criteria contain: second follow-up.
  - **Pages have no attachment list:** `.../attachments` → 400 on `x-bb-document` items (3/3 page bodies, 3/3 other documents), 200 on `x-bb-file` (5/5). That explains the first run's 4 × 400.
  - **Files embedded in page bodies: 283 on 164 pages** (the `data-bbfile` markup announcements use): `application/pdf` 126, no mime type 79, `application/octet-stream` 31, `text/csv` 20, `text/plain` 8, zip 5, images 7, PPTX/PPT/PPS 4, mp4 2, docx 1. `data-bbtype` values found: `embedded-app` 73, `attachment` 11, `video` 6. So many slides live inside Ultra pages, not as file items. How they download: second follow-up.
  - Recursive listing: 0 sites with pages left over.
- **Second follow-up (29 Oct, 171 GETs):**
  - Release rules on all 142 `PartiallyVisible` items (page bodies left out); visible items have none (0 of 5). Every rule `type=User`. Criteria: **`Pre_Content` 134** ("after another item"), **`Date_Range` 8** (8 with a start, 1 with an end; 7 open now, 1 closed, **0 still to come**); none negated. The criterion fields don't name the other item.
  - The internal `visibility` says whether *I* meet the rule: `VISIBLE` 124, `PARTIALLY_VISIBLE` 18 (really held back from me).
  - The internal folder list (`/learn/api/v1/courses/{id}/contents/ROOT/children`) carries `adaptiveReleaseRules` on its items without any `expand` (1 of 17 root items on the first site), so listing one folder gives its rules in 1 request.
  - Embedded files (283): `<a>` tags with no `data-bbtype` 193, `attachment` 11, `embedded-app` 73, `video` 6. JSON fields: `linkName` / `mimeType` 204, `render` 201, `viewerUrl` 158, `alternativeText` 79, `displayName` 70, `resourceUrl` 39, `fileName` / `fileSize` 7 (so almost never a size). Extensions: pdf 98, ipynb 31, csv 20, txt 8, zip 5, pptx/ppt/pps 4, images 6, mp4 2, docx 1, none 107.
  - `resourceUrl` (39): `https://ntulearn.ntu.edu.sg/bbcswebdav/...` with a query string (35) or `/sessions/...` (4). One PDF: 302 → `alt-<id>.blackboard.com` (signed, no cookies) → 302 → 200 `application/pdf`, no `Content-Length`.
- **Third follow-up (29 Oct, 22 GETs), embedded files' links:**
  - `viewerUrl` and `resourceUrl` are the **same kind of link**: `https://ntulearn.ntu.edu.sg/bbcswebdav/<long>/xid-N_N?<3 signed parameters>`; `viewerUrl` adds `isInlineRender`, `locale`, `render`, `xythos-download`. A viewer-only PDF: 302 → `alt-<id>.blackboard.com` (signed, no cookies) → 302 → 200 `application/pdf`, no `Content-Length`. So every embedded file with either link downloads the same way (3 requests).
  - By tag: no `data-bbtype` 193 (`viewerUrl` 156, `resourceUrl` 32, both 30, **only `mimeType`, no link: 35**); `attachment` 11 (3 with no link); `embedded-app` 73 (only `url`: videos and tools, Phase 5); `video` 6 (no link fields). The 38 without a link may carry it in the `<a href>` (not checked).
  - 4 `resourceUrl`s are images under `/sessions/...`.
  - **The signed parameters are secrets**, like Kaltura's `ks`: read fresh from the page body each time, never stored, logged or shown. Phase 1's `parse_html` already turns every `<a data-bbfile>` into `[file: name]` without its link.
- Discussions: `public v1 .../discussions` → 200 with 0 items on 16/16; `internal v1 .../discussions` → 404. Groups: `public v2 .../groups` → 200 on 16/16, **193 groups**, fields `name`, `availability.available`, `enrollment.type`, `enrollment.limit`, `enrollment.signupSheet` (`name`, `description`, `showMembers`), `groupSetId`; `public v1 .../groups` → 200 with 0 items.

**Recordings**
- Media Gallery: content handler `resource/x-bb-bltiplacement-NewUCVGalleryLTI_1_1`; `GET /learn/api/public/v1/lti/placements?handle=NewUCVGalleryLTI_1_1` → `_1327_1`.
- Launch (Playwright): `/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_1327_1&content_id=<id>&course_id=<id>&wrapped=true` → `https://ntulearnv1.ntu.edu.sg/channel/<name>/<id>`.
- List: links `/media/t/<entryId>/<channelId>`, 15 per page; next pages via `/channel/<name>/<id>/sort/recent/sortBy/recent/pageSize/15/fields/all/created/any/updated/any?format=ajax&page=N`.
- Transcripts: `GET /attachments/index/index/entryid/<entryId>/entryView/1?format=ajax` → download links on `api.sg.kaltura.com/.../attachment_attachmentasset/action/serve/attachmentAssetId/<id>?ks=<token>` (works without cookies; never store the token).
- All 51 gallery recordings (5 sites) have `.txt` + `.json`. Use JSON: `w`, `s`/`e` (ms), `a` (confidence). Auto-captions mishear terms ("breadth-first" → "breast research"); correct with slides.
- Module-embedded videos (IE2110): `ultraDocumentBody` has `<a data-bbtype="embedded-app" data-bbfile='{...}'>`, placement "Kaltura Media" (`_1326_1`), URL `.../browseandembed/index/media/entryid/<entryId>/...`. The 4 checked had no transcripts or captions.

**Phase 2 findings (25 Oct)**
- Bodies: of 114 announcements (180 days), the longest was ~4000 characters (median ~770); of 25 messages, longest ~3800 (median ~340). Phase 1's 4000-character cut hit 1 announcement, so the limit is now 20,000 (a safety net only).
- A full sync (deadlines −14/+120 days, announcements and messages 30 days) takes ~63 s: deadlines 27 s, announcements 16 s, messages 20 s. 34 deadlines, 31 announcements, 15 messages.
- `content_hash` is stable across real re-fetches: two syncs in a row → 0 changes (several times, including two scheduled runs). Cross-posted announcements kept the same id.
- An announcement that passed 30 days old stopped being fetched and stayed in the store as active (not "removed"), as designed.
- A windowless `pythonw` run and a run started by Task Scheduler both signed in silently from the saved profile (exit code 0).

**Grades (structure only)**
- `GET /learn/api/public/v2/courses/{courseId}/gradebook/users/{userId}`: `columnId`, `status`, `displayGrade`, `exempt`, `changeIndex`, `firstRelevantDate`, `lastRelevantDate`.

## Appendix B: Tech stack

- Python ≥ 3.12 with `uv`. Windows + PowerShell.
- MCP Python SDK `mcp>=2.3,<3`: `from mcp.server import MCPServer`; `@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))` (`from mcp.types import ToolAnnotations`); `mcp.run(transport="stdio")`; raise `ToolError` for errors the model should see.
- Playwright (Chromium), `requests`, SQLite (stdlib; FTS5 for search, checked 29 Oct: SQLite 3.53.1), pypdf (chosen over PyMuPDF for its licence; pypdfium2 as fallback), python-pptx, numpy (vector maths), PyYAML.
- Embeddings (Phase 4): Google Gemini API `gemini-embedding-001` over plain `requests`, free tier, own Google Cloud project; swappable for OpenRouter or `fastembed` (local).
- Claude Code: channels (Telegram plugin, research preview, needs Bun), Desktop local scheduled tasks, Remote Control, Google Calendar connector.
- Telegram Bot API (free) for push messages.
