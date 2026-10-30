"""
Offline tests for embeddings.py: chunking, and the Gemini embedder against a
fake Gemini (every limit and failure case answered with a plain reason, the
key never in a URL or a message), the pacer, and the free/paid tiers.

In plain words:
  FakeSession stands in for requests.Session: it records each POST and hands
  back made-up Gemini answers from a list. The clock and sleep are fakes too,
  so "waiting 40 s" takes no time. The key is a made-up string.
"""

import json

import numpy as np
import pytest
import requests

from uni_copilot.ntulearn_mcp.config import EMBED_DIMENSIONS, EMBED_LIMITS
from uni_copilot.ntulearn_mcp.embeddings import (
    EmbeddingUnavailable,
    GeminiEmbedder,
    _MinutePacer,
    file_chunks,
    page_chunks,
    split_words,
)

KEY = "FAKE-KEY-for-tests"


# Stands in for requests.Session: answers from a list of (status, json) or exceptions.
class FakeSession:
    def __init__(self, answers):
        self.headers: dict = {}
        self.answers = list(answers)
        self.sent: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.sent.append({"url": url, "body": json})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        r = requests.Response()
        r.status_code, r._content = status, _json(body)
        return r


def _json(body) -> bytes:
    return json.dumps(body).encode()


def vectors(n: int, size: int = EMBED_DIMENSIONS) -> tuple[int, dict]:
    return 200, {"embeddings": [{"values": [3.0] + [4.0] + [0.0] * (size - 2)} for _ in range(n)]}


def quota(quota_id: str, retry: str | None = None) -> tuple[int, dict]:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}


# A fake clock that moves only when something sleeps; sleeps are recorded.
@pytest.fixture
def clock():
    class Clock:
        now, sleeps = 1000.0, []

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(round(seconds))
            self.now += seconds
    return Clock()


def embedder(answers, clock, limits=None) -> tuple[GeminiEmbedder, FakeSession]:
    session = FakeSession(answers)
    return GeminiEmbedder(KEY, session, clock, clock.sleep, limits=limits), session


CHUNKS = file_chunks("XX1000", "_1_1", "_10_1", "_77_1", "avl.pdf", ["AVL trees keep the height balanced"])


# ---------- Chunks ----------

def test_split_words():
    assert split_words("Questions?") == []                      # too short to be worth a vector
    assert split_words("one two three four five") == ["one two three four five"]
    pieces = split_words(" ".join(f"w{i}" for i in range(700)))
    assert [len(p.split()) for p in pieces] == [300, 300, 200]
    assert pieces[1].split()[0] == "w250"                        # 50 words overlap


# One chunk per page or slide (so a hit names the slide); copies share a hash,
# whatever the file is called.
def test_chunks_keep_their_place_and_hash():
    chunks = file_chunks("XX1000", "_1_1", "_10_1", "_77_1", "a.pdf", ["first page has enough words", "", "x " * 400])
    assert [(c.page, c.part) for c in chunks] == [(1, 1), (3, 1), (3, 2)]
    copy = file_chunks("YY2000", "_2_1", "_20_1", "_88_1", "a (copy).pdf", ["first page has enough words"])
    assert copy[0].text_hash == chunks[0].text_hash
    page = page_chunks("XX1000", "_1_1", "_30_1", "Intro", "Welcome to the course, read the outline first.")
    assert page[0].source == "page" and page[0].page is None


# ---------- Gemini ----------

# A batch: the settings sent as top-level fields (Gemini ignores the newer
# config object), the key in a header only, vectors scaled to length 1.
def test_batch_request(clock):
    e, session = embedder([vectors(1)], clock)
    (v,) = e.embed_documents(CHUNKS)
    request = session.sent[0]["body"]["requests"][0]
    assert request["taskType"] == "RETRIEVAL_DOCUMENT" and request["outputDimensionality"] == EMBED_DIMENSIONS
    assert request["title"] == "avl.pdf"
    assert KEY not in session.sent[0]["url"] and session.headers["x-goog-api-key"] == KEY
    assert v.shape == (EMBED_DIMENSIONS,) and abs(float(np.linalg.norm(v)) - 1) < 1e-6


# A question is RETRIEVAL_QUERY, one text.
def test_query_request(clock):
    e, session = embedder([(200, {"embedding": {"values": [1.0] * EMBED_DIMENSIONS}})], clock)
    e.embed_query("avl rotation")
    assert session.sent[0]["url"].endswith(":embedContent")
    assert session.sent[0]["body"]["taskType"] == "RETRIEVAL_QUERY"


# Per-minute limit while indexing: waited out (Gemini's retry delay), then on.
def test_minute_limit_waited_out(clock):
    said = []
    e, _ = embedder([quota("EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier", "40s"), vectors(1)], clock)
    e.embed_documents(CHUNKS, on_wait=said.append)
    assert clock.sleeps == [40] and said == ["waiting 40 s for Gemini's per-minute limit"]


# Each failure gives its own kind and a plain reason, never the key.
@pytest.mark.parametrize("answers, kind, words", [
    ([quota("EmbedContentRequestsPerDayPerProjectPerModel-FreeTier")], "daily_limit", "midnight US Pacific"),
    ([(402, {"error": {"status": "RESOURCE_EXHAUSTED"}})], "no_credit", "prepaid credit"),
    ([(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Monthly spending cap exceeded."}})],
     "spend_cap", "spend cap"),
    ([(400, {"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}})],
     "bad_key", "rejected the key"),
    ([(403, {"error": {"status": "PERMISSION_DENIED"}})], "bad_key", "rejected the key"),
    ([requests.ConnectionError(f"https://x?key={KEY}")], "offline", "couldn't be reached"),
    ([(503, {}), (503, {}), (503, {})], "service", "503"),
    ([vectors(1, size=3072)], "service", "3072 numbers"),
])
def test_failures_have_plain_reasons(clock, answers, kind, words):
    e, _ = embedder(answers, clock)
    with pytest.raises(EmbeddingUnavailable) as caught:
        e.embed_documents(CHUNKS)
    assert caught.value.kind == kind and words in caught.value.reason
    assert KEY not in caught.value.reason and KEY not in str(caught.value)


# A search never waits for the per-minute limit: it reports it at once.
def test_query_does_not_wait(clock):
    e, _ = embedder([quota("EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier", "40s")], clock)
    with pytest.raises(EmbeddingUnavailable) as caught:
        e.embed_query("avl rotation")
    assert caught.value.kind == "minute_limit" and clock.sleeps == []


# ---------- Pacing and tiers ----------

# Gemini counts every text as a request, also inside a batch: a second batch
# of 50 in the same minute waits on the free tier.
def test_pacer_counts_texts(clock):
    pacer = _MinutePacer(80, 24_000, clock)
    pacer.record(500, texts=50)
    assert pacer.wait_for(500, texts=50) == 60
    assert pacer.wait_for(500, texts=30) == 0
    clock.now += 61
    assert pacer.wait_for(500, texts=50) == 0


# The paid tier's caps take thousands of texts a minute; the tier comes from .env.
def test_tiers(clock, monkeypatch):
    pacer = _MinutePacer(*EMBED_LIMITS["paid"][:2], clock)
    for _ in range(40):
        pacer.record(1_000, texts=50)
    assert pacer.wait_for(1_000, texts=50) == 0
    monkeypatch.setattr("uni_copilot.ntulearn_mcp.config.env_setting",
                        lambda name: "paid" if name == "UNI_COPILOT_GEMINI_TIER" else "")
    from uni_copilot.ntulearn_mcp.config import gemini_tier
    assert gemini_tier() == ("paid", EMBED_LIMITS["paid"])
    monkeypatch.setattr("uni_copilot.ntulearn_mcp.config.env_setting", lambda name: "fast please")
    assert gemini_tier()[0] == "free"                          # anything unknown is the safe, slower tier
