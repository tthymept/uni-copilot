"""
Offline tests for cache.py: the content cache (trees, pages, file text),
its freshness, "file changed -> read again", forgetting what NTULearn removed,
the signed-link safety net, and the chunks and vectors of meaning search.

In plain words:
  Each test gets its own temporary cache file (pytest's tmp_path), filled with
  made-up items and text. Vectors are tiny made-up numbers, not real embeddings.
"""

import sqlite3
from datetime import timedelta

import numpy as np
import pytest

from fake_ntulearn import CODE, NOW, SITE
from uni_copilot.ntulearn_mcp.cache import ContentCache, is_fresh
from uni_copilot.ntulearn_mcp.models import ContentItem, Link, Release

V1 = NOW - timedelta(days=10)
MODEL = "fake@4"
LONG = " ".join(f"w{i}" for i in range(450))     # one page long enough to become 2 chunks


# A made-up content item of this kind.
def item(content_id: str, kind: str = "file", name: str | None = "a.pdf", title: str = "An item") -> ContentItem:
    return ContentItem(id=content_id, course_code=CODE, course_site_id=SITE, title=title, kind=kind,
                       release=Release(state="open"), file_name=name if kind == "file" else None)


@pytest.fixture
def cache(tmp_path):
    return ContentCache(tmp_path / "content.sqlite")


# A tree is fresh for 6 hours, then a tool fetches it again.
def test_tree_and_freshness(cache):
    cache.save_tree(SITE, CODE, [item("_10_1", "folder"), item("_13_1")], NOW)
    tree = cache.load_tree(SITE)
    assert [i.id for i in tree.items] == ["_10_1", "_13_1"]
    assert is_fresh(tree.fetched_at, NOW + timedelta(hours=1))
    assert not is_fresh(tree.fetched_at, NOW + timedelta(hours=7))


# File text is kept for the item's version; a newer `modified` means read it again.
def test_file_text_follows_the_version(cache):
    cache.save_file_text(SITE, CODE, "_13_1", "_77_1", "a.pdf", "pdf", ["one", "", "three"], V1, NOW)
    same = cache.load_file_text(SITE, "_13_1", "_77_1", V1)
    assert same.pages == ["one", "", "three"] and same.empty_pages == 1
    assert cache.load_file_text(SITE, "_13_1", "_77_1", V1 + timedelta(days=1)) is None


# An item that disappeared from NTULearn takes its page and file text (and
# search rows and chunks) with it.
def test_removed_item_is_forgotten(cache):
    cache.save_tree(SITE, CODE, [item("_13_1"), item("_20_1", "page", None)], NOW)
    cache.save_page(SITE, CODE, "_20_1", "Intro", "Welcome to the course, read the outline first.", [], V1, NOW)
    cache.save_file_text(SITE, CODE, "_13_1", "_77_1", "a.pdf", "pdf", ["Dijkstra uses a priority queue"], V1, NOW)
    cache.save_tree(SITE, CODE, [], NOW)
    stats = cache.stats()
    assert (stats.pages, stats.files, stats.file_pages, stats.chunks) == (0, 0, 0, 0)
    assert cache.load_page(SITE, "_20_1") is None


# The last safety net: a signed file link never lands in the file, whatever
# a caller passes in (page text, links, file pages).
def test_signed_links_never_stored(cache, tmp_path):
    signed = "https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1/xid-5_1?sig=SECRET"
    cache.save_page(SITE, CODE, "_20_1", "Intro", f"Pasted: {signed}",
                    [Link(text="raw", url=signed), Link(text="site", url="https://example.org")], V1, NOW)
    cache.save_file_text(SITE, CODE, "_13_1", "_77_1", "a.pdf", "pdf", [f"See {signed}"], V1, NOW)
    page = cache.load_page(SITE, "_20_1")
    assert [link.url for link in page.links] == ["https://example.org"]
    with sqlite3.connect(tmp_path / "content.sqlite") as conn:
        everything = " ".join(str(row) for table in ("pages", "file_pages", "search_text", "chunks")
                              for row in conn.execute(f"SELECT * FROM {table}"))
    assert "SECRET" not in everything


# ---------- Chunks and vectors ----------

# Two copies of one file make one set of distinct texts: embedded once.
# Short pages and data files get no chunks; a long page is cut in two.
def test_chunks_and_copies(cache):
    pages = ["AVL trees keep their height balanced", "Hi", LONG]
    cache.save_file_text(SITE, CODE, "_31_1", "_1", "avl.pdf", "pdf", pages, None, NOW)
    cache.save_file_text(SITE, CODE, "_32_1", "_2", "avl (copy).pdf", "pdf", pages, None, NOW)
    cache.save_file_text(SITE, CODE, "_33_1", "_3", "data.csv", "text", ["a,b,c,d,e,f,g,h"], None, NOW)
    stats = cache.stats()
    assert (stats.chunks, stats.distinct_texts) == (6, 3)
    todo = cache.pending_chunks(MODEL)
    assert [(c.page, c.part) for c in todo] == [(1, 1), (3, 1), (3, 2)]


# Vectors: saved per text, found by hash with every place, kept when the same
# text is saved again, and dropped by prune_vectors once no chunk uses them.
def test_vectors_saved_shared_and_pruned(cache):
    cache.save_tree(SITE, CODE, [item("_31_1"), item("_32_1")], NOW)
    pages = ["AVL trees keep their height balanced"]
    cache.save_file_text(SITE, CODE, "_31_1", "_1", "avl.pdf", "pdf", pages, None, NOW)
    cache.save_file_text(SITE, CODE, "_32_1", "_2", "copy.pdf", "pdf", pages, None, NOW)
    (chunk,) = cache.pending_chunks(MODEL)
    cache.save_vectors(MODEL, [(chunk.text_hash, np.array([1, 0, 0, 0], dtype=np.float32))], NOW)
    hashes, matrix = cache.vector_matrix(MODEL)
    assert hashes == [chunk.text_hash] and matrix.shape == (1, 4)
    assert {c.content_id for c in cache.chunks_by_hash(hashes)[chunk.text_hash]} == {"_31_1", "_32_1"}
    assert cache.embedding_counts(MODEL) == (1, 1)
    cache.save_file_text(SITE, CODE, "_31_1", "_1", "avl.pdf", "pdf", pages, None, NOW)
    assert cache.pending_chunks(MODEL) == []
    cache.save_tree(SITE, CODE, [item("_31_1")], NOW)            # the copy goes: the text stays
    assert cache.prune_vectors(MODEL) == 0
    cache.save_tree(SITE, CODE, [], NOW)                          # the original goes too
    assert cache.prune_vectors(MODEL) == 1
    assert cache.vector_matrix(MODEL)[1].shape == (0, 0)


# Vectors from another model can't be compared: prune drops them.
def test_other_model_vectors_dropped(cache):
    cache.save_file_text(SITE, CODE, "_31_1", "_1", "avl.pdf", "pdf", ["AVL trees keep their height balanced"],
                         None, NOW)
    (chunk,) = cache.pending_chunks("old@2")
    cache.save_vectors("old@2", [(chunk.text_hash, np.array([1, 0], dtype=np.float32))], NOW)
    assert cache.prune_vectors(MODEL) == 1
    assert len(cache.pending_chunks(MODEL)) == 1


# A cache from before step 15 (version 1, text but no chunks) is upgraded when
# opened, keeps its text, and fill_missing_chunks makes the chunks once.
def test_upgrade_from_version_1(tmp_path):
    path = tmp_path / "content.sqlite"
    old = ContentCache(path)
    old.save_page(SITE, CODE, "_20_1", "Intro", "Welcome to the course, read the outline first.", [], V1, NOW)
    with sqlite3.connect(path) as conn:
        conn.executescript("DROP TABLE chunks; DROP TABLE vectors; PRAGMA user_version = 1;")
    reopened = ContentCache(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert reopened.load_page(SITE, "_20_1") is not None
    assert reopened.fill_missing_chunks() == 1
    assert reopened.fill_missing_chunks() == 0
