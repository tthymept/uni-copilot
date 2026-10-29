# Phase 4: content and search

Status (29 Oct 2026): decisions made (all as recommended; embeddings with Google's Gemini first; probe approved). Step 0 done; step 1 (the live probe) done on 29 Oct in four runs, 344 GETs (results in the plan's Appendix A and "Changed by the probe" below). `upcoming_windows` dropped (your OK, 29 Oct); steps 2 (packages) and 3 (config) done, steps 4 (`get_file`), 5 (models), 6 (content parser), 7 (file text) and 8 (cache) done; next is step 9. Phase 3's test week (step 13) runs in parallel until about 3 Nov, and Phase 4 doesn't touch its pieces (see "Living next to the test week"). Before planning: working tree clean at `4de0371`, `uv run pytest` → 186 passed, 4 skipped; SQLite 3.53.1 in this project's Python has FTS5. Full details of every step: `phase-4-build-log.md` in this folder.

## What Phase 4 builds

Phases 1–3 know **when** things are due and **what profs posted**. They can't see the course itself: the folders, the Ultra pages, the slides and PDFs. Phase 4 lets Claude read those, find things in them, and get a file to you:

- **`list_course_content(course, folder_id=None)`:** one folder level of a course (like opening a folder on the course page): folders, pages, files, quizzes and assignments, links, and the files embedded in pages. Each item says whether it's open, conditional but open to you, held back until you finish another item, or (rarely) opens or closes on a date.
- **`read_page(course, content_id)`:** the text of an Ultra "Document" page, as light Markdown (Phase 1's HTML parser, reused), with its links and attached files.
- **`read_file_text(course, content_id, attachment_id | xid, pages=None)`:** the text of a PDF, PPTX or notebook, page by page (slide by slide), a few pages at a time, from a file item or a file embedded in a page. No OCR: a scanned PDF says "no text found".
- **`get_assessment(...)`:** one quiz or assignment, found by its content id or by its gradebook column id (the one deadlines carry): instructions, due date, attempts allowed, password needed, secure browser or webcam required, late attempts allowed, your status, and its release condition (open, held back until another item is done, or a date).
- **No `upcoming_windows`** (dropped 29 Oct after the probe): of 966 items only 8 have a date rule and none was still to come, so opening times are shown per item instead.
- **`search(query, course=None)`:** **hybrid** search over page text, file text, titles, and your announcements and messages: keyword search (SQLite FTS5) plus search by meaning (embeddings from Google's Gemini), merged into one ranked list with a short snippet and the source of each hit. If the meaning half isn't available (no key, Gemini's limit reached, no internet), search still answers with keyword results and says why.
- **`download_file(...)`:** saves one course file to `~/Downloads/uni-copilot/<COURSE>/`. Terminal only, asks you every time.
- **`send_course_file(...)`:** sends one course file to your own Telegram chat (after you say yes), without saving it on your PC. Built after the test week.
- **Caching:** content trees, page text and file text are kept in `.data/cache/content.sqlite`, so the second question about the same slides doesn't download them again.

How it fits with Phases 1–3:

```
NTULearn (GET only)
   │  content tree, Ultra pages, attachments (PDF/PPTX), assessments
   ▼
client/http.py ── get_json / get_all (unchanged) + get_file (new: streams bytes, size cap, checks redirects)
   ▼
client/contents.py ──► parsers/content.py  (content JSON → items: kind, locked, opens/closes)
   │               ──► parsers/html.py     (page HTML → Markdown, reused from Phase 1)
   │               ──► parsers/files.py    (PDF/PPTX/ipynb → text per page or slide)
   ▼
cache.py ── .data/cache/content.sqlite: folder listings, page text, file text,
   ▲        FTS5 keyword index, chunks + their embedding vectors
   │                                     ▲                                  ▲
   │ indexer (run by hand:               │ announcements + messages copied  │ embeddings.py ──► Gemini API
   │ walks every course, fills both      │ READ-ONLY from the Phase 2 store │ (course content and your search
   │ indexes, paced, resumable)          │ (keyword only, never sent out)   │  words only; limits handled)
   ▼
server.py: list_course_content, read_page, read_file_text, get_assessment, search,
           download_file (terminal only), send_course_file (after 3 Nov)
   │
   ▼
Claude Code (terminal; the Telegram session gets everything except download_file)
```

**RAG in one line:** `search` finds the right pieces (retrieval), and Claude, on your Claude plan, writes the answer from them and cites them (generation). Only the embeddings use a separate service.

Done when:

1. **"What does this week's <course> lecture cover?"** → Claude finds the week's folder with `list_course_content`, reads the slides with `read_file_text`, and answers with the file name, slide numbers and a link.
2. **"Where did <course> say what the quiz covers?"** → `search` finds it (an announcement, a page or a slide), and the answer names the source and how old the cached copy is.
3. **A question with no shared words** (e.g. "how do I find the shortest route in a graph?" when the slides say "Dijkstra") → hybrid search finds it; keyword-only doesn't. You compare 10 questions both ways.
4. **With Gemini unavailable** (key removed, or the limit reached) → search still answers with keyword results and says plainly that meaning search is off and why; the indexer stops cleanly, keeps its progress and says when to run it again.
5. **"When does <quiz> open?"** → Claude gives the date if NTULearn has a date rule for it, and otherwise says plainly what it can see: open to you, or held back until another item is done.
6. **`download_file` saves a PDF** to the downloads folder from the terminal (after Claude Code asks you), and doesn't exist in the Telegram session.
7. **Phase 3 kept running all along:** `.data\sync.log`, `remind.log` and `digest.log` look normal, and all old tests still pass.

## Living next to the test week

The scheduled sync **imports Phase 1 code that Phase 4 extends**: `client/http.py`, `client/courses.py`, `models.py`, `config.py` (through `DeadlinesClient`, `AnnouncementsClient`, `MessagesClient`). A mistake in those files would break the 2-hourly sync, and so the test week. So:

- In shared files, Phase 4 **only adds** (new functions, new models, new constants). `get_json`, `get_all`, the existing models and clients stay exactly as they are.
- After every step that touches a shared file: `uv run pytest` (all old tests still pass), and the next `.data\sync.log` line is checked.
- New code goes in new files where possible: `client/contents.py`, `parsers/content.py`, `parsers/files.py`, `cache.py`, `search.py`, `embeddings.py`, `indexer.py`, `probe_content.py`.
- **Not touched** during the test week (Phase 3 pieces): `store/`, `sources/`, `pilot_mcp/`, `notify/`, `cli.py`, `filelock.py` (only used), the `scripts\schedule-*.ps1` tasks, the two skills, `.claude/telegram-session.json`. Exceptions you agreed to: one line in `scripts\start-telegram.ps1` (step 19, deny `download_file`), and, **after 3 Nov**, one new function in `notify/telegram.py` (step 21, sending a file).
- **Installing packages (step 2), decided 29 Oct:** a plain `uv add` reinstalls the project, which rewrites `ntulearn-mcp.exe` and fails while any Claude Code session (this one included) is running that server. So step 2 installs in two parts that never touch the project's own programs: `uv add --no-sync pypdf python-pptx numpy` (writes only `pyproject.toml` and `uv.lock`), then `uv sync --inexact --no-install-project` (installs just the new packages into `.venv`; `--inexact` removes nothing, so the scheduled jobs keep working). Flags checked in uv 0.12.11. Other sessions closed first anyway (the Telegram one too; restart it after step 2), not at :00–:12. A plain `uv sync` once some day when no session is open tidies the rest.
- Nothing new runs on a schedule in Phase 4, so `docs/how-it-runs.md` doesn't change. If a scheduled indexer is added later, it follows "poke often, decide in code" and gets its section there.

## Steps

One file per step. You test each step before the next, like Phases 1–3.

0. Housekeeping: decisions recorded here and in the plan doc (status, non-goals, sections 4.1, 4.3, 7, 8, 9, 11, Appendix B), `phase-4-build-log.md` created (Phase 3's format), `.data/cache/` and `.data/index.lock` confirmed git-ignored (inside `.data/`). **Done.**
1. `src/uni_copilot/ntulearn_mcp/probe_content.py`: **the live probe** (approved). GET only, your account, prints counts, status codes and field names only (never titles, bodies, file names, dates or URLs). Checks the NTULearn items in "Things to check". Results go into the plan's Appendix A as verified facts, and any later step that guessed wrong is changed before it's built. Kept afterwards as a re-runnable check (like `spike_session_check.py`). **Done** (29 Oct: four runs, `--follow-up`, `--follow-up-2`, `--follow-up-3`; 344 GETs in total, counts and field names only).
2. `pyproject.toml` (+ `uv.lock`): `uv add --no-sync pypdf python-pptx numpy`, then `uv sync --inexact --no-install-project` (see "Installing packages" above: this session can stay open). No Google package: Gemini is called with `requests`, like Telegram. Test: `uv run python -c "import pypdf, pptx, numpy"`, then `uv run pytest`. **Done** (29 Oct: pypdf 6.21.4, python-pptx 1.0.2, numpy 2.5.4, plus lxml, pillow and xlsxwriter that python-pptx needs).
3. `src/uni_copilot/ntulearn_mcp/config.py`: new constants only: the cache file path, freshness (6 h), size limits (read ≤ 50 MB, download ≤ 200 MB), the downloads folder (`UNI_COPILOT_DOWNLOAD_DIR`, default `~/Downloads/uni-copilot`), the indexer's pace (1 request every 2 s), the embedding settings (provider, model, vector size, Gemini's per-minute limits with a safety margin). **Done** (29 Oct; also two `Settings` fields with defaults, `embeddings` and `download_dir`, so existing callers are unaffected; `UNI_COPILOT_EMBEDDINGS=off` turns meaning search off).
4. `src/uni_copilot/ntulearn_mcp/client/http.py`: `get_file(path, max_bytes)`: a GET that streams the bytes, stops at the size limit, and follows the download redirect itself (only to hosts the probe found; no NTULearn cookies sent to another host; the signed redirect URL is never logged or stored, like the Kaltura `ks` rule). Needed because the existing "redirected to another host = signed out" check would misread a file download. `get_json` and `get_all` unchanged. Live self-test on one attachment: status, size and type only. **Done** (29 Oct; also embedded files' signed links as a start; offline check with fake sessions and live `--file` check passed).
5. `src/uni_copilot/ntulearn_mcp/models.py`: new result models: `ContentItem` (title UNTRUSTED, kind, release condition: open / conditional, open to you / held back / opens-closes in UTC + SGT), `EmbeddedFile` (name UNTRUSTED, type, `xid`; never its link), `ContentList`, `PageText`, `FileText` (pages returned, total pages, cut or not), `Assessment`, `SearchHit` / `SearchResults` (with `search_mode`: hybrid or keyword-only, and why), `DownloadResult`. Every content result says when it was fetched ("cached at … SGT"). Existing models unchanged. Offline self-test. **Done** (29 Oct: 12 new models; no grade fields and no file-link fields, checked by the self-test).
6. `src/uni_copilot/ntulearn_mcp/parsers/content.py`: pure functions, no network: raw content JSON → `ContentItem` (content handler → kind, release rules + `visibility` → the release condition, the Ultra page = a folder with one `ultraDocumentBody` document), embedded files from page bodies (`data-bbfile` → name, type, `xid`), any remaining signed `/bbcswebdav/` link in page text → `[file: name]`, and assessment fields (never `score`, `possible` or `gradesReleased`). Offline self-test on made-up JSON. **Done** (29 Oct).
7. `src/uni_copilot/ntulearn_mcp/parsers/files.py`: bytes → text per page or slide. PDF with pypdf, PPTX with python-pptx (slide text, tables, speaker notes), `.ipynb` with the standard `json` module (Markdown and code cells, no outputs), plain text files. Anything else: "not supported yet". Only reads: nothing is run or opened in another program. Offline self-test on tiny files it builds itself; then one of your real slide PDFs (if the text comes out jumbled, switch to pypdfium2). **Done** (30 Oct; also a zip-bomb guard for PPTX, `choose_pages` for page ranges, and unusual PPTX shapes skipped safely; live `--real` check: PDF, PPTX and notebook text all clean, pypdf stays).
8. `src/uni_copilot/ntulearn_mcp/cache.py`: `.data/cache/content.sqlite`: folder listings, page text and file text with the time fetched, plus the FTS5 search table. File text is kept until the file changes. Never stores file bytes, download URLs, grades or tokens. Deleting the file is always safe. Offline self-test in memory. **Done** (30 Oct; also attachments, removal of items that disappear, and a last-line mask for signed links).
9. `src/uni_copilot/ntulearn_mcp/client/contents.py`: `ContentsClient`: the whole tree (one recursive request per course, through the cache), one folder with its release rules (the internal folder list, 1 request), read a page, get a file's text from a file item or an embedded file (download → extract → cache the text → drop the bytes; an embedded file's link comes from a fresh page body), save a file, find an assessment by gradebook column id. Live self-test, counts only (also: how many embedded files have no link, the open question from the probe).
10. `src/uni_copilot/ntulearn_mcp/server.py`: the first tools: `list_course_content`, `read_page`, `read_file_text`, `get_assessment`. All `read_only_hint=True`, each with `refresh` for "check live now". Test: `--self-test` and the MCP Inspector, then a real question in Claude Code.
11. `src/uni_copilot/ntulearn_mcp/search.py`: keyword search: FTS5 match, ranked (BM25), with snippets, filtered by course and kind. Before searching, announcements and messages are copied from the Phase 2 store (opened read-only) if its last sync is newer than the last copy. Every result says what's indexed ("6 of 8 courses, 120 files, as of …"), so a miss is never mistaken for "it's not there". Offline self-test.
12. `src/uni_copilot/ntulearn_mcp/indexer.py`: run by hand, `uv run python -m uni_copilot.ntulearn_mcp.indexer [--course IE2108] [--dry-run]`: walks every current-term course's content, extracts new or changed PDFs/PPTX into the cache, prints counts. 1 request every 2 s, its own lock `.data/index.lock`, and the usual sign-in lock. Later runs only fetch what changed.
13. `src/uni_copilot/ntulearn_mcp/server.py`: `search` (keyword for now) and `download_file` (downloads folder only, safe names, never overwrites, no videos, Mark of the Web). Test: search for a word you know is in a slide; download one PDF. **From here, keyword search is usable on its own.**
14. `src/uni_copilot/ntulearn_mcp/embeddings.py`: cuts text into chunks (a few hundred words, keeping the course, file, page or slide, and link), and turns chunks and questions into vectors. One small interface with Gemini behind it first (`gemini-embedding-001`, documents as `RETRIEVAL_DOCUMENT`, questions as `RETRIEVAL_QUERY`, shorter vectors to keep the file small), so OpenRouter or a local model can be swapped in later. Paces itself under Gemini's per-minute limits and tells the limit cases apart (see decision 13). Key from `.env` (`GEMINI_API_KEY`), never printed or logged. Offline self-test with a fake model; a live `--check` embeds one made-up sentence.
15. `src/uni_copilot/ntulearn_mcp/cache.py`: chunks and their vectors (stored compactly, with the model name, so a model change is noticed), and which chunks still need embedding. Offline self-test.
16. `src/uni_copilot/ntulearn_mcp/search.py`: hybrid ranking: the keyword list and the meaning list (cosine similarity with `numpy`) merged by reciprocal rank fusion. Falls back to keyword-only with a clear reason when the question can't be embedded. Offline self-test with fake vectors.
17. `src/uni_copilot/ntulearn_mcp/indexer.py`: also embeds new chunks: prints the token count and the estimated cost first, `--max-tokens` to stop early, waits out per-minute limits, stops cleanly at the daily limit with progress saved, and `--embed-only` to carry on the next day without walking NTULearn again.
18. `.claude/settings.json`: pre-allow the read-only content tools and `search` (so Telegram and terminal questions don't stall); `download_file` is **not** pre-allowed, so Claude Code asks you every time.
19. `scripts/start-telegram.ps1`: add `mcp__ntulearn__download_file` to the session's `--disallowedTools`. Then restart the Telegram session once. Test: in that session, `download_file` isn't in the tool list.
20. `CLAUDE.md`: routing for content questions ("what's in the slides", "where did they say…" → the content tools and `search`, saying "cached at …" and when search ran keyword-only), the Telegram rules for file text (summaries, not whole files), the probe, indexer and embedding commands, `GEMINI_API_KEY` in the secrets rule.
21. `src/uni_copilot/notify/telegram.py` (**after 3 Nov**): `send_document(name, bytes)`: Telegram's `sendDocument`, only to your chat id from `.env`, at most 50 MB, the token scrubbed from errors like `send_message`. A new function only.
22. `src/uni_copilot/ntulearn_mcp/server.py` (**after 3 Nov**): `send_course_file`: fetches the file into memory and sends it with step 21's function; PDF/PPTX only; Claude says which file and its size and sends after your "yes". Pre-allowed like `send_telegram` (it can only reach your own chat), and added to the guard test.
23. `tests/`: synthetic only: content JSON → items (kinds, release conditions), the Ultra page, embedded files (and their signed links never in any output), PDF/PPTX/ipynb text from tiny files built in the test, cache freshness and "file changed → re-read", keyword ranking, snippets and the post copy, chunking, hybrid merging, every Gemini failure case answered properly (fake server: per-minute limit waited out, daily limit → keyword-only with the reset time, bad key, no internet, the key never in an error), safe file names (`..\`, `CON`, very long names), never overwriting, size limits, redirects to unknown hosts refused, no URL or token in errors, and a guard test: **every `ntulearn` tool is either pre-allowed or denied in the Telegram session**, so a new tool can't slip into Telegram unnoticed.
24. Final check: the "done when" items in a fresh session and from Telegram; Phase 3 logs normal; plan doc, CLAUDE.md and logs updated.

Steps 21–22 wait for the test week to end (~3 Nov); if we reach them earlier, we do step 23–24's parts that don't depend on them first.

**Later (each needs your OK, not part of "done"):** `weekly-actions` using `search` and the quiz details (password, secure browser); a scheduled indexer (poked nightly, the code decides); `.docx` text; `get_announcement`.

## Decisions

Made 29 Oct 2026: all as recommended; for decision 13 you chose Gemini first and asked that hitting its limit gives a proper answer, never a bare failure. Probe approved. Also recorded in the plan doc.

1. **Tool order by what each needs:** `list_course_content` → `read_page` → `read_file_text` → `get_assessment` → `search` → `download_file` → `send_course_file`. Search needs the text the others produce; the tools that write to your disk or send things come last. (Not chosen: search first; download first.)
2. **Hybrid search:** SQLite FTS5 keyword search first (built in, instant, BM25 ranking, stemming), then embeddings on top, merged by reciprocal rank fusion. Keyword search catches exact words ("Lab 4", "IE2108"), embeddings catch meaning ("shortest route" ↔ "Dijkstra"). Built in that order, so you can compare keyword-only with hybrid. All in its own file `.data/cache/content.sqlite`, not the event store. (Not chosen: embeddings only; no index.)
3. **Search covers content plus announcements and messages.** Posts are copied from the Phase 2 store, opened read-only and never changed, refreshed when the store's last sync is newer than the last copy. Posts are keyword-only (decision 13). (Not chosen: content only; a live fetch of posts per search.)
4. **The index is filled on demand and by an indexer you run by hand,** at 1 request every 2 s (it may overlap a scheduled sync or a Claude session, each with its own pacer). A scheduled indexer may come after the test week, then with its section in `docs/how-it-runs.md`.
5. **Caching:** `.data/cache/content.sqlite`. Folder listings and page text fresh for 6 hours (`refresh=true` checks live); file text kept until the file changes; vectors kept until their text or the model changes. Never on disk: file bytes, download and redirect URLs, grades, API keys. Every cached answer says "cached at <time> SGT".
6. **Libraries:** pypdf (PDF) and python-pptx (PPTX); `.ipynb` with the standard library; `numpy` for vector maths. `.docx` later. If pypdf's text is jumbled on your real slides, pypdfium2 replaces it (one function). (Not chosen: PyMuPDF, AGPL licence.)
7. **`download_file` saves to `~/Downloads/uni-copilot/<COURSE>/`** (changeable with `UNI_COPILOT_DOWNLOAD_DIR`); no free choice of folder. Untrusted file names are cleaned (no `..`, `\`, `/`, Windows names like `CON`, very long names shortened), nothing is overwritten (`name (2).pdf`), videos and files over 200 MB are refused, the file is never opened or run, and it gets Windows' Mark of the Web.
8. **The Telegram session gets all read-only content tools and `search`** (pre-allowed); `download_file` is **denied** there (`--disallowedTools` in `start-telegram.ps1`, which removes it from the session) and asks every time in the terminal. A guard test checks every `ntulearn` tool is either pre-allowed or denied in Telegram. (Not chosen: only search and list from Telegram; nothing until after the test week.)
9. **Opens/closes windows: no `upcoming_windows` tool** (changed 29 Oct after the probe, your OK: "do as you recommend"). The probe found 8 date rules in 966 items, none still to come; 134 rules are "after you finish another item". So each item shows its release condition instead (open / conditional, open to you / held back until another item is done / opens or closes on a date), in `list_course_content` and `get_assessment`, read from the internal folder list at no extra cost. Store events and "opens today" reminders are no longer planned; can be revisited if date rules show up later in the semester. (First plan: an `upcoming_windows` tool. Not chosen: inside `upcoming_deadlines`, which the scheduled sync uses.)
10. **`get_assessment` takes a content id or a gradebook column id,** so a deadline (live or from pilot) leads straight to its instructions.
11. **Live probe first** (step 1), GET only, counts, status codes and field names only. Discussions and Groups only probed; their tools stay in Phase 8.
12. **`send_course_file`** sends a course file to your own Telegram chat after your "yes": fetched into memory, never saved on your PC, PDF/PPTX only, ≤ 50 MB. Built after the test week (it needs a new function in `notify/telegram.py`). Course files then pass through Telegram's servers (bot chats aren't end-to-end encrypted). (Not chosen: link only; the plugin's own file attachments, which would need a disk write in the Telegram session.)
13. **Embeddings: Google's Gemini API first** (`gemini-embedding-001`, free tier), behind one small interface so OpenRouter or a free local model (`fastembed`) can be swapped in later (a model change means re-embedding everything once).
    - **Its own Google Cloud project and key** (`GEMINI_API_KEY` in `.env`, typed by you), because Gemini's limits are per project: 100 requests/min, 30,000 tokens/min, 1,000 requests/day on the free tier (your screenshot). The first full index (~2 million tokens, a guess) takes about an hour or more, possibly over two days.
    - **What leaves your PC:** course content (page text, file text, titles) and your search words. Never announcements, messages or grades.
    - **Free tier and your data:** Google may use text sent on the free tier to improve its products, and people may review it. Accepted for course content for now; turning on billing for that project stops it (~$0.15 per million tokens, about $0.30 for everything).
    - **This changes a plan non-goal** ("no paid AI API"): Claude's answers stay on your Claude plan; only embeddings use an outside API (free for now).
    - **Hitting a limit gives a proper answer, never a bare failure** (your rule):
      - Per-minute limit (Gemini's 429 with a retry delay): the code waits the delay and carries on; the indexer prints "waiting 40 s for Gemini's per-minute limit".
      - Daily limit, no key, a rejected key, Gemini down or no internet, **during a search**: search returns keyword-only results with `search_mode = "keyword_only"` and a plain reason, e.g. "Meaning search is off: Gemini's free daily limit is used up (it resets at midnight US Pacific time, about 15:00–16:00 SGT). These are keyword results." Claude passes that on in its answer.
      - The same, **during indexing**: the indexer stops cleanly, keeps everything done so far, and prints what's left and when to run it again (`--embed-only` carries on without walking NTULearn again). Chunks without vectors are still found by keyword search.
      - Never retried in a tight loop, never a stack trace to you, and the key is never in a message.

## Changed by the probe (29 Oct)

All agreed 29 Oct ("for that tool do as you recommend"); already worked into the steps and decisions above:

- **Opens/closes windows (decision 9): `upcoming_windows` dropped.** Of 966 items, 8 have a date rule and none is still to come; the rest are "after you finish another item". A tool that walks ~140 items to find nothing isn't worth it. Instead, through the internal folder list (1 request per folder, so no extra cost): `list_course_content` and `get_assessment` show each item as **open**, **conditional, open to you** (the rule is met), **held back: after you finish another item** (NTULearn doesn't say which; the rule's title, written by the prof, may, labelled UNTRUSTED), or **opens <date> / closes <date>** for a date rule. The 4 Turnitin items' start/due dates come from their settings. Done-when item 5 changed to match.
- **Embedded files (new, follows from the facts):** about half the slides sit inside Ultra pages. `list_course_content` and `read_page` list a page's embedded files (name, type, an id `xid-N_N`); `read_file_text`, `download_file` and `send_course_file` take either a file item (`content_id` + `attachment_id`) or an embedded file (page `content_id` + `xid`). The link is taken from the page body fetched just now, never from the cache. Skipped: embedded apps and videos (Phase 5), images.
- **Signed links in page text:** `parse_html` (Phase 1, shared with the sync, not changed) already hides links on file tags. `parsers/content.py` additionally turns any remaining `/bbcswebdav/` link with a query string into `[file: name]`, so a link a prof pasted by hand can't leak into answers or the cache either.
- **`get_assessment` gains what to prepare:** attempts allowed, password needed, secure browser or webcam required, late attempts allowed or not. Useful for "anything I need to prepare?". Never `score`, `possible` points or `gradesReleased`.
- **Step 4 (`get_file`):** takes a path or a full NTULearn link; follows up to 5 redirects; allowed hosts `ntulearn.ntu.edu.sg` (with cookies) and `*.blackboard.com` (without); counts bytes as they arrive (no `Content-Length`); never logs a link.
- **Step 8 (`cache.py`):** file text keyed by attachment id or `xid` + the item's `modified`. Never raw HTML (it holds signed links).
- **Steps 9 and 12:** one recursive request per course for the tree, which also brings every page body (and so the fresh embedded links). Indexer's first run: ~170 file items × 4 requests + ~140 embedded PDF/PPTX/notebook/text files × 3 ≈ 1,100 requests ≈ 40 minutes at its pace; later runs only fetch items whose `modified` changed.
- **Mime types:** `application/octet-stream` occurs, so the file type is also taken from the file name's extension.

## Things to check

Checked by the probe on 29 Oct (details in the plan's Appendix A):

- ✓ **Walking the tree:** `.../contents?recursive=true` lists a whole course in one request (a few pages at most). The indexer lists all 16 sites in about 20 requests.
- ✓ **Ultra pages:** 136 folders with one `ultraDocumentBody` document, as expected; 212 of 221 documents have a body.
- ✓ **Attachment fields:** only `id`, `fileName`, `mimeType`: no size, no modified time. So the cache notices a changed file from the **content item's `modified`** time (every item has one). Not yet checked: that replacing a file really changes `modified`.
- ✓ **The download:** three redirects: NTULearn → NTULearn (signed link) → `alt-<id>.blackboard.com` (signed link, no cookies needed) → 200, with **no `Content-Length`**. So `get_file` follows up to 5 redirects, allows only `ntulearn.ntu.edu.sg` (with cookies) and `*.blackboard.com` (without), and counts bytes as they arrive to enforce the size limit. 4 requests per file: a full first index of ~220 files is roughly 900 requests, about 30 minutes at the indexer's pace.
- ✓ **Availability windows: none.** No item has `adaptiveRelease` dates; the internal item shows none either. Only 4 Turnitin items carry start/due dates in their settings. See "Changed by the probe".
- ✓ **Assessments:** 119 test links, all with `gradeColumnId` (so deadlines lead straight to them); attempts from the gradebook column (`grading.attemptsAllowed`); `password.enabled`, secure browser and webcam flags on the item; `deploymentSettings` on the internal item. No time-limit field on the 3 probed.
- ✓ **Discussions and Groups:** no discussions (0 on 16 sites, and no discussion items); **193 groups** with sign-up sheet fields (Phase 8).
- ✓ **"Viewed" state:** a download changed none of the item's tracking fields. Whether NTULearn's own server logs record API reads can't be seen from outside.

Checked by the follow-up on 29 Oct:

- ✓ **`PartiallyVisible` is not locked:** it means "visible with release conditions". Those folders open and those files list their attachments. Shown as "conditional", never as "locked".
- ✓ **The 400s:** pages (`x-bb-document`) have no attachment list at all; their files are embedded in the body.
- ✓ **Embedded files: 283 on 164 pages, 126 of them PDFs.** About as many slides live inside Ultra pages as in separate file items, so `read_file_text` and `download_file` must handle embedded files too.
- ✓ **Release rules exist** (`adaptiveReleaseRules`, with `criteria[].start` / `end`), but only on the internal item, one request per item.

Checked by the second follow-up on 29 Oct:

- ✓ **What the release rules are:** 134 of 142 are "after you finish another item" (`Pre_Content`); only 8 are date ranges, and none of those is still to come (7 open now, 1 closed). NTULearn's own `visibility` says whether you meet the rule: 124 already open to you, **18 really held back**.
- ✓ **The internal folder list carries the rules**, so one folder's conditions cost 1 request, not one per item.
- ✓ **Embedded files with a `resourceUrl`** (39 of 283) download like attachments: `/bbcswebdav/...` → the Blackboard file server → the file. Almost none say their size (7 of 283), so the size limit is always counted while downloading.

Checked by the third follow-up on 29 Oct:

- ✓ **Embedded files' links:** `viewerUrl` and `resourceUrl` are both a signed `/bbcswebdav/.../xid-N_N` file link (the viewer one adds display settings), and a viewer-only PDF downloaded the same way. So every embedded file with a link is reached the same way, in 3 requests, and its link comes with the page body the tree listing already returns (no extra request).
- ✓ **The links are signed** (three signature parameters): treated as secrets, read fresh each time, never cached, logged or shown. `xid-N_N` (the file's id inside NTULearn's file store) is not secret and works as the embedded file's id.
- Open: 38 embedded files have no link in their JSON (maybe in the `<a href>`); step 9's live self-test counts them. Until then they're listed as "no download link found".

Not verified yet; the step named checks each one:

- **Text quality** on your real slides (step 7): reading order, spacing, slides that are only pictures.
- **Size and time:** how big `content.sqlite` gets, how many tokens your files really are, and how long the first full index takes (steps 12, 17).
- **Gemini** (steps 14, 17): whether a batch call (many chunks in one request) counts as one request toward the 1,000 a day; the exact error Gemini sends for "per-minute" vs "daily" limits (so the two are told apart correctly); when the daily limit resets; the current free-tier data terms.
- **Whether embeddings help:** the same 10 questions with keyword-only and hybrid search, compared by you (step 24).
- **Three programs at once:** the scheduled sync, the Telegram session and a terminal session each run their own ntulearn code with their own 1-per-second pacer. Fine for short calls; the indexer's slower pace is for this. (Step 12)
- **The Telegram session's tool list** after step 19: `download_file` absent, the read-only content tools and `search` present and not prompting.
- **Shared files and the scheduled sync** (steps 3–5): the next `.data\sync.log` line after each step.

## New ideas you'll meet

Explained in the build log when they first appear, tied to the code:

- **Probe:** a one-off script that asks a few read-only questions to learn the shape of the answers before building on them.
- **Content tree and content handlers:** how a Blackboard course is a tree of items, and how `resource/x-bb-folder`, `x-bb-document`, `x-bb-file` and the test links say what each item is.
- **Ultra Document:** a page that the API stores as a folder with one hidden document inside.
- **Adaptive release / availability window:** "show from … until …" dates on an item.
- **Full-text search, FTS5, tokenizer, stemming, BM25, snippet:** how a search index splits text into words, matches "searching" to "search", ranks results, and shows the matching bit.
- **RAG, embeddings, vectors, cosine similarity, chunking, hybrid search, reciprocal rank fusion:** how text becomes a list of numbers that's close to other text with a similar meaning, how two ranked lists are merged, and how Claude answers from the pieces search hands it.
- **Rate limits and graceful degradation:** per-minute vs per-day limits, waiting vs stopping, and falling back to something that still works instead of failing.
- **Cache, freshness (TTL) and change detection:** keeping a copy, how long it counts as fresh, and noticing when the original changed.
- **Streaming download and size limit:** reading a file in chunks so a huge file can't fill memory.
- **HTTP redirect and signed URL:** why a download goes to another server with a temporary key in the link, and why that link is never stored.
- **Path traversal and safe file names:** how a file name like `..\..\x` could escape a folder, and how we stop it.
- **Mark of the Web:** the note Windows keeps on downloaded files.
- **PDF and PPTX text extraction:** what's in these files, and why scanned pages have no text.
- **Allow list vs deny list:** why the Telegram session lists built-in tools it keeps, but MCP tools it removes, and the test that keeps the two in step.
- **Prompt injection through files:** slides and PDFs are other people's text too; why Claude treats them as data.
