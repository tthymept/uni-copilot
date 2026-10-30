"""
Offline tests for indexer.py: reading every course's files (phase 1) and
embedding the new text (phase 2), with the made-up course of fake_ntulearn.py
and a fake Gemini.

In plain words:
  The checks follow the indexer's promises: a second run reads nothing it
  already has; a removed file is skipped and the run goes on; Gemini's daily
  limit stops embedding cleanly with what's left and how to carry on;
  --embed-only makes no NTULearn request at all; Ctrl+C keeps what's done.
"""

import io

import numpy as np
import pytest

from fake_ntulearn import NOW, FakeContentHttp, FakeCourses
from uni_copilot.ntulearn_mcp import indexer as indexer_mod
from uni_copilot.ntulearn_mcp import search as search_mod
from uni_copilot.ntulearn_mcp.cache import ContentCache
from uni_copilot.ntulearn_mcp.client.contents import ContentsClient
from uni_copilot.ntulearn_mcp.embeddings import EmbeddingUnavailable
from uni_copilot.ntulearn_mcp.indexer import Indexer

MODEL = "fake@4"
DAILY = EmbeddingUnavailable("daily_limit", "Gemini's free daily limit is used up; it resets at midnight "
                                            "US Pacific time (Sat 15:00 SGT).")


# A fake Gemini: counts batches; can fail after some, or be "interrupted" (Ctrl+C).
class FakeGemini:
    model_id = MODEL

    def __init__(self, fail_after: int | None = None, fail: Exception | None = None):
        self.batches, self.fail_after, self.fail = 0, fail_after, fail

    def embed_documents(self, chunks, wait=True, on_wait=None):
        if self.fail_after is not None and self.batches >= self.fail_after:
            raise self.fail
        self.batches += 1
        return [np.array([1, 0, 0, 0], dtype=np.float32) for _ in chunks]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "EMBED_BATCH_SIZE", 2)      # a few texts make several batches
    monkeypatch.setattr(search_mod, "STORE_PATH", tmp_path / "no-store.sqlite")
    http = FakeContentHttp()
    cache = ContentCache(tmp_path / "content.sqlite")
    contents = ContentsClient(http, FakeCourses(), cache, now=lambda: NOW)
    # More text to embed: a made-up file of 5 pages (no NTULearn item behind it).
    cache.save_file_text("_1_1", "XX1000", "_900_1", "_1", "extra.pdf", "pdf",
                         [f"page {i} talks about topic number {i} in some detail" for i in range(5)], None, NOW)

    def run(gemini=None, **kw):
        out = io.StringIO()
        factory = gemini if callable(gemini) and not isinstance(gemini, FakeGemini) else (lambda: gemini)
        report = Indexer(contents, FakeCourses(), cache, out=out, embedder_factory=factory,
                         model_id=MODEL).run(**kw)
        return report, out.getvalue()
    return http, cache, run


# Reading: the first run reads both files; the second reads nothing and asks
# NTULearn only for the tree (the "already read?" check is local).
def test_reads_once(setup):
    http, cache, run = setup
    report, _ = run(embed=False)
    assert report.read == 2 and not report.failed and cache.get_state("indexer_finished_at")
    before = len(http.calls)
    report, _ = run(embed=False)
    assert report.read == 0 and report.already_read == 2
    assert all(c.endswith("/contents") for c in http.calls[before:])


# A file removed from NTULearn is skipped with a plain reason; the run goes on.
def test_removed_file_skipped(setup):
    http, _, run = setup
    http.missing = True
    report, out = run(embed=False)
    assert report.read == 0 and len(report.failed) == 2 and "404" in out and report.stopped is None


# Dry run: counts and estimates only; nothing read, nothing sent.
def test_dry_run_sends_nothing(setup):
    http, cache, run = setup
    gemini = FakeGemini()
    report, out = run(gemini, dry_run=True)
    assert not any(c.startswith("FILE:") for c in http.calls) and gemini.batches == 0
    assert "to embed" in out and cache.stats().vectors == 0


# The daily limit: embedding stops cleanly, keeps what's done, says how to
# carry on, and isn't an error; --embed-only makes no NTULearn request.
def test_daily_limit_and_carry_on(setup):
    http, cache, run = setup
    before = len(http.calls)
    report, out = run(FakeGemini(fail_after=2, fail=DAILY), embed_only=True)
    assert len(http.calls) == before                              # no NTULearn at all
    assert report.embedded == 4 and report.stopped is None
    assert "resets at midnight US Pacific" in out and "--embed-only" in out
    assert cache.embedding_counts(MODEL)[0] == 4
    report, _ = run(FakeGemini(), embed_only=True)
    assert report.embedded == 1 and cache.embedding_counts(MODEL) == (5, 5)
    report, _ = run(FakeGemini(), embed_only=True)
    assert report.embed_todo == 0


# No key: files are still read; embedding is skipped with the reason.
def test_no_key_skips_embedding(setup):
    _, _, run = setup

    def no_key():
        raise EmbeddingUnavailable("no_key", "Meaning search isn't set up: no GEMINI_API_KEY in .env.")
    report, out = run(no_key)
    assert report.read == 2 and "Embedding skipped: Meaning search isn't set up" in out


# --max-tokens stops before passing the cap; Ctrl+C keeps every saved batch.
def test_max_tokens_and_ctrl_c(setup):
    _, cache, run = setup
    report, _ = run(FakeGemini(), embed_only=True, max_tokens=30)
    assert report.embedded == 2 and "max-tokens" in report.embed_stopped
    report, _ = run(FakeGemini(fail_after=1, fail=KeyboardInterrupt()), embed_only=True)
    assert report.stopped == "stopped by you (Ctrl+C)" and report.embedded == 2
    assert cache.embedding_counts(MODEL)[0] == 4
