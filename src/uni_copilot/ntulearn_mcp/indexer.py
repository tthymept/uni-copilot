"""
The indexer: reads every course's files into the content cache, so search
covers everything, not only what someone happened to read. Run by hand.

  uv run python -m uni_copilot.ntulearn_mcp.indexer               every course this term
  uv run python -m uni_copilot.ntulearn_mcp.indexer --course IE2108
  uv run python -m uni_copilot.ntulearn_mcp.indexer --dry-run     what it would read, and how long
  uv run python -m uni_copilot.ntulearn_mcp.indexer --refresh     read every file again, changed or not

In plain words, per course:
  1. Fetch its tree fresh (1 request per course site). That alone updates
     titles and page text, removes what NTULearn removed, and notices
     replaced files (contents.py + cache.py).
  2. List the readable files: file items and files embedded in pages that
     are PDF, PPTX, notebooks or text, and that have a download link.
  3. Skip each one whose text is already cached for its current version
     (checked locally, no request). Read the others: download into memory,
     extract the text, save it, drop the bytes (ContentsClient.file_pages).
  4. Copy announcements and messages into search (from the event store, read-only).
  The first run reads everything (~140 files, about 30-40 minutes); later
  runs only read what's new or changed (usually a few minutes).

Polite and safe:
  - One request every INDEXER_INTERVAL_S (2 s), half the tools' pace: a
    scheduled sync or a Claude session may use NTULearn at the same time.
  - One indexer at a time (.data/index.lock); sign-ins take turns as always
    (.auth/profile.lock, in auth/session.py).
  - Every file is saved the moment it's read, so stopping half-way (Ctrl+C,
    a lost connection, a sign-out) keeps everything done so far; the next
    run carries on where it stopped.
  - One file that fails (removed from NTULearn, too big, unreadable, not
    supported) is reported and skipped; the run goes on.
  - GET only, like everything else. Prints course codes, file names, page
    counts and timings; never file contents or links.
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
from uni_copilot.ntulearn_mcp.config import INDEX_LOCK, INDEXER_INTERVAL_S, load_settings
from uni_copilot.ntulearn_mcp.models import ContentItem
from uni_copilot.ntulearn_mcp.search import READABLE_EXTENSIONS, KeywordSearch

# Rough cost of reading one file, for the --dry-run estimate: a file item
# takes ~4 requests (attachments list + 3 download hops), an embedded file ~4
# (its page + 3 hops); at 2 s per request, plus reading time.
SECONDS_PER_FILE = 4 * INDEXER_INTERVAL_S + 1


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


# The readable files in one course's items: file items with a readable
# extension, and embedded files (with a download link) in pages. Videos,
# zips, images and other types are left out.
def readable_files(items: list[ContentItem]) -> list[FileJob]:
    jobs = []
    for item in items:
        if item.kind == "file" and PurePosixPath((item.file_name or "").lower()).suffix in READABLE_EXTENSIONS:
            jobs.append(FileJob(item.course_site_id, item.course_code, item, None, item.file_name or ""))
        for emb in item.embedded_files:
            if emb.has_link and PurePosixPath(emb.name.lower()).suffix in READABLE_EXTENSIONS:
                jobs.append(FileJob(item.course_site_id, item.course_code, item, emb.xid, emb.name))
    return jobs


# True if this file's text is already cached for the item's current version.
# Checked locally (no request): file items by their `modified`, embedded files
# by their xid (a replaced upload gets a new xid).
def already_read(cache: ContentCache, job: FileJob) -> bool:
    if job.file_ref is None:
        return bool(cache.current_file_refs(job.site_id, job.item.id, job.item.modified_at))
    return job.file_ref in cache.current_file_refs(job.site_id, job.item.id, None)


class Indexer:
    # Keeps the contents client (which uses a slow HTTP client, see make_indexer)
    # and the cache; `out` is where progress lines go (stdout by default).
    def __init__(self, contents: ContentsClient, courses: CoursesClient, cache: ContentCache, out=None):
        self._contents = contents
        self._courses = courses
        self._cache = cache
        self._out = out or sys.stdout

    # Prints one progress line at once (flush), so a long run shows where it is.
    def _say(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    # The whole run: every course (or one), its tree fresh, then each readable
    # file not read yet. dry_run: trees only, then a list of what would be read
    # and an estimate. refresh: read every file again. Stops cleanly on a
    # sign-in problem or Ctrl+C, keeping everything done so far.
    def run(self, course: str | None = None, dry_run: bool = False, refresh: bool = False) -> RunReport:
        report = RunReport()
        subjects = self._courses.pick(course) if course else self._courses.list_courses().courses
        todo: list[FileJob] = []
        try:
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
                return report
            for n, job in enumerate(todo, 1):
                self._read_one(job, n, len(todo), refresh, report)
        except KeyboardInterrupt:
            report.stopped = "stopped by you (Ctrl+C)"
        except AuthError as e:
            report.stopped = f"sign-in problem ({e}); run the ntulearn `login` tool, then run the indexer again"
        posts_note: list[str] = []
        KeywordSearch(self._cache).refresh_posts(posts_note)
        if not dry_run and not report.stopped and course is None:
            self._cache.set_state("indexer_finished_at", time_to_db(datetime.now(timezone.utc)))
        self._summary(report, posts_note)
        return report

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

    # The closing lines: what was read, what failed, what search covers now.
    def _summary(self, report: RunReport, posts_note: list[str]) -> None:
        coverage = KeywordSearch(self._cache).coverage()
        self._say(f"\nRead {report.read} files ({report.pages} pages) in {report.courses} courses; "
                  f"{report.already_read} were already read; {len(report.failed)} skipped.")
        for line in report.failed:
            self._say(f"   skipped: {line}")
        if report.stopped:
            self._say(f"Stopped early: {report.stopped}. Everything read so far is kept; run it again to carry on.")
        self._say(f"Search now covers {coverage.courses_indexed} of {coverage.courses_total} courses, "
                  f"{coverage.files} of {coverage.files_known} known files, {coverage.pages} pages, "
                  f"{coverage.posts} posts." + (f" {' '.join(posts_note)}" if posts_note else ""))


# Builds an indexer with its own, slower HTTP client (INDEXER_INTERVAL_S) and
# the real cache.
def make_indexer() -> Indexer:
    http = NtuLearnHttp(interval=INDEXER_INTERVAL_S)
    courses = CoursesClient(http, load_settings())
    cache = ContentCache()
    return Indexer(ContentsClient(http, courses, cache), courses, cache)


# The command line. Takes the indexer lock first: a second run at the same
# time stops at once with a plain message. Exit code 1 if it stopped early.
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read every course's files into the search index (GET only).")
    ap.add_argument("--course", help="only this course, e.g. IE2108")
    ap.add_argument("--dry-run", action="store_true", help="fetch trees and list what would be read; read no files")
    ap.add_argument("--refresh", action="store_true", help="read every file again, even if unchanged")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
    sys.stdout.reconfigure(errors="replace")   # file names may have characters the console lacks
    with file_lock(INDEX_LOCK) as got:
        if not got:
            print("Another indexer run is going (.data/index.lock). Stopping.")
            return 1
        report = make_indexer().run(args.course, args.dry_run, args.refresh)
    return 1 if report.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
