# Phase 4 build log

What each finished step changed, how to test it, and its commit. Unverified points from each step are collected in the "Open items" section at the bottom, and new terms in the "Concepts" section. The short overview is `phase-4-core.md` in this folder. Phase 3 (Telegram, whose test week runs alongside the start of this phase) is in `docs/phase 3/`.

## How it all connects (read this first when you come back)

### The layers

```
NTULearn (GET only)
  └─ client/http.py: get_json / get_all (Phase 1, unchanged) + get_file (Phase 4: streamed, size cap, checked redirects)
       └─ client/contents.py: folders (with release conditions), pages, embedded files, file text, assessments
            ├─ parsers/content.py   content JSON → ContentItem (kind, locked, opens/closes)
            ├─ parsers/html.py      page HTML → Markdown (Phase 1)
            └─ parsers/files.py     PDF / PPTX / ipynb → text per page or slide
                 ▼
            cache.py: .data/cache/content.sqlite
              ├─ folder listings + page text (fresh 6 h), file text (until the file changes)
              ├─ FTS5 keyword index  ◄── announcements + messages copied read-only from the Phase 2 store
              └─ chunks + vectors    ◄── embeddings.py ──► Gemini API (course content + search words only)
                 ▲
            indexer.py (run by hand: walks every course, fills both indexes, paced, resumable)
                 ▼
            search.py: keyword list + meaning list → merged (reciprocal rank fusion); keyword-only with a reason if Gemini is unavailable
                 ▼
server.py tools ──► Claude Code (terminal; Telegram session: everything except download_file)
```

Restaurant version, continuing Phase 3's: so far the restaurant only kept the **order book** (deadlines) and the **notice board** (announcements). Phase 4 opens the **library** next door: the course's own books (slides, pages). The **cache** is the photocopies on the shelf, dated, so nobody fetches the same book twice in a day. **Search** is the librarian with two ways of finding things: the **card catalogue** (exact words, FTS5) and a **librarian who knows what each book is about** (embeddings: "shortest route" → the Dijkstra chapter). The librarian's helper is an outside service (Gemini) with a daily allowance; when it's used up, the card catalogue still works, and the librarian says so.

### Living next to the test week

The scheduled sync imports `client/http.py`, `client/courses.py`, `models.py` and `config.py`. Phase 4 only **adds** to those files, and after each such step runs `uv run pytest` and checks the next `.data\sync.log` line. Phase 3 pieces (`store/`, `sources/`, `pilot_mcp/`, `notify/`, `cli.py`, the schedules, the skills) are not touched, except one line in `start-telegram.ps1` (step 19) and, after 3 Nov, one new function in `notify/telegram.py` (step 21).

## Step 0: housekeeping

- **Decisions made (29 Oct):** all as recommended in `phase-4-core.md`, plus your choices:
  - **Embeddings (13): Google's Gemini first** (`gemini-embedding-001`, free tier, its own Google Cloud project and key), behind one interface so OpenRouter or a local model can replace it.
  - **Your rule for limits:** hitting Gemini's limit must give a proper answer, never a bare failure. Built into decision 13: the per-minute limit is waited out; the daily limit, a missing or rejected key, or no internet turn search into keyword-only with a plain reason (and when it resets), and make the indexer stop cleanly with its progress kept.
  - **Probe (11): approved.** It runs in step 1, GET only, counts and field names only.
- **What changed from the first draft of `phase-4-core.md`** (your questions, 29 Oct):
  - "Can you send me the week 10 slides on Telegram?" → decision 12, `send_course_file` (fetched into memory, sent only to your own chat after your "yes", never saved on the PC), built after the test week.
  - "Can I try RAG and embeddings?" → decision 2 became **hybrid search** (keyword + meaning), decision 13 the embedding model; 4 new steps (14–17) after keyword search works.
  - "Is Gemini Embedding enough? New project or share the key?" → Gemini as the first model; **its own project**, because Gemini's limits (100 requests/min, 30,000 tokens/min, 1,000 requests/day on the free tier) are shared by every key in a project.
  - The steps were renumbered 0–24: embeddings are steps 14–17, the Telegram send steps 21–22 (after 3 Nov), tests 23, final check 24.
- **`docs/phase 4/phase-4-core.md`:** decisions marked as made, with what wasn't chosen; steps, "done when" (7 items, including "Gemini unavailable → still a proper answer"), "Things to check" (Gemini items added), "New ideas you'll meet".
- **`docs/uni-copilot-project-plan.md`:**
  - Status line: Phase 4 started, in parallel with Phase 3's test week.
  - Non-goals: "no paid AI API" now reads "for answers"; search uses an outside embedding API (Gemini, free tier for now) for course content and search words only.
  - Section 4.1: the content tools as decided (`download_file` loses `dest_dir`; `upcoming_windows`, `send_course_file`; `get_assessment` takes a column id; hybrid `search` with its fallback).
  - Section 4.3: what `cache.py` holds, and the new Phase 4 files.
  - Section 7.1: repo layout (`phase 4/` docs, new modules), what stays local (`.data/cache/`, `GEMINI_API_KEY`, the downloads folder).
  - Section 8: the Gemini key and signed download URLs are secrets; what may leave the PC for embeddings; files from NTULearn are untrusted.
  - Roadmap (Phase 4 started, its done test), open questions (what the probe checks, Gemini's limits), Appendix B (pypdf, python-pptx, numpy, Gemini).
- **This log:** created, in the same format as the Phase 3 log.
- **`.gitignore`:** nothing to add. `git check-ignore -v` shows `.data/cache/content.sqlite` and `.data/index.lock` ignored by the `.data/` line, and `.env` by its own line, so the cache, the search index and the Gemini key can never be committed.
- **FTS5:** checked before planning: SQLite 3.53.1 in this project's Python creates an FTS5 table with stemming (`porter unicode61`), and "search" matched "Breadth-first search slides".
- **Not changed:** no code, no schedules, `docs/how-it-runs.md` (nothing new runs on a schedule in Phase 4).
- **Noticed:** `docs/phase 3/phase-3-build-log.md` had an uncommitted change not made in this session (the 29 Oct entry now includes the 11:29 digest retry), probably from your test-week session. Left as it is.

**Your part (any time before step 14, no rush):**

1. In Google AI Studio → **API keys** → **Create API key** → **Create API key in new project** (name it e.g. `uni-copilot`). A new project gets its own limits, separate from the project your screenshot showed.
2. Put the key in `.env` yourself (next to the Telegram lines; never paste it into Claude):

   ```
   GEMINI_API_KEY=<your key>
   ```

3. Optional: if you'd rather Google didn't use your slides' text, turn on billing for that project (about $0.30 for everything, by the estimate).

Check (PowerShell):

```
git check-ignore -v .data/cache/content.sqlite .env
git status
```

Expect the first to print the `.data/` line and the `.env` line from `.gitignore`. The second shows the changed plan doc and the new `docs/phase 4/` folder (and the Phase 3 log change from your test-week session).

Commit (when you choose; `git add` only what's yours to commit):

```
git add "docs/phase 4" docs/uni-copilot-project-plan.md
git commit -m "docs: plan phase 4 content and search"
```

**Done (29 Oct).**

## Step 1: probe_content.py (the live probe)

**New file `src/uni_copilot/ntulearn_mcp/probe_content.py`:** a one-off, re-runnable script that asks NTULearn read-only questions about course content and prints **what the answers look like**: counts, status codes, field names, item types (content handlers such as `resource/x-bb-folder`) and mime types. Never titles, bodies, file names, dates, ids or URLs. Nothing is saved.

What it checks:

1. **The content tree** per site: `GET .../courses/{id}/contents` (the top level), then the same with `recursive=true`. If the recursive answer has more items than the top level, one request per course is enough for the whole tree (big for the indexer). If not, it walks one level down into up to 3 folders instead (`.../contents/{id}/children`). Also: how many pages were left over (`paging.nextPage`), and every field name with how many items had it.
2. **Ultra pages:** how many `x-bb-document` items are titled `ultraDocumentBody` and have a body, and how many folders have exactly that one child. Titles are compared inside the script only.
3. **Availability and windows:** counts of `availability.available` (Yes / No / PartiallyVisible), how many items have `availability.adaptiveRelease` start/end dates, on which item types, how many are still in the future, and how many locked items have an empty body or a window.
4. **Attachments** of up to 2 file items per site: status, field names (does it give a size? a modified time?), mime types.
5. **Discussions and Groups:** `public v1 .../discussions` and `public v2 .../groups` on every site, plus `public v1 .../groups` and `internal v1 .../discussions` on the first site only. Status and item counts.
6. **One PDF download, headers only:** `.../attachments/{id}/download` sent by hand without following redirects (`allow_redirects=False`, `stream=True`, closed after the headers, so the file isn't read). For each redirect: the target **host name** only, whether the link carries a query string (a sign of a signed, temporary link), and whether that host answers without NTULearn's cookies. At the end: status, `Content-Type`, whether `Content-Length` is there.
7. **Up to 3 quizzes/assignments:** their `contentHandler` field names, how many content items point at a gradebook column (`contentHandler.gradeColumnId`, the link to deadlines), and the attempt / time / availability field names of that column and of the internal content item.
8. **"Viewed" check:** the internal content item's tracking-looking field names (view, review, progress, read, access, completion) before and after the download, and which of them changed. Values are compared, never printed.

How it's built:

- **`probe_get()`** goes through `NtuLearnHttp.get_json` (so sign-in, 1 request/s, retries and GET-only all apply) but returns the status instead of raising, because an error status (403, 404) is an answer we want to record.
- **`probe_list()`** follows `paging.nextPage` up to 10 pages and retries once without `limit` if NTULearn rejects it.
- **`key_paths()`** turns a JSON object into dotted field names (`availability.adaptiveRelease.start`) without values, which is what makes the output safe to print.
- **`raw_get_headers()`** is the one place that doesn't use `get_json`: a download is a redirect to another host, and `get_json` would follow it and decide "another host = signed out" (the reason for step 4's `get_file`). It borrows the client's pacer and session, uses a fresh session without cookies for other hosts, and never reads the body.
- Logging is set to warnings only, because the client's info lines name request paths with ids.
- `--sites N` probes only the first N sites; `--no-download` skips checks 6 and 8.

Checks so far (offline): the helpers on made-up items (field paths, window and Ultra-page counts) and `--help`; `uv run pytest` → 186 passed, 4 skipped (nothing imports the probe).

Check (PowerShell, live, GET only; about 3 minutes):

```
uv run --no-sync python -m uni_copilot.ntulearn_mcp.probe_content --sites 1
uv run --no-sync python -m uni_copilot.ntulearn_mcp.probe_content
```

The first is a short trial on one site (about 10 requests). The second probes every site and ends with `Done: N requests in N s. Nothing was saved.` Progress lines (`site 3/16 (IE2108) ...`) go to the screen only. Paste the output into the chat: it has no content, only counts and names. The results then go into the plan's Appendix A.

**First run (29 Oct): 108 requests in 106 s**, all 200 except where noted, nothing saved. Full facts in the plan's Appendix A ("Content, Phase 4 probe"). What they mean for Phase 4:

- **Good news:** `recursive=true` lists a whole course in one request (966 items on 16 sites), so the indexer only needs ~20 requests for every tree. Every quiz link (119) carries its gradebook column id, so a deadline leads straight to its quiz. A download changed none of the item's "reviewed" fields.
- **Downloads:** NTULearn → NTULearn (signed link) → `alt-<id>.blackboard.com` (signed link, no cookies needed) → the PDF, with no `Content-Length`. So `get_file` (step 4) follows up to 5 redirects to two allowed hosts and counts bytes as they arrive. 4 requests per file: a first full index of ~220 files ≈ 900 requests ≈ 30 minutes at the indexer's pace.
- **No opens/closes dates:** none of the 966 items has an availability date; only 4 Turnitin items have start/due dates in their settings. So `upcoming_windows` is proposed to be dropped (see "Changed by the probe" in `phase-4-core.md`).
- **Attachments** have only `id`, `fileName`, `mimeType`, so a changed file is noticed by the content item's `modified` time.
- **Discussions:** none. **Groups:** 193, with sign-up sheets (Phase 8).
- **Still unclear:** what the 221 `PartiallyVisible` items are (none have dates, 151 have no body), which item kind answered 400 on 4 attachment lists, and whether slides also sit inside page bodies as embedded files.

**Follow-up (`--follow-up`):** added after the first run, about 40 requests:

- **A:** the whole trees again.
- **B:** `PartiallyVisible` items by kind, how many have an empty body, and how many items sit inside a `PartiallyVisible` folder.
- **C:** files embedded in page bodies (`data-bbfile` markup, decoded with `html.unescape` and `json`): count, `data-bbtype` values, mime types.
- **D:** attachment-list status for 4 kinds: files, `PartiallyVisible` files, `ultraDocumentBody` documents, other documents.
- **E:** whether `PartiallyVisible` folders open.
- **F:** release-rule field names on the internal item, one per `PartiallyVisible` kind (ignoring `permissions.*`, which only says what you may do).

Checked offline with made-up items: embedded files counted, both quoting styles of `data-bbfile` read, the `PartiallyVisible` parent counted. `uv run pytest` → 186 passed, 4 skipped.

```
uv run --no-sync python -m uni_copilot.ntulearn_mcp.probe_content --follow-up
```

**Follow-up run (29 Oct): 43 requests in 41 s.** Full facts in Appendix A ("Follow-up").

- **`PartiallyVisible` isn't locked:** it means "visible with conditions". Those folders opened and those files listed their attachments. Mostly folders (80, many of them Ultra pages), page bodies (79) and quizzes (54).
- **Pages have no attachment list** (400 on all 6 documents tried), which explains the first run's 400s. Their files are **embedded in the body: 283 files on 164 pages, 126 of them PDFs.** About half the slides live there rather than as file items, so the reading and download tools must handle both.
- **Release rules exist** on the internal item (`adaptiveReleaseRules.rules[].criteria[]` with `start`, `end`, `type`, `negate`), one request per item. The public `adaptiveRelease` field was empty because it only covers simple date availability, not these rules (likely; not verified).

**Second follow-up (`--follow-up-2`), the last probe run:**

- **A:** the whole trees again (16 requests).
- **G1:** two tries at a list request that includes the rules: internal `.../contents/ROOT/children`, with and without `expand=adaptiveReleaseRules`.
- **G2:** the internal item of every `PartiallyVisible` item except page bodies (capped at 150), plus one visible item of each kind for comparison. Counted: rule and criterion types (enum words like `DATE`), date criteria with a start or end, whether each is not open yet, open now or closed, negated criteria, `visibility` values and reason codes. Never rule titles (written by profs) or dates.
- **H:** embedded files: the tag and `data-bbtype` around each one, the JSON field names, file extensions (`.pdf`, never the name), and the shape of `resourceUrl` (host, first path segment, query or not; never the URL).
- **H2:** one embedded PDF's download, headers only, with the same redirect check as the first run. That code moved into a shared `follow_download()`.

Checked offline: a made-up item with two date criteria and a negated membership criterion counts as 2 date criteria (1 not open yet, 1 closed); two made-up embedded files (an `<a>` with HTML-escaped JSON, an `<img>` with plain JSON) give their tags, extensions and URL shapes. `uv run pytest` → 186 passed, 4 skipped.

```
uv run --no-sync python -m uni_copilot.ntulearn_mcp.probe_content --follow-up-2
```

**Second follow-up (29 Oct): 171 requests in 169 s.** Full facts in Appendix A ("Second follow-up").

- **Release rules:** all 142 conditional items have one; 134 are "after you finish another item", 8 are date ranges (none still to come). NTULearn's `visibility` says 124 are already open to you, 18 are really held back. The internal folder list already carries the rules (no `expand` needed). So `upcoming_windows` is proposed to be dropped, and items say open / conditional, open to you / held back / opens-closes <date> instead ("Changed by the probe" in `phase-4-core.md`).
- **Embedded files:** only 39 of 283 have a `resourceUrl`; those download like attachments (`/bbcswebdav/...` → the Blackboard file server, no cookies needed there → the file, no size header). 158 have only a `viewerUrl`. 31 are Jupyter notebooks (`.ipynb`, the "octet-stream" ones). 7 of 283 give a size.

**Third follow-up (`--follow-up-3`), about 20 requests:** the trees again, then per kind of embedded tag which link fields come together (`resourceUrl`, `viewerUrl`, `url`, `fileSize`, `mimeType`), the **masked shape** of every `viewerUrl` and `resourceUrl`, and one viewer-only PDF followed by hand, headers only. `masked_shape()` keeps the host and the path's structure but turns digits into `N`, a part with a dot (a file name) into `<name>.ext`, very long parts into `<long>`, and keeps only the names of query parameters, so no id, file name or signed value is printed. Checked offline on three made-up links.

```
uv run --no-sync python -m uni_copilot.ntulearn_mcp.probe_content --follow-up-3
```

**Third follow-up (29 Oct): 22 requests in 20 s.** Full facts in Appendix A ("Third follow-up").

- **Both link kinds are the same signed file link:** `/bbcswebdav/<long>/xid-N_N?<3 signature parameters>`, the viewer one with display settings added. A viewer-only PDF downloaded like the others (→ `alt-<id>.blackboard.com`, no cookies → the PDF). So every embedded file with a link is reached in 3 requests, and its link comes with the page body that the tree listing already returns.
- **The signature parameters are secrets** (like Kaltura's `ks`): read fresh, never cached, logged or shown. Phase 1's `parse_html` already writes every `<a data-bbfile>` as `[file: name]` without its link (checked in the code); `parsers/content.py` (step 6) will also hide any hand-pasted `/bbcswebdav/` link with a query string.
- **`xid-N_N`** (the file's id in NTULearn's file store) is the embedded file's id for the tools.
- 38 embedded files have no link in their JSON (maybe in the `<a href>`); step 9 counts them live. 73 embedded apps and 6 videos are Phase 5.

**Step 1 is done:** four runs, 344 GETs in total, nothing saved, no content printed. What changed in the plan because of it is in "Changed by the probe" in `phase-4-core.md`: no `upcoming_windows` (you agreed 29 Oct: "do as you recommend"; decision 9 rewritten, done-when 5 changed, the plan's sections 4.1, 4.2 and 9 updated), conditions shown per item instead, embedded files handled by every file tool, `get_file`'s redirect rules, `modified` as the change signal, and the indexer's first run ~40 minutes.

**Decided before step 2 (29 Oct): this session can stay open for installing.** A plain `uv add` reinstalls the project too, which rewrites `.venv\Scripts\ntulearn-mcp.exe`, and Windows won't let a running program's file be replaced (this session runs that server). So step 2 uses `uv add --no-sync` (only `pyproject.toml` and `uv.lock`) and then `uv sync --inexact --no-install-project` (only the new packages; nothing removed). Both flags are in uv 0.12.11 (`--help` checked).

**Done (29 Oct).**

Commit (steps 0 and 1 together):

```
git add src/uni_copilot/ntulearn_mcp/probe_content.py "docs/phase 4" docs/uni-copilot-project-plan.md
git commit -m "feat(ntulearn): add live probe for course content endpoints"
```

## Step 2: pyproject.toml + uv.lock (the new packages)

**What was installed:**

- **`pypdf` 6.21.4:** reads the text out of PDFs (step 7). Pure Python, BSD licence.
- **`python-pptx` 1.0.2:** reads slide text, tables and speaker notes out of PPTX files (step 7). MIT licence.
- **`numpy` 2.5.4:** fast maths on long lists of numbers: comparing embedding vectors in hybrid search (step 16).
- **Pulled in by python-pptx:** `lxml` (reads the XML a PPTX is made of), `pillow` (images), `xlsxwriter` (charts inside slides). We don't use them directly.

**`pyproject.toml`:** three new lines under `dependencies` (`numpy>=2.5.4`, `pypdf>=6.21.4`, `python-pptx>=1.0.2`). `>=` means "this version or newer"; `uv.lock` pins the exact versions (and those of every package they need), so every `uv sync` installs the same ones.

**How it was installed, and why in two parts** (decided before this step): a plain `uv add` also reinstalls this project, which rewrites `.venv\Scripts\ntulearn-mcp.exe`, and Windows refuses while a Claude Code session runs that server. So:

1. `uv add --no-sync pypdf python-pptx numpy`: only edits `pyproject.toml` and `uv.lock` ("Resolved 58 packages").
2. `uv sync --inexact --no-install-project`: installs what the lock file lists but `.venv` doesn't have yet (the 6 packages above). `--no-install-project` leaves this project's own install and its programs alone; `--inexact` removes nothing that isn't in the lock file, so nothing the scheduled jobs use can disappear.

Run at 22:55, before the 23:00 sync. uv warned "Failed to hardlink files; falling back to full copy": uv keeps downloaded packages in a cache on drive C: and links them into `.venv`, which doesn't work across drives (the project is on D:), so it copied them. Harmless; only a little more disk space.

Checks (29 Oct):

- `import pypdf, pptx, numpy` works and prints the versions above.
- `.venv\Scripts\python.exe -m uni_copilot.cli status` (the Python the scheduled jobs use) works.
- `ntulearn-mcp.exe`, `pilot-mcp.exe`, `uni-copilot.exe` still dated 25 Oct (not rewritten); pilot's self-test lists its 8 tools.
- `uv lock --check`: the lock file matches `pyproject.toml`.
- `uv run pytest` → 186 passed, 4 skipped.

Check (PowerShell):

```
uv run --no-sync python -c "import pypdf, pptx, numpy; print(pypdf.__version__, pptx.__version__, numpy.__version__)"
uv run --no-sync pytest
Get-Content .data\sync.log -Tail 6
```

Expect `6.21.4 1.0.2 2.5.4`, "186 passed, 4 skipped", and the 23:00 sync finishing normally in the log (this step changed `.venv`, which the scheduled jobs run from). Then restart the Telegram session (`scripts\start-telegram.ps1`).

Later, when no Claude Code session is open: a plain `uv sync` once, so this project's own install records the new packages too. ~~Nothing depends on it.~~ **Correction (29 Oct, found after step 5):** something does. Until that `uv sync`, every `uv run` tries to reinstall the project first, which rewrites `ntulearn-mcp.exe` and fails while any Claude Code session runs that server (`failed to remove file ... ntulearn-mcp.exe ... os error 32`; seen on `uv run pytest`). Claude Code starts its servers with `uv run` too (`.mcp.json`), so meanwhile a **new** session (a restarted Telegram session, the digest's headless `claude -p`) would start without its ntulearn and pilot servers. The scheduled sync and remind were never affected (they run `.venv\Scripts\pythonw.exe` directly). Fix: close every Claude Code session, `uv sync` once in a plain PowerShell, `uv run pytest`, then reopen (`claude --continue`) and restart the Telegram session. Lesson for later dependency changes: do the plain `uv sync` in the same sitting, with all sessions closed, not "some day".

Commit:

```
git add pyproject.toml uv.lock docs
git commit -m "build(deps): add pypdf, python-pptx and numpy for phase 4"
```

## Step 3: config.py (Phase 4 settings)

**`src/uni_copilot/ntulearn_mcp/config.py`**, a new section "Phase 4: course content, files and search". Constants (fixed values, changed by editing the file):

- **Paths:** `CONTENT_DB` = `.data/cache/content.sqlite` (content cache + search index; safe to delete), `INDEX_LOCK` = `.data/index.lock` (one indexer at a time).
- **Freshness:** `CONTENT_FRESH_S` = 6 hours for folder listings and page text. File text has no age limit: it's re-read when the item's `modified` time changes (the probe found no size or modified time on attachments).
- **Size limits**, counted while downloading because downloads never say their size up front: `READ_MAX_BYTES` 50 MB (reading and indexing), `DOWNLOAD_MAX_BYTES` 200 MB, `SEND_MAX_BYTES` 50 MB (Telegram's bot limit). `READ_MAX_PAGES` 20 and `READ_MAX_CHARS` 30,000 per `read_file_text` call.
- **Download redirects:** `MAX_DOWNLOAD_REDIRECTS` 5; `FILE_HOST_SUFFIXES` = `.blackboard.com` (the probe's `alt-<id>.blackboard.com`; NTULearn itself is always allowed).
- **Indexer pace:** `INDEXER_INTERVAL_S` 2.0 (half the tools' pace).
- **Embeddings:** `GEMINI_API_KEY_NAME` (only the *name* of the `.env` line), `GEMINI_BASE_URL`, `EMBED_MODEL` `gemini-embedding-001`, `EMBED_DIMENSIONS` 768, `EMBED_MAX_REQUESTS_PER_MIN` 80 and `EMBED_MAX_TOKENS_PER_MIN` 24,000 (80% of the free tier's 100 and 30,000), `EMBED_BATCH_SIZE` 50, `CHUNK_WORDS` 300 with `CHUNK_OVERLAP_WORDS` 50.

**Settings from the environment or `.env`**, two new `Settings` fields. Like the Telegram settings, a real environment variable wins, then a line in the project's `.env` (added after your question: at first they were read from the environment only, so a `.env` line would have done nothing). `config.py` has its own small `.env` reader, `_env_file_values()`, with the same rules as `notify/telegram.py`'s `read_env_file` (that one can't be imported here: it imports `config.py`, so importing it back would be circular). A missing or unreadable `.env` counts as empty, so it can never stop the sync. `NTULEARN_TERM` and `NTULEARN_ENABLE_GRADES` stay environment-only, as before.

`.env` lines need no quotes, even for a path with spaces (everything after `=` is the value; one pair of quotes is removed if you use them). All three are optional until step 14:

```
# Phase 4 (optional)
UNI_COPILOT_DOWNLOAD_DIR=D:\Uni\Course Files
UNI_COPILOT_EMBEDDINGS=gemini
GEMINI_API_KEY=<your key, from step 0's "your part">
```

The two fields:

- `embeddings`: from `UNI_COPILOT_EMBEDDINGS`; `gemini` (default) or `off` (keyword-only search). Deliberately **not** checked in `load_settings()`: the scheduled sync and both servers call it at start, and a typo in a Phase 4 setting must never stop them. Search (step 16) will treat an unknown value as keyword-only and say why.
- `download_dir`: from `UNI_COPILOT_DOWNLOAD_DIR` (`~` allowed), default `<your home>\Downloads\uni-copilot`, built by the new `_download_dir()`.

Both fields have **defaults**, so every existing `Settings(term_override=..., enable_grades=...)` (in `tests/conftest.py` and older code) keeps working unchanged. Nothing else in the file changed.

**New idea: a dataclass field with a default.** `download_dir: Path = field(default_factory=_download_dir)` means "if nobody passes `download_dir`, call `_download_dir()` to make one". `default_factory` (a function) instead of a plain default because the value depends on the environment at the moment `Settings` is built, not when the file is loaded.

Checks (29 Oct, offline): the paths sit inside `.data/`; old-style `Settings(...)` still builds (with `gemini` and the default folder); a custom `UNI_COPILOT_DOWNLOAD_DIR=~/...` is expanded; `UNI_COPILOT_EMBEDDINGS=GEMNI` (a typo) loads without an error. `.env` reading, with made-up files only: a path with spaces and no quotes, a quoted value, a byte-order mark, a missing file (empty), and a real environment variable winning over `.env`; the real `.env` has none of the three lines yet (checked by name only, no values printed). `uni-copilot status` with the scheduled jobs' Python works; `uv run pytest` → 186 passed, 4 skipped.

**Downloads and git:** the default folder is outside the repo, so git never sees it; nothing to add to `.gitignore`. If you ever point `UNI_COPILOT_DOWNLOAD_DIR` inside the project, use `.data\downloads` (already ignored by the `.data/` line) rather than a new folder.

Check (PowerShell):

```
uv run --no-sync python -c "from uni_copilot.ntulearn_mcp.config import load_settings, CONTENT_DB; s = load_settings(); print(CONTENT_DB.name, s.embeddings, s.download_dir)"
uv run --no-sync pytest
```

Expect `content.sqlite gemini C:\Users\<you>\Downloads\uni-copilot` and "186 passed, 4 skipped". The folder isn't created until the first download (step 13).

Commit:

```
git add src/uni_copilot/ntulearn_mcp/config.py docs
git commit -m "feat(ntulearn): add phase 4 settings for content, files and search"
```

## Step 4: client/http.py (`get_file`, the file download)

**Why a new method.** `get_json` can't fetch a course file: a file isn't JSON, and the download leaves NTULearn. The probe showed the chain: NTULearn → NTULearn with a signed link → `alt-<id>.blackboard.com` (signed, no cookies needed) → the file, with no size header. `get_json` lets `requests` follow redirects and then decides "we ended up on another host = signed out", which is right for API calls and wrong here. So `get_file` follows the chain itself, one hop at a time.

**What was added** (everything before it in the file is unchanged; the sync doesn't use any of it):

- **`get_file(path_or_url, max_bytes, sink) -> FileInfo`**, the public method:
  - `path_or_url`: an API path (a file item's `.../attachments/{id}/download`) or a full NTULearn link (an embedded file's signed `/bbcswebdav/` link, taken from a fresh page body).
  - `sink`: where the bytes go, anything with `.write()`. `io.BytesIO()` keeps the file in memory (for reading its text); an open file saves it (for `download_file`). So the same method serves reading and saving, and a 200 MB download never has to sit in memory.
  - Signs in first if needed; if NTULearn answers "signed out" (401, or a redirect to a host we don't allow, e.g. Microsoft's sign-in page), signs in silently once and starts again. Nothing is written to the sink before the final answer, so a restart begins clean.
  - Returns `FileInfo(size, content_type, hosts)`: safe facts only (host *names*, never links).
- **`_download()`**: the loop over hops, at most `MAX_DOWNLOAD_REDIRECTS` (5). NTULearn hops use the signed-in session (with cookies); other allowed hosts get a fresh session made by `file_session_factory` (no cookies, closed afterwards). A redirect to anything else is refused.
- **`_send_file_hop()`**: one hop: `allow_redirects=False`, `stream=True` (headers first, body later), `Accept: */*`. Paced and limited like every request (1 per second, 2 at once); retries 429, 5xx and network errors with the same waits as `get_json`. Logs the hop number, host and status only.
- **`_read_body()`**: reads the body in 64 KB chunks into the sink and stops with `FileTooLarge` as soon as the count passes `max_bytes` (or at once if the server announces a bigger size). Always closes the connection.
- **`FileTooLarge`**: a `ValueError` with a plain message ("The file is larger than 50 MB, the limit for this; it wasn't fetched."), so the tools pass it to Claude as it is. If it happens, the sink holds a partial file that the caller throws away.
- **`_allowed_file_host()`**: https, and NTULearn itself or a host ending in `FILE_HOST_SUFFIXES` (`.blackboard.com`).
- **`_file_start()`**: an API path gets the base URL and is its own label in logs and errors (ids only, no secrets). A full link must be on NTULearn, and its label is always "an embedded file", because the link holds signed values.
- **`NtuLearnHttp(..., file_session_factory=requests.Session)`**: a new optional parameter, so tests can hand in a fake file server. Existing `NtuLearnHttp()` and `NtuLearnHttp(signer=...)` calls are unaffected.
- **`--file` self-test**: finds one PDF attachment of a file item and one PDF embedded in a page on your courses, downloads both into memory (thrown away), prints size, type, the hosts passed and "starts like a PDF", then checks two refusals (a link off NTULearn; a 1000-byte limit).

**How it connects:**

```
read_file_text / indexer (steps 9, 12)      download_file (step 13)        send_course_file (step 22)
        │ sink = io.BytesIO()                    │ sink = a temporary file       │ sink = io.BytesIO()
        └──────────────────────► NtuLearnHttp.get_file(path_or_url, max_bytes, sink)
                                   ├─ _file_start: API path or an NTULearn link only
                                   ├─ _download: hop by hop (≤ 5 redirects)
                                   │    ├─ ntulearn.ntu.edu.sg ── signed-in session (cookies)
                                   │    ├─ *.blackboard.com ───── fresh session (no cookies)
                                   │    └─ anything else ──────── refused (or "signed out" → one silent sign-in)
                                   └─ _read_body: 64 KB chunks, FileTooLarge past the limit
```

**New ideas:**

- **Streaming:** with `stream=True`, `requests` returns as soon as the headers arrive; the body is read piece by piece with `iter_content()`. That's how a size limit can stop a download half-way instead of after it has filled memory.
- **Redirect (301/302/303/307/308):** "the thing you asked for is over there" plus a `Location` header. Following them by hand (`allow_redirects=False`) lets us check every host before going there.
- **Signed link:** a link with a temporary signature in its query string; whoever has it can fetch the file for a while. Treated like a password: never logged, never in an error message, never returned.
- **File-like object (a "sink"):** anything with `.write()`. `io.BytesIO` is a file that lives in memory.

Checks (29 Oct, offline, fake sessions, no network): the verified 4-hop chain (2 hops with the cookie session, 2 with the cookie-less one, PDF bytes complete); a CDN redirect to an unknown host refused, with no link in the error; a redirect to Microsoft's sign-in page → one hidden sign-in → success; the size limit counted while reading and an announced size refused before reading; more than 5 redirects refused; an embedded link's 404 reported as "NTULearn 404 on an embedded file" (no link); a start off NTULearn refused before any request; 503 then 200 retried. `uv run pytest` → 186 passed, 4 skipped; `uni-copilot status` with the scheduled jobs' Python works. (These become real tests in step 23.)

Check (PowerShell, live, GET only; about 40 requests, under a minute):

```
uv run --no-sync python -m uni_copilot.ntulearn_mcp.client.http --file
```

Expect two lines like `file item's attachment: 1.23 MB, application/pdf, via ['ntulearn.ntu.edu.sg', 'ntulearn.ntu.edu.sg', 'alt-….blackboard.com', 'alt-….blackboard.com'], starts like a PDF: True` and `embedded file: …, via ['ntulearn.ntu.edu.sg', 'alt-….blackboard.com', 'alt-….blackboard.com'], starts like a PDF: True`, then `a link off NTULearn -> A file download must start on NTULearn …` and `limit 1000 bytes -> The file is larger than 0.001 MB …`. Nothing is saved.

**Live check (29 Oct): passed.** A file item's attachment: 0.91 MB, `application/pdf`, 4 hops (NTULearn, NTULearn, `alt-<id>.blackboard.com` × 2), real PDF bytes, 4.8 s. An embedded file: 0.03 MB, `application/pdf`, 3 hops (its signed link starts at the second NTULearn hop), real PDF bytes, 2.3 s. Both refusals worked: a link off NTULearn refused before any request, and the 1000-byte limit stopped the download. (The last two lines are refusals on purpose, not failures.) So a file costs about 1 second per hop: ~5 s for a file item, ~2–3 s for an embedded file.

**Done (29 Oct).**

Commit:

```
git add src/uni_copilot/ntulearn_mcp/client/http.py docs
git commit -m "feat(ntulearn): add get_file for course file downloads"
```

## Step 5: models.py (what the new tools return)

**`src/uni_copilot/ntulearn_mcp/models.py`**, a new section "Phase 4: course content, files, search" after the Phase 1 models (which are unchanged; the sync uses some of them). Like Phase 1's models, these are the **output schema** Claude sees for each tool: field names, types and descriptions, with `UNTRUSTED` on everything written by other people.

Building blocks:

- **`ContentKind`:** what an item is: `folder`, `page` (an Ultra Document), `file`, `assessment` (Ultra makes tests and assignments the same kind of link), `survey`, `module`, `link`, `external_tool`, `video_gallery`, `other`. Step 6 maps NTULearn's content handlers onto these.
- **`Release`** (replaces the old "locked" and opens/closes idea, after the probe): `state` is `open` (no conditions), `conditional` (has conditions, not checked whether you meet them: the cached tree only says "PartiallyVisible"), `open_to_you` (conditions met) or `held_back` (not met yet); `rule` = the rule's title written by the prof (UNTRUSTED), and `opens_at` / `closes_at` only for the rare date rules, each with an SGT text field.
- **`EmbeddedFile`:** a file inside a page: `xid` (its id in NTULearn's file store, which the tools take), `name` (UNTRUSTED), `mime_type`, `has_link` (False for the ~38 the probe found without a link). **No link field**: signed links never leave the code that downloads.
- **`ContentItem`:** one item: id, course, parent, title, kind, description, `release`, has_children, file name and type (file items), `column_id` (assessments: the same id deadlines carry), `embedded_files` (pages), `modified_at`, and `url` (the course page; a direct link to the item isn't verified).
- **`_CachedResult`:** the base for content answers: `cached_at` (when the copy was fetched from NTULearn), `from_cache`, and `cached_sgt`, so every answer can say "cached at … SGT", like pilot's "as of".

Tool answers:

- **`ContentList`** (`list_course_content`): a folder's items.
- **`PageText`** (`read_page`): the page as light Markdown, its links (never file links) and its embedded files.
- **`FileText`** (`read_file_text`): `pages` (each a `FilePage`: number + text), `total_pages`, `cut_short` and `next_pages` ("21-40") when there's more than one call returns, `empty_pages` (pictures or scans; no OCR). Its text carries a stronger label: course files may contain instructions aimed at an AI, to be treated as content.
- **`Assessment`** (`get_assessment`): instructions, due date, attempts allowed, password / secure browser / webcam required, late attempts allowed, your status, release. Never a score or points.
- **`SearchResults`** (`search`): `hits` (each a `SearchHit`: source page / file / announcement / message / title, a snippet, where to look next: `content_id` + `file_ref` + `page`, or `event_id` for pilot, and `found_by` keyword and/or meaning), **`search_mode`** (`hybrid` or `keyword_only`) with **`mode_reason`** in plain words (your rule: the Gemini limit gives a proper answer), and **`coverage`** (`SearchCoverage`: how many courses, pages, files and posts are indexed, how many text pieces have embeddings, when the indexer last ran), so a miss is never mistaken for "it's not there".
- **`DownloadResult`** (`download_file`): the saved file's cleaned name, full path, size and type.

**New ideas:**

- **Nested model:** `Release` sits inside `ContentItem` and `Assessment`, so "is it open?" has one shape everywhere.
- **Computed field:** `opens_sgt`, `cached_sgt`, `due_sgt` aren't stored; pydantic works them out from the UTC time each time the model becomes JSON (the Phase 1 pattern).
- **`Literal[...]`:** a field that may only hold one of the listed words; anything else is rejected when the model is built.

Checks (29 Oct, offline): `python -m uni_copilot.ntulearn_mcp.models` now also prints a made-up folder listing (a held-back page with its embedded slides, "cached at Thu 29 Oct 22:00 SGT"), a date rule in SGT, and a keyword-only search with its reason; and checks all 12 new models' 72 field names: **no grade-like fields, no file-link fields**. `uv run pytest` → 186 passed, 4 skipped; `uni-copilot status` (the scheduled jobs' Python) and pilot's self-test work.

Check (PowerShell):

```
uv run --no-sync python -m uni_copilot.ntulearn_mcp.models
```

Expect the Phase 1 lines 1–4 as before, then 5–8, ending with `12 new models, 72 field names. Grade-like fields: none; file-link fields: none`.

Commit:

```
git add src/uni_copilot/ntulearn_mcp/models.py docs
git commit -m "feat(ntulearn): add result models for content, files and search"
```

## Open items

Everything not yet verified in Phase 4. Updated as steps finish.

- **After step 2, urgent (29 Oct):** a plain `uv sync` with every Claude Code session closed, before the next session or digest starts (see the correction in step 2). (Verified, kept for the record: the first scheduled sync on the changed `.venv` and on step 3's `config.py`, started 22:59 on 29 Oct, finished normally, exit code 0, 2 new announcements; remind's catch-up at 22:59 too.)
- **38 embedded files without a link in their JSON** (probe): step 9 counts them live.

- **All of "Things to check" in `phase-4-core.md`:** the probe (step 1) answers the NTULearn ones; the Gemini ones are checked in steps 14 and 17.
- **Gemini's free-tier data terms:** re-read when building step 14; billing is the switch if they're not acceptable.
- **Telegram tool list:** new `ntulearn` tools appear in the locked Telegram session by themselves (the lock limits only built-in tools). Until steps 18–19, a new tool there would wait for a click at the PC.

## Concepts

- **Probe:** a one-off script that asks a few read-only questions to learn the shape of NTULearn's answers before building on them. Prints counts, status codes and field names, never content.
- **RAG (retrieval-augmented generation):** find the right pieces of text first (retrieval: `search`), then let the AI answer from only those pieces (generation: Claude, on your Claude plan).
- **Embedding / vector:** a long list of numbers that stands for a text's meaning; texts that mean similar things get similar numbers.
- **Hybrid search:** keyword search and meaning search run together and their result lists are merged.
- **Rate limit:** how much a service lets you use per minute or per day; per project for Gemini, so keys in the same project share it.
- **Dependency / lock file:** a package the project needs, listed in `pyproject.toml` with a minimum version; `uv.lock` writes down the exact version of it and of everything it needs in turn, so installs are the same every time.
- **Transitive dependency:** a package pulled in because another package needs it (lxml, pillow, xlsxwriter for python-pptx).
- **Hardlink:** a second name for the same file on disk; impossible across drives, so uv copies instead.
- **Graceful degradation:** when one part is unavailable, fall back to something that still works (keyword-only search) and say so, instead of failing.
