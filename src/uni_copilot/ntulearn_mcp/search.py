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
from uni_copilot.ntulearn_mcp.config import DATA_DIR, DATA_EXTENSIONS
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
NO_MEANING_YET = "Meaning search isn't built yet (Phase 4 steps 14-17); these are keyword results."
# Question words and glue: they match almost every row, so they'd decide the
# "most words matched" order without saying anything about the topic.
STOP_WORDS = {
    "a", "an", "and", "are", "about", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "the", "this", "that", "to", "was", "what",
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
        words = query_words(query)
        all_words, any_word = match_queries(words)
        hits = self._run(all_words, words, course_code, sources, limit, every_word=True)
        if not hits and len(words) > 1:
            hits = self._run(any_word, words, course_code, sources, limit, every_word=False)
            if hits:
                notes.append("No result had all the words; these match some of them, most words first "
                             "(see matched_words).")
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

    # One FTS5 query: matching rows, best first, filtered by course and
    # source, copies merged, each turned into a SearchHit with a snippet
    # (matched words in **bold**) from the text, or from the title when only
    # the title matched.
    #   Order: rows with more of the words first (every_word=False only; with
    #   every_word=True all rows have them all), then BM25 with titles weighted.
    #   Which words a row has: one small lookup per word,
    #   "rowid IN (rows matching that word)", worked out once per query.
    #   Snippets: FTS5 marks matches with two invisible characters first
    #   (char 2 and 3), so we can tell whether the text or only the title
    #   matched, then they become ** (plain ASCII, fine on Telegram and in
    #   any console).
    def _run(self, match: str, words: list[str], course_code: str | None, sources: list[str] | None,
             limit: int, every_word: bool) -> list[SearchHit]:
        has_word = "" if every_word else "".join(
            f", rowid IN (SELECT rowid FROM search_text WHERE search_text MATCH ?) AS w{i}" for i in range(len(words)))
        sql = ("SELECT source, title, body, course_code, site_id, content_id, file_ref, page, event_id, "
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
            rows = _varied(_merge_copies(conn.execute(sql, params).fetchall()), limit)
        urls = self._post_urls([r["event_id"] for r, _ in rows if r["event_id"]])
        hits = []
        for r, also_in in rows:
            matched = words if every_word else [w for i, w in enumerate(words) if r[f"w{i}"]]
            others = list(dict.fromkeys(also_in))
            if len(others) > MAX_ALSO_IN:
                others = others[:MAX_ALSO_IN] + [f"and {len(others) - MAX_ALSO_IN} more"]
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
                found_by=["keyword"], matched_words=matched, also_in=others, url=url,
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
            print(f"   {h.source:12} {h.course_code:7} page={h.page} matched={h.matched_words} "
                  f"copies={len(h.also_in)} snippet {len(h.snippet)} characters")
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


if __name__ == "__main__":
    _main()
