"""
Search over everything the content cache holds, plus your announcements and
messages: keyword search and meaning search together. Never contacts NTULearn.

In plain words:
  Browsing (list_course_content, read_file_text) answers "read this place".
  Search answers "which place?": it looks through every cached title, page,
  file page, announcement and message at once and returns the best matches,
  each with a short snippet and where it is (course, file, page), so Claude
  can then read just that page.

How:
  - The search table is SQLite FTS5 (cache.py, `search_text`): a word index.
    The query's words are matched with stemming ("rotations" finds
    "rotation"); results are ranked with BM25 (more matches in a shorter
    text rank higher; a match in the title counts 5 times as much).
  - All words must match first; if that finds nothing, any word may match
    (and the result says so). Then rows with MORE of the words come first,
    BM25 only orders rows with as many words; each hit lists its
    matched_words. (Without this, a rare word alone outranks a page with
    the common word: "AVL rotation" put another course's "rotation" page
    above the AVL slides, 30 Oct.) Very common words ("what", "is", "the")
    are left out of the query unless it has nothing else.
  - Copies are merged: the same text in several places (one file attached
    to 10 items, an announcement posted to two course sites) becomes one
    hit with `also_in` listing the other places.
  - Data files (.csv) are left out of search: their text is rows of numbers.
    The indexer doesn't read them; read_file_text still can.
  - Your words are never passed to FTS5 as they are: each becomes a quoted
    term, so characters like - : " * or the words AND / OR / NEAR can't change
    the query's meaning or break it.
  - Announcements and messages come from the Phase 2 event store
    (.data/uni-copilot.sqlite), opened READ-ONLY (SQLite's mode=ro): never
    changed. They're copied into the search table only when the store's last
    sync times differ from the last copy (~60 posts, well under a second).
  - Every answer has `coverage`: how many courses have a cached tree, how many
    files are read out of how many known, how many posts, and when the
    indexer last finished. A miss with low coverage means "not indexed yet",
    not "not there".

Meaning search (step 16), next to the keywords:
  - Your question becomes a vector (Gemini, 1 request; the same question
    again within this session is remembered, no request). Every chunk's
    vector (cache.vector_matrix) is compared with it at once: one matrix
    multiplication gives each chunk's cosine similarity (vectors have length
    1, so that's a dot product). The closest chunks above MIN_SIMILARITY win.
  - It covers course files and pages (the chunks); titles and posts are
    keyword-only (posts are never sent to Google).
  - The two ranked lists are merged by reciprocal rank fusion (RRF): each
    hit scores 1/(60 + its rank) in every list it's in, added up. A hit both
    searches found rises to the top; a hit only one found keeps its place.
    Both lists point at rows of the same search table, so the same page found
    both ways is one hit with found_by ["keyword", "meaning"].
  - search_mode is "hybrid" when meaning search took part, else
    "keyword_only" with mode_reason in plain words: off, no key, no vectors
    yet, Gemini's limits (when the daily one resets), offline, or a search
    only over titles/posts. Never an error: keyword results always come back.

Untrusted: titles and snippets are other people's text. Nothing here acts on them.
"""

import argparse
import re
import sys
import sqlite3
from pathlib import Path

import threading
from collections import OrderedDict
from collections.abc import Callable

import numpy as np

from uni_copilot.ntulearn_mcp.cache import ContentCache, time_from_db
from uni_copilot.ntulearn_mcp.config import DATA_DIR, DATA_EXTENSIONS
from uni_copilot.ntulearn_mcp.embeddings import (
    GEMINI_MODEL_ID,
    Embedder,
    EmbeddingUnavailable,
    make_embedder,
)
from uni_copilot.ntulearn_mcp.models import SearchCoverage, SearchHit, SearchResults, SearchSource, course_url
from uni_copilot.ntulearn_mcp.parsers.files import TEXT_EXTENSIONS

# The Phase 2 event store (written by `uni-copilot sync`; only read here).
STORE_PATH = DATA_DIR / "uni-copilot.sqlite"
# Its "last successful sync" notes for posts: when either changes, re-copy.
STORE_MARKER_KEYS = ("last_sync:ntulearn_announcement", "last_sync:ntulearn_message")
READABLE_EXTENSIONS = {".pdf", ".pptx", ".ipynb", *TEXT_EXTENSIONS}
INDEXED_EXTENSIONS = READABLE_EXTENSIONS - DATA_EXTENSIONS
MAX_HITS = 30
MAX_WORDS = 12          # words used from one query (each costs one extra lookup)
MAX_ALSO_IN = 5         # places listed in a hit's also_in
TITLE_WEIGHT = 5.0      # a match in a title counts this many times a match in the text
SNIPPET_WORDS = 14
WORD_RE = re.compile(r"\w+", re.UNICODE)
MATCH_START, MATCH_END = "\x02", "\x03"   # FTS5 marks matches with these first
KEYWORD_RESULTS = "These are keyword results."
RRF_K = 60              # reciprocal rank fusion's usual constant: rank 1 scores 1/61, rank 10 1/70
# Below this cosine similarity a chunk isn't "about" the question. Measured
# on your 4,808 texts (30 Oct): unrelated text sits at ~0.46-0.49 (median),
# an off-topic question ("penguin migration in antarctica") peaks at 0.57,
# and on-topic slides score 0.60-0.73 ("AVL rotation": 114 texts >= 0.60,
# all IE2108; at 0.55 unrelated IE0005/CC0006 texts crept in).
MIN_SIMILARITY = 0.60
MEANING_SOURCES = {"file", "page"}   # what has chunks
QUERY_MEMORY = 64       # question vectors remembered, so repeating a search costs no request
# Question words and glue: they match almost every row, so they'd decide the
# "most words matched" order without saying anything about the topic.
STOP_WORDS = {
    "a", "an", "and", "are", "about", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how",
    "i", "in", "is", "it", "its", "me", "my", "of", "on", "or", "the", "this", "that", "to", "was", "what",
    "when", "where", "which", "who", "why", "with",
}


# The user's words to search for: lowercase, no repeats, no one-letter words
# (except digits), common words left out unless nothing else is left, at most
# MAX_WORDS. Raises ValueError if there are no words.
def query_words(query: str) -> list[str]:
    words = list(dict.fromkeys(w for w in WORD_RE.findall(query.lower()) if len(w) > 1 or w.isdigit()))
    if not words:
        raise ValueError("Give some words to search for.")
    return ([w for w in words if w not in STOP_WORDS] or words)[:MAX_WORDS]


# One word as a safe FTS5 query: quoted, so nothing in it is read as FTS5
# syntax (- : * AND OR NEAR ...).
def _quoted(word: str) -> str:
    return '"' + word.replace('"', "") + '"'


# The two FTS5 queries for a list of words: all words ("a" "b" = both must
# appear) and any word ("a" OR "b").
def match_queries(words: list[str]) -> tuple[str, str]:
    quoted = [_quoted(w) for w in words]
    return " ".join(quoted), " OR ".join(quoted)


# The key under which rows count as copies of each other: same kind of hit
# and same text (and, for posts and titles, the same title; for files, the
# same page number). Two files with the same text are copies whatever their names.
def _copy_key(row) -> tuple:
    if row["source"] == "file":
        return ("file", row["page"], row["body"])
    return (row["source"], row["title"], row["body"])


# Merges copies: keeps the first (best) row of each group and remembers the
# others' "COURSE title" as its also_in. Returns [(row, also_in)].
def _merge_copies(rows: list) -> list[tuple]:
    groups: dict[tuple, tuple] = {}
    for row in rows:
        key = _copy_key(row)
        if key in groups:
            groups[key][1].append(f"{row['course_code'] or ''} {row['title'] or ''}".strip())
        else:
            groups[key] = (row, [])
    return list(groups.values())


# A search-table row as a plain dict (the columns both lists share).
def _plain(r) -> dict:
    return {k: r[k] for k in ("rowid", "source", "title", "body", "course_code", "site_id", "content_id",
                              "file_ref", "page", "event_id")}


# Reciprocal rank fusion: merges the keyword and meaning lists (rows of the
# same table, so the same rowid is the same place). Each row scores
# 1/(RRF_K + rank) per list it's in; best total first. A row in both keeps the
# keyword version (bold snippet, matched words) plus the meaning score.
def _fuse(keyword: list[dict], meaning: list[dict]) -> list[dict]:
    total: dict[int, float] = {}
    merged: dict[int, dict] = {}
    for name, rows in (("keyword", keyword), ("meaning", meaning)):
        for rank, row in enumerate(rows, 1):
            rid = row["rowid"]
            total[rid] = total.get(rid, 0.0) + 1.0 / (RRF_K + rank)
            if rid in merged:
                merged[rid]["found_by"].append(name)
                merged[rid]["score"] = merged[rid]["score"] or row["score"]
            else:
                merged[rid] = {**row, "found_by": [name]}
    return [merged[rid] for rid in sorted(merged, key=lambda rid: -total[rid])]


# Mixes kinds of hit: rows come best first; no single source (title, file,
# page, announcement, message) may take more than half of `limit`, unless
# nothing else matched. Without this, short title rows (weighted 5x) can
# crowd out the posts and pages that say something about the words.
def _varied(rows: list[tuple], limit: int) -> list[tuple]:
    cap = max(1, (limit + 1) // 2)
    picked, held, per_source = [], [], {}
    for pair in rows:                      # (row, also_in) from _merge_copies
        source = pair[0]["source"]
        if per_source.get(source, 0) < cap:
            picked.append(pair)
            per_source[source] = per_source.get(source, 0) + 1
        else:
            held.append(pair)
        if len(picked) == limit:
            return picked
    return picked + held[:limit - len(picked)]


class ContentSearch:
    # Keeps the cache and where the event store is (tests pass temporary
    # files; None = STORE_PATH, looked up now so tests can point it elsewhere),
    # and how to get an embedder (made on the first search that needs one; if
    # that fails, e.g. no key yet, it's tried again next time).
    def __init__(self, cache: ContentCache, store_path: Path | str | None = None,
                 embedder_factory: Callable[[], Embedder] = make_embedder, model_id: str = GEMINI_MODEL_ID):
        self._cache = cache
        self._store_path = Path(store_path) if store_path is not None else STORE_PATH
        self._embedder_factory = embedder_factory
        self._model_id = model_id
        self._embedder: Embedder | None = None
        self._queries: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()      # tools run in worker threads; one Gemini call at a time

    # The entry point. `course`: part of a course code ("IE2108", "2108"),
    # matched against the courses the cache and store know. `sources`: only
    # these kinds of hit (page, file, announcement, message, title).
    def search(self, query: str, course: str | None = None, sources: list[SearchSource] | None = None,
               limit: int = 10) -> SearchResults:
        if not 1 <= limit <= MAX_HITS:
            raise ValueError(f"limit must be between 1 and {MAX_HITS}.")
        notes: list[str] = []
        self.refresh_posts(notes)
        codes = self._known_codes()
        course_code = self._pick_code(course, codes) if course else None
        words = query_words(query)
        keyword, every_word = self._keyword_rows(words, course_code, sources, limit)
        if keyword and not every_word:
            notes.append("No result had all the words; these match some of them, most words first "
                         "(see matched_words).")
        meaning, reason = self._meaning_rows(query, course_code, sources, limit)
        picked = _varied(_merge_copies(_fuse(keyword, meaning)), limit)
        return SearchResults(query=query, search_mode="keyword_only" if reason else "hybrid", mode_reason=reason,
                             hits=self._hits(picked), coverage=self.coverage(codes), notes=notes)

    # ----- Posts from the event store -----

    # Copies announcements and messages from the event store into the search
    # table if the store's last sync times changed since the last copy. Opens
    # the store read-only. A missing or unreadable store becomes a note; the
    # search still runs over course content. Returns True if it copied.
    def refresh_posts(self, notes: list[str] | None = None) -> bool:
        notes = notes if notes is not None else []
        try:
            store = sqlite3.connect(f"file:{self._store_path.as_posix()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            notes.append("Announcements and messages aren't searchable: the event store couldn't be opened.")
            return False
        try:
            marks = dict(store.execute(
                f"SELECT key, value FROM state WHERE key IN ({','.join('?' * len(STORE_MARKER_KEYS))})",
                STORE_MARKER_KEYS).fetchall())
            marker = "|".join(marks.get(k, "") for k in STORE_MARKER_KEYS)
            if marker == self._cache.get_state("posts_marker"):
                return False
            rows = store.execute(
                "SELECT title, body, kind, course_code, course_site_id, event_id FROM events "
                "WHERE kind IN ('announcement', 'message') AND removed_at IS NULL").fetchall()
        except sqlite3.Error:
            notes.append("Announcements and messages aren't searchable: the event store couldn't be read "
                         "(run `uni-copilot sync` once).")
            return False
        finally:
            store.close()
        self._cache.replace_posts([(t or "", b or "", k, c or "", s or "", e) for t, b, k, c, s, e in rows], marker)
        return True

    # The links of some posts, read from the event store (read-only).
    def _post_urls(self, event_ids: list[str]) -> dict[str, str]:
        if not event_ids:
            return {}
        try:
            store = sqlite3.connect(f"file:{self._store_path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                rows = store.execute(f"SELECT event_id, url FROM events WHERE event_id IN "
                                     f"({','.join('?' * len(event_ids))})", event_ids).fetchall()
            finally:
                store.close()
        except sqlite3.Error:
            return {}
        return {e: u for e, u in rows if u}

    # ----- Querying -----

    # The keyword list: rows with all the words (every_word=True), or if
    # none, rows with any of them. Returns (rows best first, every_word).
    def _keyword_rows(self, words: list[str], course_code: str | None, sources: list[str] | None,
                      limit: int) -> tuple[list[dict], bool]:
        all_words, any_word = match_queries(words)
        rows = self._fts(all_words, words, course_code, sources, limit, every_word=True)
        if rows or len(words) == 1:
            return rows, True
        return self._fts(any_word, words, course_code, sources, limit, every_word=False), False

    # One FTS5 query: matching rows, best first, filtered by course and
    # source, as dicts with a snippet (matched words in **bold**) from the
    # text, or from the title when only the title matched, and matched_words.
    #   Order: rows with more of the words first (every_word=False only; with
    #   every_word=True all rows have them all), then BM25 with titles weighted.
    #   Which words a row has: one small lookup per word,
    #   "rowid IN (rows matching that word)", worked out once per query.
    #   Snippets: FTS5 marks matches with two invisible characters first
    #   (char 2 and 3), so we can tell whether the text or only the title
    #   matched, then they become ** (plain ASCII, fine on Telegram and in
    #   any console).
    def _fts(self, match: str, words: list[str], course_code: str | None, sources: list[str] | None,
             limit: int, every_word: bool) -> list[dict]:
        has_word = "" if every_word else "".join(
            f", rowid IN (SELECT rowid FROM search_text WHERE search_text MATCH ?) AS w{i}" for i in range(len(words)))
        sql = ("SELECT rowid, source, title, body, course_code, site_id, content_id, file_ref, page, event_id, "
               f"snippet(search_text, 1, char(2), char(3), '...', {SNIPPET_WORDS}) AS body_snip, "
               f"snippet(search_text, 0, char(2), char(3), '...', {SNIPPET_WORDS}) AS title_snip"
               f"{has_word} FROM search_text WHERE search_text MATCH ?"
               " AND NOT (source = 'file' AND (" + " OR ".join("lower(title) LIKE ?" for _ in DATA_EXTENSIONS) + "))")
        params: list = ([] if every_word else [_quoted(w) for w in words]) + [match]
        params += [f"%{ext}" for ext in sorted(DATA_EXTENSIONS)]
        if course_code:
            sql += " AND course_code = ?"
            params.append(course_code)
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params += list(sources)
        most_words = "" if every_word else "(" + " + ".join(f"w{i}" for i in range(len(words))) + ") DESC, "
        sql += f" ORDER BY {most_words}bm25(search_text, {TITLE_WEIGHT}, 1.0) LIMIT ?"
        params.append(limit * 8)   # more than needed: copies merge, and _varied mixes kinds
        with self._cache._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        found = []
        for r in rows:
            body_snip, title_snip = r["body_snip"] or "", r["title_snip"] or ""
            snippet = body_snip if MATCH_START in body_snip else title_snip
            found.append({**_plain(r), "snippet": snippet.replace(MATCH_START, "**").replace(MATCH_END, "**"),
                          "matched": words if every_word else [w for i, w in enumerate(words) if r[f"w{i}"]],
                          "score": None})
        return found

    # The meaning list: the chunks closest to the question, as rows of the
    # same search table (so they can be merged with the keyword list).
    # Returns (rows best first, None), or ([], reason) when meaning search
    # can't take part: the reason, in plain words, becomes mode_reason.
    def _meaning_rows(self, query: str, course_code: str | None, sources: list[str] | None,
                      limit: int) -> tuple[list[dict], str | None]:
        if sources and not MEANING_SOURCES & set(sources):
            return [], f"Meaning search covers course files and pages only. {KEYWORD_RESULTS}"
        done, _ = self._cache.embedding_counts(self._model_id)
        if done == 0:
            return [], ("Meaning search has nothing to compare with yet: the indexer hasn't embedded any "
                        f"course text. {KEYWORD_RESULTS}")
        try:
            question = self._question_vector(query)
        except EmbeddingUnavailable as e:
            return [], f"{e.reason} {KEYWORD_RESULTS}"
        hashes, matrix = self._cache.vector_matrix(self._model_id)
        if matrix.shape[0] == 0:
            return [], f"Meaning search has nothing to compare with yet. {KEYWORD_RESULTS}"
        if matrix.shape[1] != question.shape[0]:
            return [], f"The stored vectors don't match the model (re-embed with the indexer). {KEYWORD_RESULTS}"
        similarity = matrix @ question                     # one number per distinct text
        best = [i for i in np.argsort(-similarity)[:limit * 8] if similarity[i] >= MIN_SIMILARITY]
        places = self._cache.chunks_by_hash([hashes[i] for i in best])
        wanted = []                                        # (chunk, similarity), best first
        for i in best:
            for chunk in places.get(hashes[i], []):
                if course_code and chunk.course_code != course_code:
                    continue
                if sources and chunk.source not in sources:
                    continue
                wanted.append((chunk, float(similarity[i])))
        rows = self._rows_for_chunks([c for c, _ in wanted])
        found, seen = [], set()
        for chunk, score in wanted:
            row = rows.get((chunk.source, chunk.site_id, chunk.content_id, chunk.file_ref, chunk.page))
            if row is None or row["rowid"] in seen:        # a later part of a page already listed
                continue
            seen.add(row["rowid"])
            words = chunk.text.split()
            snippet = " ".join(words[:SNIPPET_WORDS * 2]) + (" ..." if len(words) > SNIPPET_WORDS * 2 else "")
            found.append({**row, "snippet": snippet, "matched": [], "score": round(score, 3)})
        return found, None

    # The question's vector: remembered for the last QUERY_MEMORY questions,
    # else 1 Gemini request (never waits for a limit: a search reports it).
    def _question_vector(self, query: str) -> np.ndarray:
        key = " ".join(query.lower().split())
        with self._lock:
            if key in self._queries:
                self._queries.move_to_end(key)
                return self._queries[key]
            if self._embedder is None:
                self._embedder = self._embedder_factory()
            vector = self._embedder.embed_query(query)
            self._queries[key] = vector
            if len(self._queries) > QUERY_MEMORY:
                self._queries.popitem(last=False)
            return vector

    # The search-table rows of some chunks' pages (one query): keyed by
    # (source, site, content id, file_ref, page) as chunks name them.
    def _rows_for_chunks(self, chunks: list) -> dict[tuple, dict]:
        ids = sorted({c.content_id for c in chunks})
        if not ids:
            return {}
        rows: dict[tuple, dict] = {}
        with self._cache._connect() as conn:
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                for r in conn.execute(
                        "SELECT rowid, source, title, body, course_code, site_id, content_id, file_ref, page, event_id "
                        f"FROM search_text WHERE source IN ('file', 'page') AND content_id IN "
                        f"({','.join('?' * len(part))})", part):
                    page = int(r["page"]) if r["page"] is not None else None
                    rows[(r["source"], r["site_id"], r["content_id"], r["file_ref"] or None, page)] = _plain(r)
        return rows

    # The picked rows as SearchHits: links, also_in, found_by, scores.
    def _hits(self, picked: list[tuple]) -> list[SearchHit]:
        urls = self._post_urls([r["event_id"] for r, _ in picked if r["event_id"]])
        hits = []
        for r, also_in in picked:
            others = list(dict.fromkeys(also_in))
            if len(others) > MAX_ALSO_IN:
                others = others[:MAX_ALSO_IN] + [f"and {len(others) - MAX_ALSO_IN} more"]
            if r["event_id"] and r["event_id"] in urls:
                url = urls[r["event_id"]]                  # a post: its own link
            elif r["site_id"]:
                url = course_url(r["site_id"])             # course content: the course page
            else:
                url = ""
            hits.append(SearchHit(
                source=r["source"], course_code=r["course_code"] or "", title=r["title"] or "",
                snippet=" ".join(r["snippet"].split()), content_id=r["content_id"], file_ref=r["file_ref"],
                page=int(r["page"]) if r["page"] is not None else None, event_id=r["event_id"],
                found_by=r["found_by"], matched_words=r["matched"], meaning_score=r["score"],
                also_in=others, url=url,
            ))
        return hits

    # ----- Courses and coverage -----

    # Every course code the cache or the event store knows (no NTULearn call).
    def _known_codes(self) -> set[str]:
        codes = set(self._cache.coverage_counts(INDEXED_EXTENSIONS)["codes"])
        try:
            store = sqlite3.connect(f"file:{self._store_path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                codes |= {c for (c,) in store.execute("SELECT DISTINCT course_code FROM events") if c}
            finally:
                store.close()
        except sqlite3.Error:
            pass
        return codes

    # The one course code that `course` names, by part of the code ("2108").
    # 0 or 2+ matches: a plain ValueError listing the codes.
    @staticmethod
    def _pick_code(course: str, codes: set[str]) -> str:
        wanted = course.strip().upper().replace(" ", "")
        found = sorted(c for c in codes if wanted in c.upper())
        if len(found) == 1:
            return found[0]
        known = ", ".join(sorted(codes)) or "none yet"
        if not found:
            raise ValueError(f"No course code contains '{course}'. Search knows: {known}.")
        raise ValueError(f"'{course}' matches several courses: {', '.join(found)}.")

    # How much the index covers, for every answer.
    def coverage(self, codes: set[str] | None = None) -> SearchCoverage:
        counts = self._cache.coverage_counts(INDEXED_EXTENSIONS)
        codes = codes if codes is not None else self._known_codes()
        return SearchCoverage(
            courses_indexed=len(counts["codes"]), courses_total=max(len(codes), len(counts["codes"])),
            pages=counts["pages"], files=counts["files"], files_known=counts["files_known"], posts=counts["posts"],
            **dict(zip(("chunks_with_meaning", "chunks_total"), self._cache.embedding_counts(self._model_id))),
            indexed_at=time_from_db(self._cache.get_state("indexer_finished_at")),
        )


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.search [--real "words"]
# Without --real: offline, a temporary cache and a temporary made-up event
# store. With --real "words": your real cache and store (read-only for the
# store), printing counts, sources, courses and pages only; never titles or text.
def _main() -> None:
    import tempfile
    from datetime import datetime, timezone

    from uni_copilot.ntulearn_mcp.models import ContentItem, Release

    ap = argparse.ArgumentParser(description="Self-test of search (keyword and meaning).")
    ap.add_argument("--real", metavar="WORDS", help="search your real cache for these words (counts only)")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")   # a Windows console may lack some characters
    if args.real:
        result = ContentSearch(ContentCache()).search(args.real, limit=10)
        c = result.coverage
        print(f"coverage: {c.courses_indexed} of {c.courses_total} courses with a cached tree, {c.pages} pages, "
              f"{c.files} of {c.files_known} known files read, {c.posts} posts, {c.chunks_with_meaning} of "
              f"{c.chunks_total} texts with meaning; notes: {result.notes or 'none'}")
        print(f"mode: {result.search_mode}; reason: {result.mode_reason or 'none'}")
        for h in result.hits:
            print(f"   {h.source:12} {h.course_code:7} page={h.page} found_by={h.found_by} matched={h.matched_words} "
                  f"meaning={h.meaning_score} copies={len(h.also_in)} snippet {len(h.snippet)} characters")
        return

    now = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)

    # Embedders for the offline test. _no_key: as if .env had no key.
    # _FakeEmbedder: a toy "meaning": words of one idea share a dimension
    # (rotation / rebalanced / height are one idea, avl / tree another), so
    # "AVL rotation" is close to a page that never says "rotation".
    def _no_key():
        raise EmbeddingUnavailable("no_key", "Meaning search isn't set up: no GEMINI_API_KEY in .env.")

    class _FakeEmbedder:
        model_id = "fake@8"
        IDEAS = {"rotation": 0, "rotations": 0, "rotate": 0, "rebalanced": 0, "height": 0, "balanced": 0,
                 "avl": 1, "tree": 1, "trees": 1, "subtrees": 1, "penguin": 2, "zoo": 2}

        def __init__(self, fail: EmbeddingUnavailable | None = None):
            self.fail, self.calls = fail, 0

        def _vec(self, text: str) -> np.ndarray:
            v = np.zeros(8, dtype=np.float32)
            for w in WORD_RE.findall(text.lower()):
                v[self.IDEAS.get(w, 3 + sum(map(ord, w)) % 5)] += 1
            return v / (np.linalg.norm(v) or 1)

        def embed_documents(self, chunks, wait=True, on_wait=None):
            return [self._vec(c.text) for c in chunks]

        def embed_query(self, text):
            self.calls += 1
            if self.fail:
                raise self.fail
            return self._vec(text)
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "store.sqlite"
        conn = sqlite3.connect(store)
        conn.executescript(
            "CREATE TABLE events (event_id TEXT, kind TEXT, course_code TEXT, course_site_id TEXT, title TEXT, "
            "body TEXT, url TEXT, removed_at TEXT); CREATE TABLE state (key TEXT, value TEXT);")
        conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("ntulearn_announcement:_1_1", "announcement", "XX1000", "_1_1", "Quiz 2 on Friday",
             "Bring a calculator. The quiz covers AVL tree rotations.", "https://example.org/a1", None),
            ("ntulearn_message:_2_1", "message", "YY2000", "_2_1", "Lab moved", "The lab is in LT2A.", None, None),
            ("ntulearn_announcement:_3_1", "announcement", "XX1000", "_1_1", "Old", "calculator", None, "2026-10-01"),
        ])
        conn.execute("INSERT INTO state VALUES ('last_sync:ntulearn_announcement', '2026-10-30T01:00')")
        conn.commit()
        conn.close()

        cache = ContentCache(Path(tmp) / "content.sqlite")
        item = ContentItem(id="_10_1", course_code="XX1000", course_site_id="_1_1", title="Week 8: AVL trees",
                           kind="file", release=Release(state="open"), file_name="avl.pdf")
        cache.save_tree("_1_1", "XX1000", [item], now)
        cache.save_file_text("_1_1", "XX1000", "_10_1", "_77_1", "avl.pdf", "pdf",
                             ["AVL trees", "A rotation keeps the tree balanced.", "Insertion and rotations"], None, now)
        search = ContentSearch(cache, store, embedder_factory=_no_key)

        r = search.search("AVL rotations")
        print(f"1) 'AVL rotations': {len(r.hits)} hits, mode {r.search_mode}")
        for h in r.hits:
            print(f"   {h.source:12} {h.course_code} {h.title!r:22} page={h.page} {h.snippet!r}")
        print("   coverage:", r.coverage.model_dump(exclude={"indexed_at", "indexed_sgt"}))
        print(f"2) 'calculator' (the removed post is left out): {[h.title for h in search.search('calculator').hits]}")
        print(f"3) only messages, 'lab': {[(h.source, h.title, h.url) for h in search.search('lab', sources=['message']).hits]}")
        print(f"4) course 'yy': {[h.course_code for h in search.search('lab', course='yy').hits]}")
        r = search.search("AVL penguin")
        print(f"5) 'AVL penguin' (no result has both): {len(r.hits)} hits; notes {r.notes}")
        print(f"6) FTS5 syntax in the words is harmless: {len(search.search('rotation\" OR * NEAR( -x:').hits)} hits")
        print(f"7) posts copied again without a store change: {search.refresh_posts()}")
        for bad in (("", None), ("avl", "zz"), ("avl", "0")):
            try:
                search.search(bad[0], course=bad[1])
            except ValueError as e:
                print(f"8) {bad!r}: {e}")

        # The step 13 fixes: most words first, copies merged, data files left out.
        zoo = ContentItem(id="_20_1", course_code="YY2000", course_site_id="_2_1", title="Zoo",
                          kind="file", release=Release(state="open"), file_name="zoo.pdf")
        copy = item.model_copy(update={"id": "_11_1", "file_name": "avl (copy).pdf", "title": "AVL again"})
        data = item.model_copy(update={"id": "_12_1", "file_name": "data.csv", "title": "Data"})
        cache.save_tree("_1_1", "XX1000", [item, copy, data], now)
        cache.save_tree("_2_1", "YY2000", [zoo], now)
        cache.save_file_text("_2_1", "YY2000", "_20_1", "_88_1", "zoo.pdf", "pdf", ["A penguin."], None, now)
        cache.save_file_text("_1_1", "XX1000", "_11_1", "_78_1", "avl (copy).pdf", "pdf",
                             ["AVL trees", "A rotation keeps the tree balanced.", "Insertion and rotations"], None, now)
        cache.save_file_text("_1_1", "XX1000", "_12_1", "_79_1", "data.csv", "text", ["balanced,1,2"], None, now)
        r = search.search("AVL trees penguin")
        print("9) 'AVL trees penguin' (no row has all 3; the rare 'penguin' row alone must not lead):")
        for h in r.hits:
            print(f"   {h.source:6} {h.course_code} page={h.page} matched={h.matched_words}")
        r = search.search("balanced")
        print(f"10) 'balanced': {[(h.title, h.page, h.also_in) for h in r.hits]} (one hit for the copy, no csv)")
        print(f"11) coverage leaves the csv out: files {r.coverage.files} of {r.coverage.files_known} known")
        print(f"12) words of 'What is a rotation?': {query_words('What is a rotation?')}; "
              f"of 'what is it': {query_words('what is it')}")

        # Step 16: meaning search next to the keywords.
        print(f"13) no vectors yet: mode {r.search_mode}; reason: {r.mode_reason}")
        balance = ContentItem(id="_30_1", course_code="XX1000", course_site_id="_1_1", title="Balancing",
                              kind="file", release=Release(state="open"), file_name="balance.pdf")
        cache.save_tree("_1_1", "XX1000", [item, copy, data, balance], now)
        cache.save_file_text("_1_1", "XX1000", "_30_1", "_90_1", "balance.pdf", "pdf",
                             ["AVL tree rebalanced by height after insertion"],
                             None, now)
        fake = _FakeEmbedder()
        todo = cache.pending_chunks(fake.model_id)
        cache.save_vectors(fake.model_id, [(c.text_hash, v) for c, v in zip(todo, fake.embed_documents(todo))], now)
        hybrid = ContentSearch(cache, store, embedder_factory=lambda: fake, model_id=fake.model_id)
        r = hybrid.search("AVL rotation")
        print(f"14) 'AVL rotation', {r.search_mode}, {r.coverage.chunks_with_meaning} of "
              f"{r.coverage.chunks_total} texts with meaning:")
        for h in r.hits:
            print(f"   {h.source:12} {h.title!r:16} page={h.page} found_by={h.found_by} "
                  f"matched={h.matched_words} meaning={h.meaning_score}")
        hybrid.search("AVL rotation")
        print(f"15) the same question again: Gemini asked {fake.calls} time(s) in all")
        r = hybrid.search("penguin rebalanced", course="yy")
        print(f"16) course 'yy' only: {[(h.course_code, h.found_by) for h in r.hits]}")
        r = hybrid.search("rotation", sources=["announcement"])
        print(f"17) announcements only: {r.search_mode}; {r.mode_reason}")
        for fail in (EmbeddingUnavailable("daily_limit", "Gemini's free daily limit is used up; it resets at "
                                          "midnight US Pacific time (Sat 15:00 SGT)."),
                     EmbeddingUnavailable("offline", "Gemini couldn't be reached (ConnectionError): no internet, "
                                          "or Google is down.")):
            broken = ContentSearch(cache, store, embedder_factory=lambda f=fail: _FakeEmbedder(f),
                                   model_id=fake.model_id)
            r = broken.search("AVL rotation")
            print(f"18) {fail.kind}: {r.search_mode}, {len(r.hits)} keyword hits; reason: {r.mode_reason}")
        r = ContentSearch(cache, store, embedder_factory=_no_key, model_id=fake.model_id).search("AVL rotation")
        print(f"19) no key: {r.search_mode}; reason: {r.mode_reason}")


if __name__ == "__main__":
    _main()
