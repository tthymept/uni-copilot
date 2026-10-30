"""
Keyword search over everything the content cache holds, plus your
announcements and messages. Never contacts NTULearn.

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
    (and the result says so).
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
  - search_mode: keyword_only until meaning search (embeddings) is built in
    steps 14-17; mode_reason says so.

Untrusted: titles and snippets are other people's text. Nothing here acts on them.
"""

import argparse
import re
import sys
import sqlite3
from pathlib import Path

from uni_copilot.ntulearn_mcp.cache import ContentCache, time_from_db
from uni_copilot.ntulearn_mcp.config import DATA_DIR
from uni_copilot.ntulearn_mcp.models import SearchCoverage, SearchHit, SearchResults, SearchSource, course_url
from uni_copilot.ntulearn_mcp.parsers.files import TEXT_EXTENSIONS

# The Phase 2 event store (written by `uni-copilot sync`; only read here).
STORE_PATH = DATA_DIR / "uni-copilot.sqlite"
# Its "last successful sync" notes for posts: when either changes, re-copy.
STORE_MARKER_KEYS = ("last_sync:ntulearn_announcement", "last_sync:ntulearn_message")
READABLE_EXTENSIONS = {".pdf", ".pptx", ".ipynb", *TEXT_EXTENSIONS}
MAX_HITS = 30
TITLE_WEIGHT = 5.0      # a match in a title counts this many times a match in the text
SNIPPET_WORDS = 14
WORD_RE = re.compile(r"\w+", re.UNICODE)
MATCH_START, MATCH_END = "\x02", "\x03"   # FTS5 marks matches with these first
NO_MEANING_YET = "Meaning search isn't built yet (Phase 4 steps 14-17); these are keyword results."


# Turns the user's words into two safe FTS5 queries: all words ("a" "b" =
# both must appear) and any word ("a" OR "b"). Each word is quoted, so
# nothing in it is read as FTS5 syntax. Raises ValueError if there are no words.
def match_queries(query: str) -> tuple[str, str]:
    words = [w for w in WORD_RE.findall(query.lower()) if len(w) > 1 or w.isdigit()]
    if not words:
        raise ValueError("Give some words to search for.")
    quoted = [f'"{w}"' for w in dict.fromkeys(words)]   # dict: drop repeats, keep order
    return " ".join(quoted), " OR ".join(quoted)


# Mixes kinds of hit: rows come best first; no single source (title, file,
# page, announcement, message) may take more than half of `limit`, unless
# nothing else matched. Without this, short title rows (weighted 5x) can
# crowd out the posts and pages that say something about the words.
def _varied(rows: list, limit: int) -> list:
    cap = max(1, (limit + 1) // 2)
    picked, held, per_source = [], [], {}
    for row in rows:
        if per_source.get(row["source"], 0) < cap:
            picked.append(row)
            per_source[row["source"]] = per_source.get(row["source"], 0) + 1
        else:
            held.append(row)
        if len(picked) == limit:
            return picked
    return picked + held[:limit - len(picked)]


class KeywordSearch:
    # Keeps the cache and where the event store is (tests pass temporary files).
    def __init__(self, cache: ContentCache, store_path: Path | str = STORE_PATH):
        self._cache = cache
        self._store_path = Path(store_path)

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
        all_words, any_word = match_queries(query)
        hits = self._run(all_words, course_code, sources, limit)
        if not hits:
            hits = self._run(any_word, course_code, sources, limit)
            if hits:
                notes.append("No result had all the words; these match some of them.")
        return SearchResults(query=query, search_mode="keyword_only", mode_reason=NO_MEANING_YET,
                             hits=hits, coverage=self.coverage(codes), notes=notes)

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

    # One FTS5 query: matching rows, best first (BM25 with titles weighted),
    # filtered by course and source, each turned into a SearchHit with a
    # snippet (matched words in **bold**) from the text, or from the title
    # when only the title matched. FTS5 marks matches with two invisible
    # characters first (char 2 and 3), so we can tell whether the text or
    # only the title matched, then they become ** (plain ASCII, fine on
    # Telegram and in any console).
    def _run(self, match: str, course_code: str | None, sources: list[str] | None, limit: int) -> list[SearchHit]:
        sql = ("SELECT source, title, course_code, site_id, content_id, file_ref, page, event_id, "
               f"snippet(search_text, 1, char(2), char(3), '...', {SNIPPET_WORDS}) AS body_snip, "
               f"snippet(search_text, 0, char(2), char(3), '...', {SNIPPET_WORDS}) AS title_snip "
               "FROM search_text WHERE search_text MATCH ?")
        params: list = [match]
        if course_code:
            sql += " AND course_code = ?"
            params.append(course_code)
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params += list(sources)
        sql += f" ORDER BY bm25(search_text, {TITLE_WEIGHT}, 1.0) LIMIT ?"
        params.append(limit * 4)   # more than needed, so _varied can mix kinds
        with self._cache._connect() as conn:
            rows = _varied(conn.execute(sql, params).fetchall(), limit)
        urls = self._post_urls([r["event_id"] for r in rows if r["event_id"]])
        hits = []
        for r in rows:
            body_snip, title_snip = r["body_snip"] or "", r["title_snip"] or ""
            snippet = body_snip if MATCH_START in body_snip else title_snip
            snippet = snippet.replace(MATCH_START, "**").replace(MATCH_END, "**")
            if r["event_id"] and r["event_id"] in urls:
                url = urls[r["event_id"]]                  # a post: its own link
            elif r["site_id"]:
                url = course_url(r["site_id"])             # course content: the course page
            else:
                url = ""
            hits.append(SearchHit(
                source=r["source"], course_code=r["course_code"] or "", title=r["title"] or "",
                snippet=" ".join(snippet.split()), content_id=r["content_id"], file_ref=r["file_ref"],
                page=int(r["page"]) if r["page"] is not None else None, event_id=r["event_id"],
                found_by=["keyword"], url=url,
            ))
        return hits

    # ----- Courses and coverage -----

    # Every course code the cache or the event store knows (no NTULearn call).
    def _known_codes(self) -> set[str]:
        codes = set(self._cache.coverage_counts(READABLE_EXTENSIONS)["codes"])
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
        counts = self._cache.coverage_counts(READABLE_EXTENSIONS)
        codes = codes if codes is not None else self._known_codes()
        return SearchCoverage(
            courses_indexed=len(counts["codes"]), courses_total=max(len(codes), len(counts["codes"])),
            pages=counts["pages"], files=counts["files"], files_known=counts["files_known"], posts=counts["posts"],
            chunks_with_meaning=0, chunks_total=0,
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

    ap = argparse.ArgumentParser(description="Self-test of keyword search.")
    ap.add_argument("--real", metavar="WORDS", help="search your real cache for these words (counts only)")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")   # a Windows console may lack some characters
    if args.real:
        result = KeywordSearch(ContentCache()).search(args.real, limit=10)
        c = result.coverage
        print(f"coverage: {c.courses_indexed} of {c.courses_total} courses with a cached tree, {c.pages} pages, "
              f"{c.files} of {c.files_known} known files read, {c.posts} posts; notes: {result.notes or 'none'}")
        for h in result.hits:
            print(f"   {h.source:12} {h.course_code:7} page={h.page} snippet {len(h.snippet)} characters")
        return

    now = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)
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
        search = KeywordSearch(cache, store)

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


if __name__ == "__main__":
    _main()
