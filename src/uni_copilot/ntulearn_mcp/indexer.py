"""
The indexer: reads every course's files into the content cache, so search
covers everything, not only what someone happened to read, then embeds the
new text for meaning search. Run by hand.

  uv run python -m uni_copilot.ntulearn_mcp.indexer               every course this term, then embed
  uv run python -m uni_copilot.ntulearn_mcp.indexer --course IE2108
  uv run python -m uni_copilot.ntulearn_mcp.indexer --dry-run     what it would read and embed, and how long
  uv run python -m uni_copilot.ntulearn_mcp.indexer --refresh     read every file again, changed or not
  uv run python -m uni_copilot.ntulearn_mcp.indexer --embed-only  only embed (no NTULearn): carry on another day
  uv run python -m uni_copilot.ntulearn_mcp.indexer --no-embed    only read files (nothing sent to Gemini)
  uv run python -m uni_copilot.ntulearn_mcp.indexer --max-tokens 50000   embed at most this much text this run

In plain words, per course (phase 1, reading):
  1. Fetch its tree fresh (1 request per course site). That alone updates
     titles and page text, removes what NTULearn removed, and notices
     replaced files (contents.py + cache.py).
  2. List the readable files: file items and files embedded in pages that
     are PDF, PPTX, notebooks or text, and that have a download link. Data
     files (.csv) are left out: rows of numbers, not reading (read_file_text
     still reads one on request).
  3. Skip each one whose text is already cached for its current version
     (checked locally, no request). Read the others: download into memory,
     extract the text, save it, drop the bytes (ContentsClient.file_pages).
     Saving text also makes its chunks (cache.py, step 15).
  4. Copy announcements and messages into search (from the event store, read-only).
  The first run reads everything (~350 files, about an hour); later runs
  only read what's new or changed (usually a few minutes).

Then phase 2, embedding (step 17), no NTULearn involved:
  5. Make chunks for any text cached without them, drop vectors nothing uses.
  6. Every distinct text without a vector (copies once) is sent to Gemini in
     batches of 50 (embeddings.py): first a line with how many texts and
     tokens, and how long at the free tier's per-minute limits. Each batch's
     vectors are saved at once.
  7. Limits, your rule (a proper answer, never a bare failure):
     per-minute limit: waited out ("waiting 40 s for Gemini's per-minute limit");
     daily limit, a rejected key, Gemini down or offline: embedding stops
     cleanly, everything embedded so far is kept, and the summary says how
     many are left and when to run `--embed-only` again; no key or
     UNI_COPILOT_EMBEDDINGS=off: embedding is skipped with that reason.
     Texts without a vector are still found by keyword search.
  Sent to Google: course text (slides, pages) with its file or page title.
  Never announcements, messages, grades or anything about you.

Polite and safe:
  - One NTULearn request every INDEXER_INTERVAL_S (2 s), half the tools'
    pace: a scheduled sync or a Claude session may use NTULearn at the same time.
  - One indexer at a time (.data/index.lock); sign-ins take turns as always
    (.auth/profile.lock, in auth/session.py).
  - Every file is saved the moment it's read, every batch of vectors the
    moment it's back, so stopping half-way (Ctrl+C, a lost connection, a
    sign-out, a Gemini limit) keeps everything done so far; the next run
    carries on where it stopped.
  - One file that fails (removed from NTULearn, too big, unreadable, not
    supported) is reported and skipped; the run goes on.
  - GET only on NTULearn, like everything else. Prints course codes, file
    names, page counts, token counts and timings; never file contents,
    links or the Gemini key.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

from uni_copilot.filelock import file_lock
from uni_copilot.ntulearn_mcp.auth.session import AuthError
from uni_copilot.ntulearn_mcp.cache import ContentCache, time_to_db
from uni_copilot.ntulearn_mcp.client.contents import ContentsClient
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError, NtuLearnHttp
from uni_copilot.ntulearn_mcp.config import (
    EMBED_BATCH_SIZE,
    INDEX_LOCK,
    INDEXER_INTERVAL_S,
    gemini_tier,
    load_settings,
)
from uni_copilot.ntulearn_mcp.embeddings import (
    GEMINI_MODEL_ID,
    Chunk,
    Embedder,
    EmbeddingUnavailable,
    estimate_tokens,
    make_embedder,
)
from uni_copilot.ntulearn_mcp.models import ContentItem
from uni_copilot.ntulearn_mcp.search import INDEXED_EXTENSIONS, ContentSearch

# Rough cost of reading one file, for the --dry-run estimate: a file item
# takes ~4 requests (attachments list + 3 download hops), an embedded file ~4
# (its page + 3 hops); at 2 s per request, plus reading time.
SECONDS_PER_FILE = 4 * INDEXER_INTERVAL_S + 1
PROGRESS_EVERY = 10     # embedding: a progress line every this many batches


# One readable file found in a tree: which item it belongs to, its file_ref
# (None = the item's only attachment, resolved when read) and its name.
@dataclass(frozen=True)
class FileJob:
    site_id: str
    course_code: str
    item: ContentItem
    file_ref: str | None
    name: str


# What one run did, for the summary at the end.
@dataclass
class RunReport:
    courses: int = 0
    files_known: int = 0
    already_read: int = 0
    read: int = 0
    pages: int = 0
    failed: list[str] = field(default_factory=list)
    stopped: str | None = None
    # Phase 2 (embedding):
    embed_todo: int = 0
    embedded: int = 0
    embed_tokens: int = 0
    embed_stopped: str | None = None     # why embedding stopped before the end (limit, key, offline)
    embed_skipped: str | None = None     # why there was no embedding at all (off, no key, --no-embed)


# The readable files in one course's items: file items with a readable
# extension, and embedded files (with a download link) in pages. Videos,
# zips, images, data files (.csv) and other types are left out.
def readable_files(items: list[ContentItem]) -> list[FileJob]:
    jobs = []
    for item in items:
        if item.kind == "file" and PurePosixPath((item.file_name or "").lower()).suffix in INDEXED_EXTENSIONS:
            jobs.append(FileJob(item.course_site_id, item.course_code, item, None, item.file_name or ""))
        for emb in item.embedded_files:
            if emb.has_link and PurePosixPath(emb.name.lower()).suffix in INDEXED_EXTENSIONS:
                jobs.append(FileJob(item.course_site_id, item.course_code, item, emb.xid, emb.name))
    return jobs


# True if this file's text is already cached for the item's current version.
# Checked locally (no request): file items by their `modified`, embedded files
# by their xid (a replaced upload gets a new xid).
def already_read(cache: ContentCache, job: FileJob) -> bool:
    if job.file_ref is None:
        return bool(cache.current_file_refs(job.site_id, job.item.id, job.item.modified_at))
    return job.file_ref in cache.current_file_refs(job.site_id, job.item.id, None)


# Minutes to embed `texts` texts of `tokens` tokens at the tier's per-minute
# caps (whichever is tighter decides; each text counts as a request).
def embed_minutes(tokens: int, texts: int, limits: tuple[int, int, int | None]) -> float:
    per_min, tokens_per_min, _ = limits
    return max(tokens / tokens_per_min, texts / per_min)


class Indexer:
    # Keeps the contents client (which uses a slow HTTP client, see make_indexer)
    # and the cache; `out` is where progress lines go (stdout by default).
    # `embedder_factory` gives the embedder for phase 2 (tests pass a fake).
    def __init__(self, contents: ContentsClient, courses: CoursesClient, cache: ContentCache, out=None,
                 embedder_factory=make_embedder, model_id: str = GEMINI_MODEL_ID):
        self._contents = contents
        self._courses = courses
        self._cache = cache
        self._out = out or sys.stdout
        self._embedder_factory = embedder_factory
        self._model_id = model_id

    # Prints one progress line at once (flush), so a long run shows where it is.
    def _say(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    # The whole run: phase 1 (read files; skipped with embed_only), then
    # phase 2 (embed; skipped with embed=False). dry_run: trees and counts
    # only, with estimates; nothing read or sent. Stops cleanly on a sign-in
    # problem or Ctrl+C, keeping everything done so far.
    def run(self, course: str | None = None, dry_run: bool = False, refresh: bool = False,
            embed: bool = True, embed_only: bool = False, max_tokens: int | None = None) -> RunReport:
        report = RunReport()
        if not embed_only:
            self._read_phase(report, course, dry_run, refresh)
            ContentSearch(self._cache).refresh_posts([])
            if not dry_run and not report.stopped and course is None:
                self._cache.set_state("indexer_finished_at", time_to_db(datetime.now(timezone.utc)))
        if not embed:
            report.embed_skipped = "--no-embed"
        elif report.stopped and "Ctrl+C" in report.stopped:
            report.embed_skipped = "stopped by you"
        else:
            try:
                self._embed_phase(report, course, dry_run, max_tokens)
            except KeyboardInterrupt:
                report.stopped = "stopped by you (Ctrl+C)"
        self._summary(report, dry_run)
        return report

    # ----- Phase 1: reading -----

    # Every course (or one): its tree fresh, then each readable file not
    # read yet. dry_run: trees only, then a list of what would be read.
    def _read_phase(self, report: RunReport, course: str | None, dry_run: bool, refresh: bool) -> None:
        todo: list[FileJob] = []
        try:
            subjects = self._courses.pick(course) if course else self._courses.list_courses().courses
            for subject in subjects:
                try:
                    code, items, _, _, notes = self._contents.subject_tree(subject.code, refresh=True)
                except NtuLearnError as e:   # no site of this course could be read: skip the course
                    report.failed.append(f"{subject.code}: course content unavailable ({e.status})")
                    self._say(f"{subject.code}: skipped, NTULearn didn't give its content ({e.status})")
                    continue
                report.courses += 1
                jobs = readable_files(items)
                report.files_known += len(jobs)
                waiting = [j for j in jobs if refresh or not already_read(self._cache, j)]
                report.already_read += len(jobs) - len(waiting)
                todo += waiting
                self._say(f"{code}: {len(items)} items, {len(jobs)} readable files, {len(waiting)} to read"
                          + (f" (notes: {'; '.join(notes)})" if notes else ""))
            minutes = len(todo) * SECONDS_PER_FILE / 60
            self._say(f"\n{len(todo)} files to read, about {minutes:.0f} minutes at 1 request every "
                      f"{INDEXER_INTERVAL_S:g} s. Already read: {report.already_read}.")
            if dry_run:
                for job in todo:
                    self._say(f"   would read: {job.course_code} {job.name}")
                return
            for n, job in enumerate(todo, 1):
                self._read_one(job, n, len(todo), refresh, report)
        except KeyboardInterrupt:
            report.stopped = "stopped by you (Ctrl+C)"
        except AuthError as e:
            report.stopped = f"sign-in problem ({e}); run the ntulearn `login` tool, then run the indexer again"

    # Reads one file (download into memory, extract, save) and prints one
    # line. A failure of this one file is reported and the run goes on.
    def _read_one(self, job: FileJob, n: int, total: int, refresh: bool, report: RunReport) -> None:
        t0 = time.monotonic()
        try:
            cached, _, _ = self._contents.file_pages(job.site_id, job.course_code, job.item, job.file_ref, refresh)
        except (ValueError, NtuLearnError) as e:   # removed, too big, unreadable, unsupported, HTTP errors
            report.failed.append(f"{job.course_code} {job.name}: {e}")
            self._say(f"[{n}/{total}] {job.course_code} {job.name}: skipped ({e})")
            return
        report.read += 1
        report.pages += len(cached.pages)
        self._say(f"[{n}/{total}] {job.course_code} {job.name}: {cached.file_type}, {len(cached.pages)} pages"
                  + (f" ({cached.empty_pages} empty)" if cached.empty_pages else "")
                  + f", {time.monotonic() - t0:.1f} s")

    # ----- Phase 2: embedding -----

    # Chunks for any text without them, old vectors dropped, then every
    # distinct text without a vector sent to Gemini batch by batch, each
    # batch saved at once. Stops cleanly at a limit or a key problem.
    def _embed_phase(self, report: RunReport, course: str | None, dry_run: bool, max_tokens: int | None) -> None:
        made = self._cache.fill_missing_chunks()
        pruned = self._cache.prune_vectors(self._model_id)
        todo = self._cache.pending_chunks(self._model_id)
        if course:
            wanted = course.strip().upper().replace(" ", "")
            todo = [c for c in todo if wanted in c.course_code.upper()]
        tokens = [estimate_tokens(c.title + " " + c.text) for c in todo]
        report.embed_todo = len(todo)
        tier, limits = gemini_tier()
        per_day = limits[2] or len(todo) or 1
        days = -(-len(todo) // per_day)
        today = min(len(todo), per_day)
        done, total = self._cache.embedding_counts(self._model_id)
        self._say(f"\nMeaning search: {done} of {total} texts embedded"
                  + (f" ({made} new chunks made, {pruned} old vectors dropped)" if made or pruned else "")
                  + f". {len(todo)} to embed, about {sum(tokens):,} tokens: about "
                  f"{embed_minutes(sum(tokens[:today]), today, limits):.0f} minutes on Gemini's {tier} tier"
                  + (f", a run a day over about {days} days (about {per_day:,} texts a day; each run stops at "
                     "the daily limit and the next carries on)" if days > 1 else "")
                  + (f", up to --max-tokens {max_tokens:,}" if max_tokens else "") + ".")
        if dry_run or not todo:
            return
        try:
            embedder = self._embedder_factory()
        except EmbeddingUnavailable as e:
            report.embed_skipped = e.reason
            return
        self._say("Sending course text (slides and pages, with their titles) to Gemini; Ctrl+C stops safely.")
        t0 = time.monotonic()
        for start in range(0, len(todo), EMBED_BATCH_SIZE):
            batch, batch_tokens = todo[start:start + EMBED_BATCH_SIZE], sum(tokens[start:start + EMBED_BATCH_SIZE])
            if max_tokens is not None and report.embed_tokens + batch_tokens > max_tokens:
                report.embed_stopped = f"reached --max-tokens {max_tokens:,}"
                return
            try:
                vectors = embedder.embed_documents(batch, wait=True, on_wait=lambda text: self._say(f"   {text}"))
            except EmbeddingUnavailable as e:
                report.embed_stopped = e.reason
                return
            self._save_batch(embedder, batch, vectors)
            report.embedded += len(batch)
            report.embed_tokens += batch_tokens
            n = start // EMBED_BATCH_SIZE + 1
            if n % PROGRESS_EVERY == 0 or report.embedded == len(todo):
                self._say(f"   [{report.embedded}/{len(todo)}] embedded, {report.embed_tokens:,} tokens, "
                          f"{(time.monotonic() - t0) / 60:.1f} min")

    # Saves one batch's vectors under each chunk's text hash, at once.
    def _save_batch(self, embedder: Embedder, batch: list[Chunk], vectors: list) -> None:
        self._cache.save_vectors(embedder.model_id, [(c.text_hash, v) for c, v in zip(batch, vectors)],
                                 datetime.now(timezone.utc))

    # ----- Summary -----

    # The closing lines: what was read, what failed, what was embedded,
    # what search covers now, and what to do next if something stopped.
    def _summary(self, report: RunReport, dry_run: bool) -> None:
        coverage = ContentSearch(self._cache, model_id=self._model_id).coverage()
        if report.courses or report.failed:
            self._say(f"\nRead {report.read} files ({report.pages} pages) in {report.courses} courses; "
                      f"{report.already_read} were already read; {len(report.failed)} skipped.")
        for line in report.failed:
            self._say(f"   skipped: {line}")
        if report.stopped:
            self._say(f"Stopped early: {report.stopped}. Everything done so far is kept; run it again to carry on.")
        if report.embed_skipped:
            self._say(f"Embedding skipped: {report.embed_skipped}")
        elif not dry_run and report.embed_todo:
            self._say(f"Embedded {report.embedded} of {report.embed_todo} texts ({report.embed_tokens:,} tokens).")
        if report.embed_stopped:
            left = report.embed_todo - report.embedded
            self._say(f"Embedding stopped: {report.embed_stopped} Everything embedded so far is kept; {left} texts "
                      "left (found by keyword search meanwhile). Carry on later with: "
                      "uv run python -m uni_copilot.ntulearn_mcp.indexer --embed-only")
        self._say(f"Search now covers {coverage.courses_indexed} of {coverage.courses_total} courses, "
                  f"{coverage.files} of {coverage.files_known} known files, {coverage.pages} pages, "
                  f"{coverage.posts} posts; meaning search {coverage.chunks_with_meaning} of "
                  f"{coverage.chunks_total} texts.")


# Builds an indexer with its own, slower HTTP client (INDEXER_INTERVAL_S) and
# the real cache. Making it sends nothing; NTULearn and Gemini are only
# contacted when a phase needs them.
def make_indexer() -> Indexer:
    http = NtuLearnHttp(interval=INDEXER_INTERVAL_S)
    courses = CoursesClient(http, load_settings())
    cache = ContentCache()
    return Indexer(ContentsClient(http, courses, cache), courses, cache)


# The command line. Takes the indexer lock first: a second run at the same
# time stops at once with a plain message. Exit code 1 if reading stopped
# early (Ctrl+C, sign-in); a Gemini limit is expected and not an error.
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read every course's files into search, then embed them (GET only).")
    ap.add_argument("--course", help="only this course, e.g. IE2108")
    ap.add_argument("--dry-run", action="store_true", help="list what would be read and embedded; read and send nothing")
    ap.add_argument("--refresh", action="store_true", help="read every file again, even if unchanged")
    ap.add_argument("--embed-only", action="store_true", help="skip NTULearn; only embed texts without a vector")
    ap.add_argument("--no-embed", action="store_true", help="only read files; send nothing to Gemini")
    ap.add_argument("--max-tokens", type=int, help="embed at most about this many tokens in this run")
    args = ap.parse_args(argv)
    if args.embed_only and args.no_embed:
        ap.error("--embed-only and --no-embed together would do nothing")
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
    sys.stdout.reconfigure(errors="replace")   # file names may have characters the console lacks
    with file_lock(INDEX_LOCK) as got:
        if not got:
            print("Another indexer run is going (.data/index.lock). Stopping.")
            return 1
        report = make_indexer().run(args.course, args.dry_run, args.refresh, embed=not args.no_embed,
                                    embed_only=args.embed_only, max_tokens=args.max_tokens)
    return 1 if report.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
