# uni-copilot: project plan

A personal academic agent for NTU students. It reads NTULearn (and later other sources), keeps track of what changed, and tells you what to do, in Telegram or Claude Code. It runs on your own computer with your own login, and never stores passwords.

Status as of 23 Oct 2026: planning done, login spike passed, nothing built yet. "Verified" below means checked on my own account (GET only); "Not verified" means designed but not yet tested.

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
- A paid AI API. Everything AI runs through my existing Claude plan via Claude Code.
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

**Session**
- `login_status()`: is there a working session (`users/me` → 200)?
- `login()`: opens a visible browser window so I sign in myself (handles MFA).

**Courses**
- `list_courses(term="current", include_organizations=False)`: current-term course sites grouped by subject code (e.g. IE2108 → lecture site + my tutorial site), with aliases ("DSA" → IE2108).

**Deadlines and time**
- `upcoming_deadlines(days=7, course=None)`: calendar API (all event types, ≤16-week windows) ∪ gradebook due dates, deduped by column id, each with submission status (not started / submitted / graded), plus "opens" and "closes" entries from content availability windows.
- `get_assessment(course, content_id)`: instructions, due date, attempts allowed, time limit, my status.

**News**
- `recent_announcements(since_days=21, course=None)`: includes edited ones; duplicates across lecture/tutorial sites merged.
- `recent_messages(since_days=14, course=None)`: course Messages (some profs use these instead of announcements).

**Content**
- `list_course_content(course, folder_id=None)`: one folder level; locked items marked "locked".
- `read_page(course, content_id)`: text of an Ultra document page.
- `search(query, course=None)`: over announcements, messages, page text, cached file text and titles.
- `read_file_text(course, content_id, attachment_id, pages=None)`: PDF first, PPTX second.
- `download_file(course, content_id, attachment_id, dest_dir)`: saves locally; changes nothing on NTULearn.

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
- `weekly_actions`: deadlines not yet submitted + announcements (~3 weeks) + messages + a logistics search for each test this week ("matric", "passport", "calculator", "venue", "bring") + opening/closing windows → checklist with sources; inferred items marked as inferred.
- `whats_new`: what changed since the last check.
- `prep_for_lecture`: this week's slides + transcript summary for one course.

### 4.3 Internal layout
- `auth/session.py`: Playwright persistent profile, login, silent re-login, cookie export.
- `client/http.py`: requests session, base URL, ~1 req/s, max 2 concurrent, retries with backoff on 429/5xx, 401 → silent re-login.
- `client/*.py`: one module per area (courses, calendar, gradebook, announcements, messages, contents, video).
- `parsers/`: HTML → text, Ultra document bodies, embedded-video detection, transcript paragraphs.
- `cache.py`: content tree and file text cache with expiry.
- `models.py`: typed results.

---

## 5. Core component B: event store and `pilot-mcp`

### 5.1 Event store (SQLite, local, git-ignored)

Every source is converted into one **event** shape:

| Field | Meaning |
|---|---|
| `event_id` | `<source>:<native id>` (e.g. `ntulearn_gradebook:_1183007_1`), the dedupe key |
| `source` | ntulearn_calendar, ntulearn_gradebook, ntulearn_announcement, ntulearn_message, ntulearn_window, kaltura_recording, outlook, academic_calendar, exam_timetable, timetable, manual |
| `kind` | deadline, test, exam, class, announcement, message, opens, closes, grade_released, recording_available, holiday |
| `course_code` / `course_site_id` | e.g. IE2108 / `_2706657_1` |
| `title`, `summary` | short text (no full bodies of untrusted text) |
| `starts_at`, `due_at` | stored in UTC, shown in SGT |
| `status` | open, submitted, graded, locked, done, cancelled |
| `url` | link back to the source |
| `content_hash` | detects edits |
| `first_seen_at`, `last_seen_at`, `modified_at` | change tracking |

Other tables:
- `event_changes`: what changed and when (new, edited, moved, removed). Powers `whats_new` and the digest.
- `notifications_sent`: which reminder went out for which event, so nothing is sent twice.
- `calendar_links`: `event_id` ↔ Google Calendar event id.
- `state`: last sync time per source, last "seen" time per channel.

### 5.2 `uni-copilot` CLI (scripts, no AI)
- `uni-copilot sync`: pull sources → upsert events → record changes. Run by Windows Task Scheduler (e.g. every 2 hours, 07:00–23:00).
- `uni-copilot remind`: find unfinished deadlines due in 24 h / 2 h and tests tomorrow → send Telegram messages → record in `notifications_sent`. Every 30 min.
- `uni-copilot notify "<text>"`: send a Telegram message (used by the digest task).

### 5.3 `pilot-mcp` tools (local state only; never touches NTULearn)
- `list_events(days=7, kinds=None, course=None)`: from the store, fast.
- `whats_new(since="last_seen", channel="telegram")`: changes since the channel last looked.
- `add_manual_event(...)`: for forwarded messages or things I tell the bot; always confirmed first.
- `mark_done(event_id)`: hide an item I've handled outside NTULearn.
- `send_telegram(text)`: push a message to my chat.
- `week_info(date)`: teaching week number, recess, holidays (from the academic calendar file).
- `link_calendar_event(event_id, gcal_event_id)`: bookkeeping for calendar sync.

---

## 6. Downstream: what I get

| # | Feature | How it runs | Uses AI? | Phase |
|---|---|---|---|---|
| 1 | **Ask from Telegram** ("what's due this week?", "what did the IE2108 prof say about the quiz?") | Claude Code with the Telegram channel plugin, running on my PC | Yes | 3 |
| 2 | **Morning digest** (only if something changed): today/tomorrow actions, new announcements in one line each, anything moved or edited | Claude Desktop local scheduled task, weekdays ~07:30 → `uni-copilot notify` | Yes | 3 |
| 3 | **Deadline reminders**: 24 h and 2 h before unfinished items; "test tomorrow: bring matric card" the evening before | `uni-copilot remind` via Task Scheduler | No | 3 |
| 4 | **Weekly actions checklist** with sources | `weekly_actions` prompt, from Telegram or Claude Code | Yes | 2–3 |
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
│   └── uni-copilot-project-plan.md
├── src/uni_copilot/
│   ├── ntulearn_mcp/              # MCP server 1: live NTULearn + NTULearnVideo (read-only)
│   │   ├── server.py              # tools, prompts
│   │   ├── auth/session.py
│   │   ├── client/                # http.py, courses.py, calendar.py, gradebook.py,
│   │   │                          # announcements.py, messages.py, contents.py, video.py
│   │   ├── parsers/
│   │   ├── cache.py
│   │   └── models.py
│   ├── store/                     # SQLite schema, upserts, change detection
│   ├── sources/                   # adapters: ntulearn → events, academic calendar, exams, timetable
│   ├── pilot_mcp/                 # MCP server 2: store, manual events, notify
│   ├── notify/telegram.py         # Bot API sendMessage
│   └── cli.py                     # uni-copilot sync | remind | notify
├── .claude/
│   ├── settings.json              # allow rules for read-only tools
│   └── skills/                    # weekly-actions, morning-digest, calendar-sync,
│                                  # lecture-notes, workload-planner, weekly-review
├── data/                          # academic_calendar.yaml, exams.csv, timetable.yaml (my own, git-ignored)
├── tests/
│   └── fixtures/                  # synthetic only
├── spike_session_check.py         # login spike (keep until phase 1 is done)
├── .mcp.json
├── .gitignore                     # .auth/, .data/, .env, data/*
└── pyproject.toml
```

Local, never committed: `.auth/profile` (browser profile with login cookies), `.data/uni-copilot.sqlite`, `.data/cache/`, `.env` (Telegram bot token, chat id), my notes folder (outside the repo).

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
   - Push: `uni-copilot notify` calls `sendMessage` with the same token and my chat id from `.env`. Sending doesn't conflict with the plugin's polling; never run a second poller.
4. **Permissions**: allow rules in `.claude/settings.json` for all `ntulearn` tools and read-only `pilot` tools, so Telegram questions don't stall on prompts. Writes (`add_manual_event`, calendar writes) always ask. Never `--dangerously-skip-permissions`.
5. **Scheduling**:
   - Windows Task Scheduler: `uni-copilot sync` (every 2 h), `uni-copilot remind` (every 30 min).
   - Claude Desktop → Code → Routines → New routine → Local: morning digest (weekdays), weekly review (Sunday). Prompt handles late runs ("if after noon, only today and tomorrow").
   - "Keep computer awake" on; PC must be on for anything to run.
6. **Always-on session for Telegram Q&A**: a terminal left open running `claude --channels ...` in the repo folder.

### 7.3 Request flow examples
- **"What do I need to do this week?" (Telegram)** → channel plugin → Claude Code → `pilot.list_events` + `ntulearn.recent_announcements` + `ntulearn.search` for test logistics → checklist with sources → reply in Telegram.
- **Morning digest** → scheduled task → `pilot.whats_new(channel="telegram")` → nothing changed: stop, no message; changed: Claude writes digest → `uni-copilot notify`.
- **Reminder** → `uni-copilot remind` → store query → Telegram message. No AI.

---

## 8. Security and privacy rules

1. **Never store passwords.** I sign in myself in the Playwright window. Cookies stay in `.auth/profile` on my PC.
2. **Read-only on NTULearn.** GET only.
3. **Never print or store secrets:** cookie values, the Kaltura `ks` token in download links (fetch fresh links every time), the Telegram bot token.
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

**Phase 0: spike** (in progress)
- Done: Playwright login; copied cookies work with plain `requests` for NTULearn and the video site.
- To do: `spike_session_check.py --watch 240` on a later day (silent sign-in, session lifetime, Cloudflare on the video site).

**Phase 1: `ntulearn-mcp` core**
- `login_status`, `login`, `list_courses`, `upcoming_deadlines` (with status), `recent_announcements`, `recent_messages`.
- Done when: Claude Code answers "what's due this week?" correctly for all my courses.

**Phase 2: event store**
- SQLite schema, `uni-copilot sync`, change detection, `pilot-mcp` (`list_events`, `whats_new`), `weekly_actions` prompt.
- Done when: two syncs in a row report only real changes.

**Phase 3: Telegram**
- Channel plugin for Q&A, `uni-copilot notify`, `uni-copilot remind`, morning digest routine.
- Done when: I get a correct digest and reminders for a week without opening NTULearn.

**Phase 4: content and search**
- `list_course_content`, `read_page`, `search`, `read_file_text`, `download_file`, `get_assessment`, caching.

**Phase 5: recordings and study pipeline**
- `list_recordings`, `get_transcript`, lecture-notes skill (slides correct the transcript).

**Phase 6: Google Calendar**
- Calendar-sync skill with confirmation and dedupe; workload planner.

**Phase 7: more sources**
- Academic calendar, exam timetable, class timetable, forward-to-add, Outlook (if the connector works with NTU).

**Phase 8: stretch**
- Custom NTULearn watcher channel, discussions, groups, module progress, weekly review.

**Phase 9: share**
- README, install guide, policy check, packaging.

---

## 10. Testing

- Offline unit tests with **synthetic** fixtures (made-up courses, announcements, a small fake transcript JSON).
- Parsers tested on fixture HTML: Ultra document bodies, embedded-video links, locked items.
- Store tests: dedupe, edits, moved deadlines, removed items, reminder de-duplication.
- Opt-in live smoke tests (my account, GET only) that print counts and status codes only.
- MCP Inspector (`uv run mcp dev ...`) for manual tool checks.

---

## 11. Open questions

- Session: silent sign-in on a later day; how long copied sessions last; whether API calls keep the 3-hour idle session alive; Cloudflare on the video site after ~30 min (Phase 0 watch run).
- Not yet verified endpoints: Messages, Discussions, Groups, content availability windows, gradebook status for "submitted, not graded".
- Why IE2106 "Assignment 1" is missing from the calendar.
- Whether the grades endpoint ever shows unreleased grades.
- What the other `x-bb-blti-link` items are (other external tools).
- Whether other courses embed videos with transcripts.
- Telegram channel plugin: does it support inline buttons? Can its reply tool be used from a scheduled run? (Design assumes reply-to-confirm and a separate notify script.)
- Outlook connector availability with NTU's Microsoft tenant.
- Official sources and formats for the academic calendar and exam timetable.

---

## Appendix A: Verified NTULearn facts (23 Oct 2026, my account, GET only)

**Platform and login**
- NTULearn = Blackboard Learn Ultra at `https://ntulearn.ntu.edu.sg`. Login via Microsoft Entra ID (SAML). "Stay signed in" makes re-login silent in my everyday Chrome.
- Cookies: `BbRouter` (session cookie, gone when the browser closes), `JSESSIONID`, `AWSALB` (~7 days), `samlCookie`.
- Idle timeout ~3 h (`GET /learn/api/v1/utilities/timeUntilBbSessionInactive`). Reading it doesn't reset the timer. API calls 1–2 min after a reset didn't reset it either, but one after ~23 min idle did, so "last activity" seems to update only every so often.
- Login check: `GET /learn/api/v1/users/me` → 200 / 401.
- Playwright spike: first run needed manual sign-in (37 s); cookies copied into `requests` worked for NTULearn and the video site. Video site sets `__cf_bm` (Cloudflare, ~30 min).
- Always use the full base URL; relative fetches hit a CDN host and 404.

**Courses and terms**
- Memberships: `/learn/api/v1/users/{userId}/memberships?expand=course&limit=100`. Course has `displayId`, `displayName`, `id`, `isOrganization`, embedded `term`.
- Terms have names (`26S1`, `25S2`, …) but no dates. Current term = highest `YYS[1|2]` name (with override).
- `displayId` = `26S1-<CODE>-<SECTION>`. 16 sites this term = 8 subjects.

**Deadlines**
- `/learn/api/public/v1/calendars/items?since=&until=` matched NTULearn's Due dates view exactly (34/34). Max range 16 weeks.
- Calendar item `id` = gradebook column `id`; `end` = `grading.due`.
- Gradebook columns: `/learn/api/public/v2/courses/{id}/gradebook/columns` (`grading.due`). One item (IE2106 Assignment 1) had a due date but wasn't in the calendar → merge both sources.

**Content**
- Children: `/learn/api/public/v1/courses/{courseId}/contents/{contentId}/children`. Attachments and download under `.../contents/{contentId}/attachments[/{id}/download]`.
- Ultra "Document" page = `x-bb-folder` with one `x-bb-document` child titled `ultraDocumentBody`; its `body` is the page HTML.
- Locked items: availability `PartiallyVisible`, empty body.
- Survey of 16 sites: PDF 156, mp4 9, PPTX 7, docx 4, ipynb 4; 109 quiz links; large trees (cache).

**Recordings**
- Media Gallery: content handler `resource/x-bb-bltiplacement-NewUCVGalleryLTI_1_1`; `GET /learn/api/public/v1/lti/placements?handle=NewUCVGalleryLTI_1_1` → `_1327_1`.
- Launch (Playwright): `/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_1327_1&content_id=<id>&course_id=<id>&wrapped=true` → `https://ntulearnv1.ntu.edu.sg/channel/<name>/<id>`.
- List: links `/media/t/<entryId>/<channelId>`, 15 per page; next pages via `/channel/<name>/<id>/sort/recent/sortBy/recent/pageSize/15/fields/all/created/any/updated/any?format=ajax&page=N`.
- Transcripts: `GET /attachments/index/index/entryid/<entryId>/entryView/1?format=ajax` → download links on `api.sg.kaltura.com/.../attachment_attachmentasset/action/serve/attachmentAssetId/<id>?ks=<token>` (works without cookies; never store the token).
- All 51 gallery recordings (5 sites) have `.txt` + `.json`. Use JSON: `w`, `s`/`e` (ms), `a` (confidence). Auto-captions mishear terms ("breadth-first" → "breast research"); correct with slides.
- Module-embedded videos (IE2110): `ultraDocumentBody` has `<a data-bbtype="embedded-app" data-bbfile='{...}'>`, placement "Kaltura Media" (`_1326_1`), URL `.../browseandembed/index/media/entryid/<entryId>/...`. The 4 checked had no transcripts or captions.

**Grades (structure only)**
- `GET /learn/api/public/v2/courses/{courseId}/gradebook/users/{userId}`: `columnId`, `status`, `displayGrade`, `exempt`, `changeIndex`, `firstRelevantDate`, `lastRelevantDate`.

## Appendix B: Tech stack

- Python ≥ 3.10 with `uv`. Windows + PowerShell.
- MCP Python SDK `mcp>=2.3,<3`: `from mcp.server import MCPServer`; `@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))` (`from mcp.types import ToolAnnotations`); `mcp.run(transport="stdio")`; raise `ToolError` for errors the model should see.
- Playwright (Chromium), `requests`, SQLite (stdlib), PyMuPDF or pypdf, python-pptx, PyYAML.
- Claude Code: channels (Telegram plugin, research preview, needs Bun), Desktop local scheduled tasks, Remote Control, Google Calendar connector.
- Telegram Bot API (free) for push messages.
