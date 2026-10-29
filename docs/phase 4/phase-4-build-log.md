# Phase 4 build log

What each finished step changed, how to test it, and its commit. Unverified points from each step are collected in the "Open items" section at the bottom, and new terms in the "Concepts" section. The short overview is `phase-4-core.md` in this folder. Phase 3 (Telegram, whose test week runs alongside the start of this phase) is in `docs/phase 3/`.

## How it all connects (read this first when you come back)

### The layers

```
NTULearn (GET only)
  └─ client/http.py: get_json / get_all (Phase 1, unchanged) + get_file (Phase 4: streamed, size cap, checked redirects)
       └─ client/contents.py: folders, pages, file text, assessments, windows
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
