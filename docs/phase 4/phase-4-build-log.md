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

## Open items

Everything not yet verified in Phase 4. Updated as steps finish.

- **All of "Things to check" in `phase-4-core.md`:** the probe (step 1) answers the NTULearn ones; the Gemini ones are checked in steps 14 and 17.
- **Gemini's free-tier data terms:** re-read when building step 14; billing is the switch if they're not acceptable.
- **Telegram tool list:** new `ntulearn` tools appear in the locked Telegram session by themselves (the lock limits only built-in tools). Until steps 18–19, a new tool there would wait for a click at the PC.

## Concepts

- **Probe:** a one-off script that asks a few read-only questions to learn the shape of NTULearn's answers before building on them. Prints counts, status codes and field names, never content.
- **RAG (retrieval-augmented generation):** find the right pieces of text first (retrieval: `search`), then let the AI answer from only those pieces (generation: Claude, on your Claude plan).
- **Embedding / vector:** a long list of numbers that stands for a text's meaning; texts that mean similar things get similar numbers.
- **Hybrid search:** keyword search and meaning search run together and their result lists are merged.
- **Rate limit:** how much a service lets you use per minute or per day; per project for Gemini, so keys in the same project share it.
- **Graceful degradation:** when one part is unavailable, fall back to something that still works (keyword-only search) and say so, instead of failing.
