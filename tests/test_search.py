"""
Offline tests for search.py: keyword search (FTS5), the posts copied from the
event store, copies merged, data files left out, meaning search with a toy
embedder, the merge (reciprocal rank fusion), and every "keyword-only" reason.

In plain words:
  A temporary content cache gets a few made-up files, and a temporary event
  store a few made-up posts. The toy embedder gives a "meaning" where some
  words share one idea (rotation / rebalanced / height), so a page can be
  found by meaning without containing the word. No network, no Gemini.
"""

import sqlite3

import numpy as np
import pytest

from fake_ntulearn import CODE, NOW, SITE
from uni_copilot.ntulearn_mcp.cache import ContentCache
from uni_copilot.ntulearn_mcp.embeddings import EmbeddingUnavailable
from uni_copilot.ntulearn_mcp.search import WORD_RE, ContentSearch, match_queries, query_words

MODEL = "toy@8"


# A toy embedder: words of one idea share a dimension. Counts its question calls.
class ToyEmbedder:
    model_id = MODEL
    IDEAS = {"rotation": 0, "rotations": 0, "rebalanced": 0, "height": 0, "balanced": 0,
             "avl": 1, "tree": 1, "trees": 1, "subtrees": 1}

    def __init__(self, fail: EmbeddingUnavailable | None = None):
        self.fail, self.calls = fail, 0

    def vector(self, text: str) -> np.ndarray:
        v = np.zeros(8, dtype=np.float32)
        for w in WORD_RE.findall(text.lower()):
            v[self.IDEAS.get(w, 2 + sum(map(ord, w)) % 6)] += 1
        return v / (np.linalg.norm(v) or 1)

    def embed_documents(self, chunks, wait=True, on_wait=None):
        return [self.vector(c.text) for c in chunks]

    def embed_query(self, text: str) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.vector(text)


# A temporary event store with two posts (one removed) and its sync marker.
@pytest.fixture
def store(tmp_path):
    path = tmp_path / "store.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE events (event_id TEXT, kind TEXT, course_code TEXT, course_site_id TEXT, title TEXT, "
            "body TEXT, url TEXT, removed_at TEXT); CREATE TABLE state (key TEXT, value TEXT);")
        conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            ("ntulearn_announcement:_1_1", "announcement", CODE, SITE, "Quiz 2 on Friday",
             "Bring a calculator. The quiz covers AVL tree rotations.", "https://example.org/a1", None),
            ("ntulearn_announcement:_3_1", "announcement", CODE, SITE, "Old", "calculator", None, "2026-10-01"),
        ])
        conn.execute("INSERT INTO state VALUES ('last_sync:ntulearn_announcement', '2026-10-30T01:00')")
    return path


# A content cache with: an AVL file (and a copy), a page that never says
# "rotation", a file in another course, a csv, and every text embedded.
@pytest.fixture
def cache(tmp_path):
    cache = ContentCache(tmp_path / "content.sqlite")
    cache.save_tree("_2_1", "YY2000", [], NOW)       # search knows course codes from the trees (and posts)
    avl = ["AVL trees and their rotation after insertion", "Rotations keep the tree balanced and short"]
    cache.save_file_text(SITE, CODE, "_10_1", "_1", "avl.pdf", "pdf", avl, None, NOW)
    cache.save_file_text(SITE, CODE, "_11_1", "_2", "avl (copy).pdf", "pdf", avl, None, NOW)
    cache.save_file_text(SITE, CODE, "_12_1", "_3", "balance.pdf", "pdf",
                         ["AVL tree rebalanced by height after insertion"], None, NOW)
    cache.save_file_text("_2_1", "YY2000", "_20_1", "_4", "gears.pdf", "pdf",
                         ["The rotation of the gear wheel in a gearbox"], None, NOW)
    cache.save_file_text(SITE, CODE, "_13_1", "_5", "data.csv", "text", ["rotation,height,1,2,3,4"], None, NOW)
    toy = ToyEmbedder()
    todo = cache.pending_chunks(MODEL)
    cache.save_vectors(MODEL, [(c.text_hash, v) for c, v in zip(todo, toy.embed_documents(todo))], NOW)
    return cache


def make(cache, store, embedder=None) -> ContentSearch:
    embedder = embedder or ToyEmbedder()
    return ContentSearch(cache, store, embedder_factory=lambda: embedder, model_id=MODEL)


# ---------- Words ----------

# Words are quoted, so FTS5 syntax in a query can't break or change it;
# common words are left out unless nothing else is left.
def test_query_words():
    assert query_words("What is an AVL rotation?") == ["avl", "rotation"]
    assert query_words("what is it") == ["what", "is", "it"]
    assert match_queries(["avl", "rotation"]) == ('"avl" "rotation"', '"avl" OR "rotation"')
    with pytest.raises(ValueError):
        query_words("!!")


# ---------- Keyword search ----------

# The keyword part, without meaning search: copies merge, csv files never show,
# matched words are listed, posts come from the event store (removed ones not).
def test_keyword_search_alone(cache, store):
    search = make(cache, store, ToyEmbedder(EmbeddingUnavailable("off", "Meaning search is off.")))
    r = search.search("rotation")
    assert r.search_mode == "keyword_only" and "Meaning search is off." in r.mode_reason
    files = [h for h in r.hits if h.source == "file"]
    assert {h.title for h in files} == {"avl.pdf", "gears.pdf"}           # the copy merged, the csv left out
    assert next(h for h in files if h.title == "avl.pdf").also_in == [f"{CODE} avl (copy).pdf"]
    assert all("**" in h.snippet for h in files)
    assert [h.title for h in search.search("calculator").hits] == ["Quiz 2 on Friday"]
    assert search.search('rotation" OR * NEAR( -x:').hits                  # odd characters are harmless


# No row has every word: rows with MORE of the words come first, whatever
# BM25 would say about a rare word on its own.
def test_most_words_first(cache, store):
    search = make(cache, store, ToyEmbedder(EmbeddingUnavailable("off", "off")))
    r = search.search("AVL gearbox insertion")
    assert any("No result had all the words" in n for n in r.notes)
    counts = [len(h.matched_words) for h in r.hits]
    assert counts == sorted(counts, reverse=True) and counts[0] == 2


# A course filter by part of the code; unknown or ambiguous codes are plain errors.
def test_course_filter(cache, store):
    search = make(cache, store)
    assert {h.course_code for h in search.search("rotation", course="2000").hits} == {"YY2000"}
    with pytest.raises(ValueError, match="No course code"):
        search.search("rotation", course="ZZ")
    with pytest.raises(ValueError, match="several"):
        search.search("rotation", course="0")


# ---------- Meaning search and the merge ----------

# "AVL rotation": the AVL slides found both ways come first; balance.pdf is
# found by meaning only (it never says "rotation"); the question is embedded once.
def test_hybrid_search(cache, store):
    toy = ToyEmbedder()
    search = make(cache, store, toy)
    r = search.search("AVL rotation")
    assert r.search_mode == "hybrid" and r.mode_reason is None
    assert r.hits[0].title == "avl.pdf" and r.hits[0].found_by == ["keyword", "meaning"]
    balance = next(h for h in r.hits if h.title == "balance.pdf")
    assert balance.found_by == ["meaning"] and balance.matched_words == [] and balance.meaning_score >= 0.6
    assert r.coverage.chunks_with_meaning == r.coverage.chunks_total > 0
    search.search("AVL rotation")
    assert toy.calls == 1


# Every reason meaning search can't take part gives keyword results and a
# plain reason, never an error.
@pytest.mark.parametrize("fail", [
    EmbeddingUnavailable("daily_limit", "Gemini's free daily limit is used up; it resets at midnight US Pacific time."),
    EmbeddingUnavailable("minute_limit", "Gemini's per-minute limit is reached; try again in a minute.", 30),
    EmbeddingUnavailable("no_credit", "Gemini says this project has no prepaid credit (402)."),
    EmbeddingUnavailable("offline", "Gemini couldn't be reached (ConnectionError)."),
    EmbeddingUnavailable("bad_key", "Gemini rejected the key (403 PERMISSION_DENIED)."),
])
def test_meaning_unavailable_falls_back(cache, store, fail):
    r = make(cache, store, ToyEmbedder(fail)).search("AVL rotation")
    assert r.search_mode == "keyword_only"
    assert r.mode_reason == f"{fail.reason} These are keyword results."
    assert r.hits and all(h.found_by == ["keyword"] for h in r.hits)


# No key: the embedder can't even be made; same fallback.
def test_no_key(cache, store):
    def no_key():
        raise EmbeddingUnavailable("no_key", "Meaning search isn't set up: no GEMINI_API_KEY in .env.")
    r = ContentSearch(cache, store, embedder_factory=no_key, model_id=MODEL).search("rotation")
    assert r.search_mode == "keyword_only" and "no GEMINI_API_KEY" in r.mode_reason


# Nothing embedded yet: keyword-only, and Gemini isn't even asked.
def test_nothing_embedded_asks_nobody(tmp_path, store):
    cache = ContentCache(tmp_path / "empty.sqlite")
    cache.save_file_text(SITE, CODE, "_10_1", "_1", "avl.pdf", "pdf", ["AVL trees and their rotation"], None, NOW)
    toy = ToyEmbedder()
    r = make(cache, store, toy).search("rotation")
    assert r.search_mode == "keyword_only" and "nothing to compare" in r.mode_reason
    assert toy.calls == 0


# Only posts asked for: meaning search covers files and pages, so it says so.
def test_posts_only_are_keyword_only(cache, store):
    toy = ToyEmbedder()
    r = make(cache, store, toy).search("calculator", sources=["announcement"])
    assert r.search_mode == "keyword_only" and "files and pages only" in r.mode_reason
    assert toy.calls == 0
