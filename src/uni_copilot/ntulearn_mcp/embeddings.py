"""
Embeddings: turning text into vectors (lists of numbers) for search by meaning.

In plain words:
  An embedding model reads a piece of text and returns a vector (here 768
  numbers) that stands for what it means. Texts about the same thing get
  vectors that point the same way, even with different words: "AVL rotation"
  lands near the IE2108 slides about rebalancing a tree by height, though
  they never say "rotation". Search (step 16) compares your question's
  vector with every chunk's vector.

This file does two things:
  1. Chunks: cuts a file or page into pieces of about CHUNK_WORDS words, one
     vector each, keeping where each piece is (course, file, page/slide).
     A slide usually is one chunk; a long page becomes overlapping pieces.
  2. Embedders: one small interface (embed_documents, embed_query) with
     Google's Gemini behind it (gemini-embedding-001). Another service or a
     local model can be swapped in later behind the same interface; a model
     change means embedding everything once more (the vectors aren't comparable).

Gemini, free tier (your choice, 30 Oct):
  - The key comes from .env (GEMINI_API_KEY) and goes in a request header
    (x-goog-api-key), never in a URL, a log line or an error message.
  - Settings (task type, vector size, title) go as top-level fields of each
    request: Gemini ignored the newer embedContentConfig object (30 Oct), and
    every answer's vector size is checked.
  - Paced under the per-minute limits (80% of them): requests and estimated
    tokens over the last 60 s are counted, and a call waits when needed.
  - Limits give a plain answer, never a bare failure (your rule):
      per-minute limit (429 with a retry delay): wait it out and carry on
        (indexing), or report it at once (a search shouldn't hang);
      daily limit: stop with "resets at midnight US Pacific time (HH:MM SGT)";
      no key, a rejected key, Gemini down, no internet: stop with what to do.
    All of these are EmbeddingUnavailable(kind, reason); search turns the
    reason into keyword-only results, the indexer into a clean stop.
  - What's sent: course text only (slides, pages, titles) and search words.
    Never announcements, messages, grades or anything about you. Google may
    use free-tier text to improve its products, and people may review it
    (the Gemini API terms, read 30 Oct; accepted for course content).

Not NTULearn: this talks to Google with plain `requests`, not client/http.py
(which only ever talks to NTULearn).
"""

import argparse
import hashlib
import logging
import re
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

import numpy as np
import requests

from uni_copilot.ntulearn_mcp.config import (
    CHUNK_OVERLAP_WORDS,
    CHUNK_WORDS,
    EMBED_BATCH_SIZE,
    EMBED_DIMENSIONS,
    EMBED_MAX_REQUESTS_PER_MIN,
    EMBED_MAX_TOKENS_PER_MIN,
    EMBED_MODEL,
    GEMINI_API_KEY_NAME,
    GEMINI_BASE_URL,
    env_setting,
    load_settings,
)
from uni_copilot.ntulearn_mcp.models import SGT

log = logging.getLogger(__name__)

MIN_CHUNK_WORDS = 5          # shorter pieces ("Questions?", a title slide) aren't worth a vector
TIMEOUT_S = 60
MAX_MINUTE_WAITS = 3         # per-minute 429s waited out in a row before giving up
MAX_WAIT_S = 120             # never wait longer than this for one retry delay
MAX_SERVER_RETRIES = 2       # Gemini 5xx retried this many times (with a pause) before giving up

UnavailableKind = Literal["off", "no_key", "bad_key", "minute_limit", "daily_limit", "offline", "service"]


# ---------- 1. Chunks ----------

# One piece of text that gets one vector, and where it came from. `title`
# (the file or page title) goes to the model with the text: it helps it
# know what the piece is about. UNTRUSTED: other people's text.
@dataclass(frozen=True)
class Chunk:
    source: Literal["file", "page"]
    course_code: str
    site_id: str
    content_id: str
    file_ref: str | None
    page: int | None             # page / slide number for files, None for course pages
    part: int                    # 1, 2, ... when a long page is cut into several pieces
    title: str
    text: str

    # The text's fingerprint: the same text gets the same hash, so it's
    # embedded once and its vector shared (step 15). The title is left out:
    # copies of a file often have different names ("avl (1).pdf"); the
    # vector is made with the first copy's title.
    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


WORD_SPLIT = re.compile(r"\S+")


# Cuts one page's text into pieces of about `size` words, each overlapping
# the previous by `overlap` words (so a sentence cut in two is whole in one
# of them). A page of up to size words stays one piece. Pieces under
# MIN_CHUNK_WORDS words are dropped. Returns the pieces' texts.
def split_words(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> list[str]:
    words = WORD_SPLIT.findall(text)
    if len(words) < MIN_CHUNK_WORDS:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    step = max(1, size - overlap)
    pieces = []
    for start in range(0, len(words), step):
        piece = words[start:start + size]
        if len(piece) >= MIN_CHUNK_WORDS:
            pieces.append(" ".join(piece))
        if start + size >= len(words):
            break
    return pieces


# The chunks of one file: each page or slide on its own (so a hit can say
# "slide 14"), long pages cut into pieces.
def file_chunks(course_code: str, site_id: str, content_id: str, file_ref: str, file_name: str,
                pages: list[str]) -> list[Chunk]:
    return [Chunk("file", course_code, site_id, content_id, file_ref, number, part, file_name, piece)
            for number, text in enumerate(pages, 1)
            for part, piece in enumerate(split_words(text), 1)]


# The chunks of one course page (an Ultra Document's text).
def page_chunks(course_code: str, site_id: str, content_id: str, title: str, text: str) -> list[Chunk]:
    return [Chunk("page", course_code, site_id, content_id, None, None, part, title, piece)
            for part, piece in enumerate(split_words(text), 1)]


# ---------- 2. Embedders ----------

# Why meaning search can't run right now, in plain words for the user.
# kind says which case (search and the indexer act on it); retry_after_s is
# set for the per-minute limit. The message never contains the key.
class EmbeddingUnavailable(Exception):
    def __init__(self, kind: UnavailableKind, reason: str, retry_after_s: float | None = None):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.retry_after_s = retry_after_s


# What every embedding service offers. `model_id` names the model and vector
# size ("gemini-embedding-001@768"): stored with each vector, so a change is noticed.
class Embedder(Protocol):
    model_id: str

    def embed_documents(self, chunks: list[Chunk], wait: bool = True,
                        on_wait: Callable[[str], None] | None = None) -> list[np.ndarray]: ...

    def embed_query(self, text: str) -> np.ndarray: ...


# A rough token count (Gemini counts about 4 characters per token in English).
# Only used to stay under the tokens-per-minute limit, so rough is fine.
def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


# Scales a vector to length 1. Google asks for this when vectors are shorter
# than the model's full 3072; it also makes cosine similarity a plain dot product.
def normalized(values: list[float]) -> np.ndarray:
    v = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


# When Gemini's daily limit resets, in SGT words: midnight US Pacific time
# (15:00 or 16:00 SGT, depending on US daylight saving).
def daily_reset_text(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
        midnight = (pacific + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return f"at midnight US Pacific time ({midnight.astimezone(SGT).strftime('%a %H:%M')} SGT)"
    except Exception:   # no time zone data on this PC
        return "at midnight US Pacific time (about 15:00-16:00 SGT)"


# Counts what was sent in the last 60 s and says how long to wait before
# the next call so both per-minute limits stay under our 80% caps.
class _MinutePacer:
    def __init__(self, max_requests: int, max_tokens: int, clock: Callable[[], float]):
        self._max_requests, self._max_tokens, self._clock = max_requests, max_tokens, clock
        self._sent: deque[tuple[float, int]] = deque()     # (time, tokens) per request

    # Seconds to wait before sending a request of `tokens` tokens (0 = now).
    def wait_for(self, tokens: int) -> float:
        now = self._clock()
        while self._sent and now - self._sent[0][0] >= 60:
            self._sent.popleft()
        used = sum(t for _, t in self._sent)
        if len(self._sent) < self._max_requests and used + tokens <= self._max_tokens:
            return 0.0
        if not self._sent:            # one request bigger than the whole cap: send it alone
            return 0.0
        # Wait until enough of the oldest requests are a minute old: after the
        # i-th oldest drops out, are both counts under the caps?
        left_tokens, wait = used, 0.0
        for i, (at, t) in enumerate(self._sent, 1):
            left_tokens -= t
            wait = 60 - (now - at)
            if len(self._sent) - i < self._max_requests and left_tokens + tokens <= self._max_tokens:
                break
        return max(0.0, wait)

    def record(self, tokens: int) -> None:
        self._sent.append((self._clock(), tokens))


# Reads Gemini's error answer: (HTTP status, status word like
# RESOURCE_EXHAUSTED, quota ids like "...PerDay...", retry delay in seconds).
# Only these parts are kept; the message text isn't shown to anyone.
def _read_error(response: requests.Response) -> tuple[int, str, list[str], float | None]:
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    quota_ids, retry = [], None
    for detail in error.get("details") or []:
        for v in detail.get("violations") or []:
            quota_ids.append(str(v.get("quotaId") or v.get("quotaMetric") or ""))
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                retry = float(delay[:-1])
            except ValueError:
                pass
        reason = detail.get("reason")
        if reason:
            quota_ids.append(str(reason))
    return response.status_code, str(error.get("status") or ""), quota_ids, retry


class GeminiEmbedder:
    # `api_key` from make_embedder (never printed). `session`, `clock` and
    # `sleep` are swapped for fakes in the self-test.
    def __init__(self, api_key: str, session: requests.Session | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._session = session or requests.Session()
        self._session.headers.update({"x-goog-api-key": api_key, "Content-Type": "application/json"})
        self._clock, self._sleep = clock, sleep
        self._pacer = _MinutePacer(EMBED_MAX_REQUESTS_PER_MIN, EMBED_MAX_TOKENS_PER_MIN, clock)
        self.model_id = f"{EMBED_MODEL}@{EMBED_DIMENSIONS}"

    # Vectors for chunks (as RETRIEVAL_DOCUMENT, with their titles), in
    # batches of EMBED_BATCH_SIZE, in the same order. wait=True (the indexer)
    # waits out per-minute limits, telling on_wait ("waiting 40 s for ...");
    # wait=False raises at once. Raises EmbeddingUnavailable when it can't go on.
    def embed_documents(self, chunks: list[Chunk], wait: bool = True,
                        on_wait: Callable[[str], None] | None = None) -> list[np.ndarray]:
        vectors: list[np.ndarray] = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start:start + EMBED_BATCH_SIZE]
            requests_ = [self._request(c.text, "RETRIEVAL_DOCUMENT", c.title) for c in batch]
            tokens = sum(estimate_tokens(c.title + " " + c.text) for c in batch)
            answer = self._post("batchEmbedContents", {"requests": requests_}, tokens, wait, on_wait)
            got = answer.get("embeddings") or []
            if len(got) != len(batch):
                raise EmbeddingUnavailable("service", f"Gemini answered {len(got)} vectors for {len(batch)} texts.")
            vectors += [self._vector(e.get("values") or []) for e in got]
        return vectors

    # The vector of a search question (as RETRIEVAL_QUERY). Never waits: a
    # search reports a limit at once and falls back to keywords.
    def embed_query(self, text: str) -> np.ndarray:
        answer = self._post("embedContent", self._request(text, "RETRIEVAL_QUERY", None),
                            estimate_tokens(text), wait=False, on_wait=None)
        return self._vector((answer.get("embedding") or {}).get("values") or [])

    # One text as Gemini wants it: the text, plus its settings (task type,
    # vector size, title for documents) as top-level fields. The docs call
    # these deprecated in favour of an embedContentConfig object, but Gemini
    # silently ignored that object (your --check, 30 Oct: 3072 numbers, not
    # 768), while these fields work. _vector() checks the size every time.
    @staticmethod
    def _request(text: str, task: str, title: str | None) -> dict:
        request: dict = {"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": text}]},
                         "taskType": task, "outputDimensionality": EMBED_DIMENSIONS}
        if title:
            request["title"] = title
        return request

    # One vector from Gemini's answer, scaled to length 1. A wrong size means
    # Gemini ignored our settings: stop rather than store vectors that can't
    # be compared with the others.
    @staticmethod
    def _vector(values: list[float]) -> np.ndarray:
        if len(values) != EMBED_DIMENSIONS:
            raise EmbeddingUnavailable(
                "service", f"Gemini returned vectors of {len(values)} numbers instead of {EMBED_DIMENSIONS}: "
                           "it ignored the requested size (a change on Google's side?).")
        return normalized(values)

    # One call to Gemini, paced, with the limit cases told apart. Returns the
    # JSON answer or raises EmbeddingUnavailable.
    def _post(self, method: str, body: dict, tokens: int, wait: bool,
              on_wait: Callable[[str], None] | None) -> dict:
        url = f"{GEMINI_BASE_URL}/models/{EMBED_MODEL}:{method}"
        minute_waits = server_retries = 0
        while True:
            pause = self._pacer.wait_for(tokens)
            if pause > 0:
                if not wait:
                    raise EmbeddingUnavailable(
                        "minute_limit", "Meaning search is busy for a moment (Gemini's per-minute limit).", pause)
                self._say(on_wait, f"waiting {pause:.0f} s to stay under Gemini's per-minute limit")
                self._sleep(pause)
            self._pacer.record(tokens)
            try:
                response = self._session.post(url, json=body, timeout=TIMEOUT_S)
            except requests.RequestException as e:
                raise EmbeddingUnavailable(
                    "offline", f"Gemini couldn't be reached ({type(e).__name__}): no internet, or Google is down."
                ) from None
            if response.ok:
                try:
                    return response.json()
                except ValueError:
                    raise EmbeddingUnavailable("service", "Gemini's answer wasn't readable.") from None
            status, word, quota_ids, retry = _read_error(response)
            joined = " ".join(quota_ids).lower()
            if status == 429 and "perday" in joined:
                raise EmbeddingUnavailable(
                    "daily_limit", f"Gemini's free daily limit is used up; it resets {daily_reset_text()}.")
            if status == 429:
                delay = min(retry if retry is not None else 60.0, MAX_WAIT_S)
                if not wait or minute_waits >= MAX_MINUTE_WAITS:
                    raise EmbeddingUnavailable(
                        "minute_limit", "Gemini's per-minute limit is reached; try again in a minute.", delay)
                minute_waits += 1
                self._say(on_wait, f"waiting {delay:.0f} s for Gemini's per-minute limit")
                self._sleep(delay)
                continue
            if status in (401, 403) or "api_key" in joined:
                raise EmbeddingUnavailable(
                    "bad_key", f"Gemini rejected the key ({status} {word}): check {GEMINI_API_KEY_NAME} in .env, "
                               "that it's restricted to the Gemini API, and that its project still exists.")
            if status >= 500 and server_retries < MAX_SERVER_RETRIES:
                server_retries += 1
                self._sleep(5 * server_retries)
                continue
            raise EmbeddingUnavailable("service", f"Gemini answered {status} {word}.".replace(" .", "."))

    @staticmethod
    def _say(on_wait: Callable[[str], None] | None, text: str) -> None:
        log.info(text)
        if on_wait:
            on_wait(text)


# The embedder the settings ask for. Raises EmbeddingUnavailable("off" /
# "no_key") with a plain reason when meaning search can't be used at all.
def make_embedder() -> Embedder:
    choice = load_settings().embeddings
    if choice != "gemini":
        raise EmbeddingUnavailable("off", f"Meaning search is off (UNI_COPILOT_EMBEDDINGS={choice}).")
    key = env_setting(GEMINI_API_KEY_NAME)
    if not key:
        raise EmbeddingUnavailable("no_key", f"Meaning search isn't set up: no {GEMINI_API_KEY_NAME} in .env.")
    return GeminiEmbedder(key)


# ---------- Self-test ----------

# A stand-in for requests.Session: answers from a list of (status, json)
# and records what was sent. The real key never gets here.
class _FakeSession:
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
        r.status_code = status
        r._content = __import__("json").dumps(body).encode()
        return r


def _vectors(n: int) -> dict:
    return {"embeddings": [{"values": [1.0] + [0.0] * (EMBED_DIMENSIONS - 1)} for _ in range(n)]}


def _offline() -> None:
    print("1) chunks:")
    print(f"   short page: {split_words('Questions?')}; 10 words: {len(split_words('word ' * 10))} piece")
    pieces = split_words(" ".join(f"w{i}" for i in range(700)))
    print(f"   700 words: {len(pieces)} pieces of {[len(p.split()) for p in pieces]} words, "
          f"overlap ok: {pieces[1].split()[0] == 'w250'}")
    chunks = file_chunks("XX1000", "_1_1", "_10_1", "_77_1", "avl.pdf",
                         ["AVL trees keep the height balanced after insertion", "", "Hi", " ".join(["x"] * 400)])
    print(f"   file of 4 pages -> {[(c.page, c.part) for c in chunks]}; copy has same hash: "
          f"{chunks[0].text_hash == file_chunks('YY', '_2_1', '_9_1', 'r', 'avl.pdf', [chunks[0].text])[0].text_hash}")

    t = [0.0]
    clock, sleeps = (lambda: t[0]), []
    def sleep(s):
        sleeps.append(round(s))
        t[0] += s

    s = _FakeSession([(200, _vectors(2))])
    e = GeminiEmbedder("SECRET-KEY", s, clock, sleep)
    v = e.embed_documents(chunks[:2])
    body = s.sent[0]["body"]["requests"][0]
    print(f"2) one batch: {len(v)} vectors of {v[0].shape[0]}, length {np.linalg.norm(v[0]):.2f}; "
          f"sent {sorted(k for k in body if k != 'content')}, size {body['outputDimensionality']}; "
          f"key in URL: {'SECRET' in s.sent[0]['url']}")

    many = [chunks[0]] * 120
    s = _FakeSession([(200, _vectors(50)), (200, _vectors(50)), (200, _vectors(20))])
    print(f"3) 120 chunks -> {len(GeminiEmbedder('k', s, clock, sleep).embed_documents(many))} vectors "
          f"in {len(s.sent)} requests")

    minute = (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40s"}]}})
    daily = (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})
    said: list[str] = []
    sleeps.clear()
    s = _FakeSession([minute, (200, _vectors(1))])
    GeminiEmbedder("k", s, clock, sleep).embed_documents(chunks[:1], on_wait=said.append)
    print(f"4) per-minute 429 while indexing: waited {sleeps}, said {said}, then carried on")

    cases = [
        ("per-minute 429 in a search", [minute], "query"),
        ("daily 429", [daily], "docs"),
        ("rejected key", [(400, {"error": {"status": "INVALID_ARGUMENT", "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}})], "docs"),
        ("403", [(403, {"error": {"status": "PERMISSION_DENIED"}})], "docs"),
        ("no internet", [requests.ConnectionError("SECRET-KEY in a message")], "docs"),
        ("Gemini down", [(503, {}), (503, {}), (503, {})], "docs"),
        ("wrong vector size", [(200, {"embeddings": [{"values": [0.1] * 3072}]})], "docs"),
    ]
    for name, answers, call in cases:
        e = GeminiEmbedder("SECRET-KEY", _FakeSession(answers), clock, sleep)
        try:
            e.embed_query("avl rotation") if call == "query" else e.embed_documents(chunks[:1])
            print(f"5) {name}: no error (unexpected)")
        except EmbeddingUnavailable as err:
            print(f"5) {name}: {err.kind}: {err.reason} | key shown: {'SECRET' in err.reason}")

    # The pacer: 80 requests in one minute, the 81st waits.
    t[0] = 1000.0
    p = _MinutePacer(EMBED_MAX_REQUESTS_PER_MIN, EMBED_MAX_TOKENS_PER_MIN, clock)
    for _ in range(EMBED_MAX_REQUESTS_PER_MIN):
        p.record(10)
    print(f"6) pacer: request 81 within the minute waits {p.wait_for(10):.0f} s; ", end="")
    p2 = _MinutePacer(EMBED_MAX_REQUESTS_PER_MIN, EMBED_MAX_TOKENS_PER_MIN, clock)
    p2.record(20_000)
    t[0] += 45
    print(f"5,000 tokens 45 s after 20,000 (cap {EMBED_MAX_TOKENS_PER_MIN:,}) waits {p2.wait_for(5_000):.0f} s; "
          f"1,000 tokens go now: {p2.wait_for(1_000) == 0}")
    print(f"7) daily reset: {daily_reset_text()}")


# Live: one batch of 3 made-up sentences and 1 question (2 requests). Prints
# whether a key was found, vector sizes and similarities; never the key.
def _check() -> None:
    try:
        embedder = make_embedder()
    except EmbeddingUnavailable as e:
        print(f"key found: no. {e.reason}")
        return
    print(f"key found: yes ({GEMINI_API_KEY_NAME} in the environment or .env); model {embedder.model_id}")
    texts = ["An AVL tree rebalances itself after insertion so that the heights of the two subtrees differ by at most one.",
             "Dijkstra's algorithm finds the shortest path from one node to every other node in a weighted graph.",
             "The canteen sells chicken rice and iced tea at lunchtime."]
    chunks = [Chunk("file", "XX1000", "_1_1", f"_{i}_1", None, 1, 1, "made-up notes", t) for i, t in enumerate(texts)]
    t0 = time.monotonic()
    try:
        docs = embedder.embed_documents(chunks, on_wait=print)
        query = embedder.embed_query("AVL rotation")
    except EmbeddingUnavailable as e:
        print(f"Gemini: {e.kind}: {e.reason}")
        return
    print(f"2 requests in {time.monotonic() - t0:.1f} s; vector sizes: documents {[d.shape[0] for d in docs]}, "
          f"question {query.shape[0]} (expected {EMBED_DIMENSIONS})")
    for name, d in zip(["AVL sentence", "Dijkstra sentence", "canteen sentence"], docs):
        print(f"   similarity of 'AVL rotation' to the {name}: {float(query @ d):.3f}")
    print("Expected: the AVL sentence highest, though it never says 'rotation'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Self-test of embeddings (offline), or --check against Gemini (live).")
    ap.add_argument("--check", action="store_true", help="embed 3 made-up sentences and 1 question with Gemini")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
    sys.stdout.reconfigure(errors="replace")
    _check() if args.check else _offline()
