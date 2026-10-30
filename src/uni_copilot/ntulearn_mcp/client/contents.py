"""
Course content: the worker that puts the Phase 4 parts together, in the order
they run. The tools (server.py, step 10) and the indexer (step 12) call this.

In plain words, for every question:
  1. The course TREE first (the map): from the cache if it's under 6 hours
     old, else one request per course site (recursive=true), parsed by
     parsers/content.py and saved. Fetching a tree also saves every page's
     text (the bodies come with the tree), so pages are searchable at once.
  2. Find the item asked for in the tree (by content id, or by the gradebook
     column id that deadlines carry).
  3. Only for a file: its text from the cache if made from the item's current
     version, else download it (get_file into a sink in memory), read it
     (parsers/files.py), save the text, drop the bytes. An embedded file's
     signed link is taken from the page fetched just now, never from the cache.
  4. Return one of the step 5 models, which says "cached at ... SGT".

Verified facts this builds on (probe and live checks, 29-30 Oct; Appendix A):
- GET /learn/api/public/v1/courses/{id}/contents?recursive=true: the whole
  tree; every item has parentId (top items point at the course's hidden root
  folder, which isn't in the list).
- A file item's attachments: .../contents/{id}/attachments (id, fileName,
  mimeType); download: .../attachments/{aid}/download (get_file).
- The internal folder list /learn/api/v1/courses/{id}/contents/{folder|ROOT}/children
  carries adaptiveReleaseRules on items that have rules. Whether it also has
  `visibility` (open to you or not) isn't verified: without it, an item stays
  "conditional", with its rule's title.
- A test link's details: the public item (password, proctoring, late
  attempts), the gradebook column (attempts, due) and the internal item.

Read-only: everything goes through NtuLearnHttp (GET only). Titles, page
text, file text and rule titles are UNTRUSTED. Never a grade: the status
comes from gradebook.my_statuses (status only), and no score is read.
"""

import argparse
import io
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from uni_copilot.ntulearn_mcp.cache import CachedAttachment, CachedFile, ContentCache, is_fresh
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.client.gradebook import my_statuses, status_for
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError, NtuLearnHttp
from uni_copilot.ntulearn_mcp.config import (
    DOWNLOAD_MAX_BYTES,
    READ_MAX_BYTES,
    READ_MAX_CHARS,
    READ_MAX_PAGES,
    SEND_MAX_BYTES,
    load_settings,
)
from uni_copilot.ntulearn_mcp.models import (
    Assessment,
    ContentItem,
    ContentList,
    DownloadResult,
    FilePage,
    FileText,
    PageText,
    Release,
)
from uni_copilot.ntulearn_mcp.parsers.content import (
    assessment_details,
    embedded_links,
    find_ultra_pages,
    page_body,
    page_text,
    release_from_internal,
    release_from_public,
    to_content_item,
)
from uni_copilot.ntulearn_mcp.parsers.files import choose_pages, detect_type, extract_text

log = logging.getLogger(__name__)

PUBLIC = "/learn/api/public/v1/courses"
INTERNAL = "/learn/api/v1/courses"
TREE_PAGE_LIMIT = 200
# download_file refuses these: videos are a plan non-goal (and big).
SENDABLE_EXTENSIONS = {".pdf", ".pptx"}      # send_course_file (decision 12)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm"}
# Windows refuses these as file names, whatever the extension.
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME_CHARS = 150


# Where a content id was found: which site, which subject, and the item.
class _Found:
    def __init__(self, site_id: str, course_code: str, item: ContentItem):
        self.site_id, self.course_code, self.item = site_id, course_code, item


class ContentsClient:
    # Keeps the shared HTTP and courses clients and the cache. `now` can be
    # swapped for a fake clock in tests.
    def __init__(self, http: NtuLearnHttp, courses: CoursesClient, cache: ContentCache | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._http = http
        self._courses = courses
        self._cache = cache or ContentCache()
        self._now = now

    # ---------- 1. Trees ----------

    # One course site's items: from the cache if fresh (and not `refresh`),
    # else fetched (one recursive request, a few pages at most), parsed and
    # saved, together with the text of every page in it.
    # Returns (items, fetched_at, from_cache).
    def site_tree(self, site_id: str, course_code: str, refresh: bool = False) -> tuple[list[ContentItem], datetime, bool]:
        now = self._now()
        cached = None if refresh else self._cache.load_tree(site_id)
        if cached and is_fresh(cached.fetched_at, now):
            return cached.items, cached.fetched_at, True
        raws = self._http.get_all(f"{PUBLIC}/{site_id}/contents", {"recursive": "true", "limit": TREE_PAGE_LIMIT})
        pages = find_ultra_pages(raws)
        items = [it for raw in raws
                 if (it := to_content_item(raw, course_code, site_id, pages, release_from_public(raw)))]
        self._cache.save_tree(site_id, course_code, items, now)
        by_id = {raw.get("id"): raw for raw in raws}
        for item in items:
            if item.kind == "page":
                text, links = page_text(page_body(by_id[item.id], pages))
                self._cache.save_page(site_id, course_code, item.id, item.title, text, links, item.modified_at, now)
                # A replaced embedded file comes back with a new xid: drop the old one's text.
                self._cache.prune_files(site_id, item.id, {f.xid for f in item.embedded_files})
        log.info("tree %s: %d items fetched", course_code, len(items))
        return items, now, False

    # All items of one subject (its lecture and tutorial sites together).
    # Returns (items, oldest fetch time, all from cache?, notes for sites that failed).
    def subject_tree(self, course: str, refresh: bool = False) -> tuple[str, list[ContentItem], datetime, bool, list[str]]:
        subject = self._one_subject(course)
        items: list[ContentItem] = []
        times: list[datetime] = []
        all_cached = True
        notes: list[str] = []
        for site in subject.sites:
            try:
                site_items, fetched_at, from_cache = self.site_tree(site.id, subject.code, refresh)
            except NtuLearnError as e:
                notes.append(f"{subject.code} content unavailable on site {site.id} ({e.status}).")
                continue
            items += site_items
            times.append(fetched_at)
            all_cached = all_cached and from_cache
        if not times:
            raise NtuLearnError(f"{subject.code} content", None, "no site of this course could be read")
        return subject.code, items, min(times), all_cached, notes

    # ---------- 2. Listing one folder ----------

    # One folder level (or the top level when folder_id is None), in
    # NTULearn's order. Items with release rules are then checked against the
    # internal folder list (1 request per site, only if any item there has
    # rules), so they say "open to you" / "held back" / dates where NTULearn
    # tells us; if that fails they stay "conditional" and a note says why.
    def list_folder(self, course: str, folder_id: str | None = None, refresh: bool = False) -> ContentList:
        code, items, fetched_at, from_cache, notes = self.subject_tree(course, refresh)
        ids = {item.id for item in items}
        if folder_id is None:
            level = [item for item in items if item.parent_id not in ids]
            folder_title = None
        else:
            folder = next((item for item in items if item.id == folder_id), None)
            if folder is None:
                raise ValueError(f"No folder {folder_id} in {code}. Use list_course_content without folder_id to start.")
            if folder.kind == "page":
                raise ValueError(f"{folder_id} is a page, not a folder: use read_page to read it.")
            level = [item for item in items if item.parent_id == folder_id]
            folder_title = folder.title
        level = self._with_releases(level, folder_id, notes)
        return ContentList(course_code=code, folder_id=folder_id, folder_title=folder_title, items=level,
                           cached_at=fetched_at, from_cache=from_cache, notes=notes)

    # Replaces "conditional" with what the internal folder list says, per site.
    # Not cached: whether you meet a rule can change at any moment.
    def _with_releases(self, level: list[ContentItem], folder_id: str | None, notes: list[str]) -> list[ContentItem]:
        sites = {item.course_site_id for item in level if item.release.state == "conditional"}
        better: dict[str, Release] = {}
        for site_id in sites:
            try:
                rows = self._http.get_all(f"{INTERNAL}/{site_id}/contents/{folder_id or 'ROOT'}/children")
            except NtuLearnError as e:
                notes.append(f"Release rules couldn't be read ({e.status}); items with rules show as 'conditional'.")
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("adaptiveReleaseRules"):
                    better[str(row.get("id"))] = release_from_internal(row)
        return [item.model_copy(update={"release": better[item.id]}) if item.id in better else item for item in level]

    # ---------- 3. Pages ----------

    # A page's text as light Markdown, its links and its embedded files: from
    # the cache if fresh, else the site's tree is fetched again (which saves
    # every page's text) and the page read from that.
    def read_page(self, course: str, content_id: str, refresh: bool = False) -> PageText:
        found = self._find(course, content_id, refresh)
        if found.item.kind != "page":
            raise ValueError(f"{content_id} is {_a(found.item.kind)}, not a page.")
        page = self._cache.load_page(found.site_id, content_id)
        from_cache = True
        if refresh or page is None or not is_fresh(page.fetched_at, self._now()):
            items, _, from_cache = self.site_tree(found.site_id, found.course_code, refresh=True)
            found = _Found(found.site_id, found.course_code, next(i for i in items if i.id == content_id))
            page = self._cache.load_page(found.site_id, content_id)
        if page is None:
            raise ValueError(f"The page {content_id} has no text on NTULearn.")
        return PageText(course_code=found.course_code, content_id=content_id, title=page.title, text=page.text,
                        links=page.links, embedded_files=found.item.embedded_files,
                        cached_at=page.fetched_at, from_cache=from_cache)

    # ---------- 4. Files ----------

    # The text of one file, some pages at a time. `file_ref` is the
    # attachment id (file items; optional when there's only one attachment)
    # or the xid of a file embedded in a page (optional when the page has
    # only one). `pages`: "5", "3-10", "21-" or None (from the start).
    def file_text(self, course: str, content_id: str, file_ref: str | None = None, pages: str | None = None,
                  refresh: bool = False) -> FileText:
        found = self._find(course, content_id, refresh)
        cached, file_ref, from_cache = self.file_pages(found.site_id, found.course_code, found.item, file_ref, refresh)
        numbers, cut_short, next_pages = choose_pages(pages, cached.pages, READ_MAX_PAGES, READ_MAX_CHARS)
        return FileText(
            course_code=found.course_code, content_id=content_id, file_ref=file_ref, file_name=cached.file_name,
            file_type=cached.file_type, total_pages=len(cached.pages),
            pages=[FilePage(number=n, text=cached.pages[n - 1]) for n in numbers],
            cut_short=cut_short, next_pages=next_pages, empty_pages=cached.empty_pages,
            cached_at=cached.extracted_at, from_cache=from_cache,
        )

    # A file's whole text (CachedFile): from the cache if made from the item's
    # current version (and not `refresh`), else downloaded into memory, read,
    # saved and returned. Also used by the indexer for every file. Returns
    # (text, the file_ref used, from_cache).
    # Which version the text belongs to: a file item's `modified` time; for
    # an embedded file None, because its xid already names one exact upload
    # (a replaced file gets a new xid), and the page's `modified` also
    # changes when only its text is edited.
    def file_pages(self, site_id: str, course_code: str, item: ContentItem, file_ref: str | None = None,
                   refresh: bool = False) -> tuple[CachedFile, str, bool]:
        start, name, mime, file_ref = self._file_start(site_id, item, file_ref, refresh)
        version = item.modified_at if item.kind == "file" else None
        if not refresh:
            cached = self._cache.load_file_text(site_id, item.id, file_ref, version)
            if cached is not None:
                return cached, file_ref, True
        sink = io.BytesIO()
        self._fetch_file(start(), READ_MAX_BYTES, sink)
        result = extract_text(sink.getvalue(), name, mime)
        now = self._now()
        self._cache.save_file_text(site_id, course_code, item.id, file_ref, name, result.file_type,
                                   result.pages, version, now)
        cached = self._cache.load_file_text(site_id, item.id, file_ref, version)
        assert cached is not None   # just saved
        return cached, file_ref, False

    # Works out how to download a file, WITHOUT downloading it yet: returns a
    # function that gives the download path or signed link when called (so an
    # embedded file's page is fetched only if the text isn't cached), and the
    # file's name, mime type and file_ref.
    def _file_start(self, site_id: str, item: ContentItem, file_ref: str | None,
                    refresh: bool) -> tuple[Callable[[], str], str, str | None, str]:
        if item.kind == "file":
            atts = self._attachments(site_id, item.id, refresh)
            if not atts:
                raise ValueError(f"The file item {item.id} has no attachment on NTULearn.")
            att = next((a for a in atts if a.attachment_id == file_ref), None) if file_ref else atts[0]
            if att is None:
                raise ValueError(f"No attachment {file_ref} on {item.id}; it has: {', '.join(a.attachment_id for a in atts)}.")
            path = f"{PUBLIC}/{site_id}/contents/{item.id}/attachments/{att.attachment_id}/download"
            return (lambda: path), att.file_name, att.mime_type, att.attachment_id
        if item.kind == "page":
            files = item.embedded_files
            if not files:
                raise ValueError(f"The page {item.id} has no embedded files.")
            if file_ref is None and len(files) > 1:
                raise ValueError(f"The page {item.id} has {len(files)} embedded files; pass file_ref, one of: "
                                 + ", ".join(f.xid for f in files) + ".")
            emb = next((f for f in files if f.xid == file_ref), None) if file_ref else files[0]
            if emb is None:
                raise ValueError(f"No embedded file {file_ref} on the page {item.id}.")
            if not emb.has_link:
                raise ValueError(f"NTULearn gives no download link for '{emb.name}', so it can't be read.")
            return (lambda: self._fresh_embedded_link(site_id, item.id, emb.xid)), emb.name, emb.mime_type, emb.xid
        raise ValueError(f"{item.id} is {_a(item.kind)}: only files and files embedded in pages can be read.")

    # A file item's attachments: from the cache if fetched within 6 hours,
    # else one request (ids, names, types; no links) and saved.
    def _attachments(self, site_id: str, content_id: str, refresh: bool) -> list[CachedAttachment]:
        now = self._now()
        cached = None if refresh else self._cache.load_attachments(site_id, content_id)
        if cached and is_fresh(cached[1], now):
            return cached[0]
        raws = self._http.get_all(f"{PUBLIC}/{site_id}/contents/{content_id}/attachments")
        atts = [CachedAttachment(str(a["id"]), str(a.get("fileName") or "file"), a.get("mimeType"))
                for a in raws if a.get("id")]
        self._cache.save_attachments(site_id, content_id, atts, now)
        return atts

    # An embedded file's signed link, from the page body fetched right now:
    # an Ultra page's folder -> its children (the hidden body document); a
    # plain document (no children) -> the document itself. One or two
    # requests. The link is never cached, logged or returned to a tool.
    def _fresh_embedded_link(self, site_id: str, page_id: str, xid: str) -> str:
        try:
            children = self._http.get_all(f"{PUBLIC}/{site_id}/contents/{page_id}/children")
        except NtuLearnError:
            children = []   # a plain document has no children list
        bodies = [c.get("body") for c in children if c.get("body")]
        if not bodies:
            bodies = [self._http.get_json(f"{PUBLIC}/{site_id}/contents/{page_id}").get("body")]
        for body in bodies:
            link = embedded_links(body).get(xid)
            if link:
                return link
        raise ValueError("That file is no longer on the page on NTULearn (it may have been removed).")

    # get_file with NTULearn's "not found" turned into a plain message (seen
    # 30 Oct: a page still linking a file that was removed from the store).
    def _fetch_file(self, start: str, max_bytes: int, sink) -> None:
        try:
            self._http.get_file(start, max_bytes, sink)
        except NtuLearnError as e:
            if e.status == 404:
                raise ValueError("NTULearn couldn't find this file (404): it may have been removed, "
                                 "even though the page still links it.") from None
            raise

    # ---------- 5. Saving a file (download_file) ----------

    # Saves one file into <download_dir>/<COURSE>/ with a cleaned name, never
    # overwriting (adds " (2)"), refusing videos and files over 200 MB. The
    # bytes go to a temporary ".part" file first, renamed when complete, so
    # a broken download never leaves a half file under the real name. The
    # file gets Windows' Mark of the Web and is never opened or run.
    def save_file(self, course: str, content_id: str, file_ref: str | None = None,
                  download_dir: Path | None = None) -> DownloadResult:
        found = self._find(course, content_id)
        start, name, mime, _ = self._file_start(found.site_id, found.item, file_ref, refresh=False)
        if PurePosixPath(name.lower()).suffix in VIDEO_EXTENSIONS or (mime or "").startswith("video/"):
            raise ValueError("Videos aren't downloaded (a uni-copilot rule): open it on NTULearn instead.")
        folder = (download_dir or load_settings().download_dir) / safe_file_name(found.course_code)
        folder.mkdir(parents=True, exist_ok=True)
        final = unique_path(folder, safe_file_name(name))
        part = final.with_name(final.name + ".part")
        try:
            with open(part, "xb") as sink:     # "x": fails if it exists, never overwrites
                self._fetch_file(start(), DOWNLOAD_MAX_BYTES, sink)
            os.rename(part, final)             # on Windows this also refuses to overwrite
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        mark_of_the_web(final)
        return DownloadResult(course_code=found.course_code, file_name=final.name, saved_to=str(final),
                              size_bytes=final.stat().st_size, content_type=mime)

    # One course file in memory, for send_course_file (Telegram): PDF or
    # PPTX only (by name, then checked by its first bytes), at most
    # SEND_MAX_BYTES (Telegram's 50 MB for bots). Never written to disk.
    # Returns (course code, file name, "pdf" | "pptx", the bytes).
    def file_bytes_for_sending(self, course: str, content_id: str,
                               file_ref: str | None = None) -> tuple[str, str, str, bytes]:
        found = self._find(course, content_id)
        start, name, mime, _ = self._file_start(found.site_id, found.item, file_ref, refresh=False)
        if PurePosixPath(name.lower()).suffix not in SENDABLE_EXTENSIONS:
            raise ValueError(f"Only PDF and PPTX files are sent to Telegram; '{name}' isn't one. "
                             "It can be downloaded at the PC instead.")
        sink = io.BytesIO()
        self._fetch_file(start(), SEND_MAX_BYTES, sink)
        data = sink.getvalue()
        try:
            kind = detect_type(data, name, mime)
        except ValueError:
            kind = None
        if kind not in ("pdf", "pptx"):
            raise ValueError(f"'{name}' doesn't look like a real PDF or PPTX file, so it isn't sent.")
        return found.course_code, name, kind, data

    # ---------- 6. Assessments ----------

    # One test or assignment, by its content id or by the gradebook column
    # id a deadline carries. Live, ~4 requests: the public item (password,
    # proctoring, instructions), its gradebook column (attempts, due), the
    # internal item (release rules), your submission status (status only).
    def assessment(self, course: str, content_id: str | None = None, column_id: str | None = None) -> Assessment:
        if not content_id and not column_id:
            raise ValueError("Give content_id (from list_course_content) or column_id (from a deadline).")
        if content_id:
            found = self._find(course, content_id)
        else:
            code, items, _, _, _ = self.subject_tree(course)
            item = next((i for i in items if i.column_id == column_id), None)
            if item is None:
                raise ValueError(f"No test or assignment in {code} has the gradebook column {column_id}.")
            found = _Found(item.course_site_id, code, item)
        item = found.item
        if item.kind not in ("assessment", "survey"):
            raise ValueError(f"{item.id} is {_a(item.kind)}, not a test or assignment.")
        raw = self._http.get_json(f"{PUBLIC}/{found.site_id}/contents/{item.id}")
        column = internal = None
        if item.column_id:
            column = self._optional(f"/learn/api/public/v2/courses/{found.site_id}/gradebook/columns/{item.column_id}")
        internal = self._optional(f"{INTERNAL}/{found.site_id}/contents/{item.id}")
        details = assessment_details(raw, column, internal)
        status = None
        if item.column_id:
            try:
                status = status_for(item.column_id, my_statuses(self._http, found.site_id, self._courses.user_id()))
            except NtuLearnError:
                status = "unknown"
        release = release_from_internal(internal) if internal else item.release
        return Assessment(course_code=found.course_code, course_site_id=found.site_id, content_id=item.id,
                          title=item.title, kind=item.kind, release=release, status=status,
                          cached_at=self._now(), from_cache=False, **details)

    # A GET that may fail without spoiling the answer (the extra details).
    def _optional(self, path: str) -> dict[str, Any] | None:
        try:
            return self._http.get_json(path)
        except NtuLearnError as e:
            log.info("optional detail unavailable (%s)", e.status)
            return None

    # ---------- Finding things ----------

    # Exactly one subject for a course name or code (courses.pick does the
    # matching and raises a plain ValueError for 0 or 2+ matches).
    def _one_subject(self, course: str):
        return self._courses.pick(course)[0]

    # An item by content id in one subject's trees. If it isn't in a cached
    # tree, the trees are fetched again once (it may be new).
    def _find(self, course: str, content_id: str, refresh: bool = False) -> _Found:
        for attempt in (refresh, True):
            code, items, _, _, _ = self.subject_tree(course, refresh=attempt)
            item = next((i for i in items if i.id == content_id), None)
            if item is not None:
                return _Found(item.course_site_id, code, item)
            if attempt:
                break
        raise ValueError(f"No item {content_id} in {code}. Use list_course_content to find it.")


# "a folder", "an assessment": the kind with the right article, for messages.
def _a(kind: str) -> str:
    return ("an " if kind[:1] in "aeiou" else "a ") + kind.replace("_", " ")


# ---------- File names (pure, tested offline) ----------

# A file name made safe for Windows: only the last part of any path (so
# "..\..\x" can't climb out of the folder), no characters Windows refuses
# (<>:"/\|?* and control characters), no trailing dots or spaces, no
# reserved names (CON, NUL, COM1, ...), at most MAX_NAME_CHARS with the
# extension kept. Never empty. The name is UNTRUSTED (written by others).
def safe_file_name(name: str) -> str:
    base = PureWindowsPath(PurePosixPath(name.replace("\\", "/")).name).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().rstrip(". ")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    if stem.split(".")[0].upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    ext = ext[:10]
    stem = stem[:MAX_NAME_CHARS - len(ext) - 1].rstrip(". ") if ext else stem[:MAX_NAME_CHARS]
    result = f"{stem}.{ext}" if ext else stem
    return result if result.strip("._ ") else "file"


# The first free path in `folder` for `name`: "name.pdf", "name (2).pdf",
# "name (3).pdf", ... An existing file is never overwritten.
def unique_path(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists() or path.with_name(path.name + ".part").exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


# Marks a downloaded file as "from the internet" (Windows' Mark of the Web,
# the Zone.Identifier stream browsers add), so Office opens it in Protected
# View. Windows only; silently skipped where the drive can't store it.
def mark_of_the_web(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://ntulearn.ntu.edu.sg/\r\n")
    except OSError:
        log.info("Mark of the Web not written (this drive doesn't support it)")


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.contents [--course CODE]
# Live, GET only, into a TEMPORARY cache (deleted afterwards): the real cache
# isn't touched. Prints counts, kinds, states and timings; never titles, text,
# names or links.
def _main() -> None:
    import tempfile
    from collections import Counter

    ap = argparse.ArgumentParser(description="Live self-test of the contents client (GET only, counts only).")
    ap.add_argument("--course", help="a course code (default: the first one this term)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    http = NtuLearnHttp()
    courses = CoursesClient(http, load_settings())
    with tempfile.TemporaryDirectory() as tmp:
        client = ContentsClient(http, courses, ContentCache(Path(tmp) / "content.sqlite"))
        code = args.course or courses.list_courses().courses[0].code

        t0 = time.monotonic()
        top = client.list_folder(code)
        print(f"1) {code} top level: {len(top.items)} items {dict(Counter(i.kind for i in top.items))}, "
              f"from cache: {top.from_cache}, {time.monotonic() - t0:.1f} s; notes: {top.notes or 'none'}")
        t0 = time.monotonic()
        again = client.list_folder(code)
        print(f"2) again: from cache: {again.from_cache}, {time.monotonic() - t0:.2f} s")

        _, items, _, _, _ = client.subject_tree(code)
        print(f"3) whole tree: {len(items)} items {dict(Counter(i.kind for i in items))}; "
              f"release states {dict(Counter(i.release.state for i in items))}")
        folder = next((i for i in items if i.kind == "folder" and any(c.parent_id == i.id for c in items)
                       and any(c.parent_id == i.id and c.release.state == "conditional" for c in items)), None)
        if folder:
            listing = client.list_folder(code, folder.id)
            print(f"4) a folder with rules: {len(listing.items)} items, after the internal list: "
                  f"{dict(Counter(i.release.state for i in listing.items))}; notes: {listing.notes or 'none'}")
        else:
            print("4) no folder with conditional items in this course")

        page = next((i for i in items if i.kind == "page"), None)
        if page:
            p = client.read_page(code, page.id)
            print(f"5) a page: {len(p.text):,} characters, {len(p.links)} links, {len(p.embedded_files)} embedded files")
        no_link = sum(1 for i in items for f in i.embedded_files if not f.has_link)
        print(f"   embedded files in this course: {sum(len(i.embedded_files) for i in items)}, without a link: {no_link}")

        targets = [("a file item", next((i for i in items if i.kind == "file"
                                         and (i.file_name or "").lower().endswith((".pdf", ".pptx"))), None), None),
                   ("an embedded file", next((i for i in items if i.kind == "page" and any(
                       f.has_link and f.name.lower().endswith((".pdf", ".pptx", ".ipynb")) for f in i.embedded_files)), None),
                    "embedded")]
        for label, item, mode in targets:
            if item is None:
                print(f"6) {label}: none in this course")
                continue
            ref = next(f.xid for f in item.embedded_files if f.has_link
                       and f.name.lower().endswith((".pdf", ".pptx", ".ipynb"))) if mode else None
            for run in ("first", "second"):
                t0 = time.monotonic()
                try:
                    ft = client.file_text(code, item.id, ref)
                except ValueError as e:
                    print(f"6) {label}: {e}")
                    break
                print(f"6) {label}, {run} read: {ft.file_type}, {ft.total_pages} pages, returned {len(ft.pages)}, "
                      f"cut short {ft.cut_short}, next {ft.next_pages}, from cache {ft.from_cache}, "
                      f"{time.monotonic() - t0:.2f} s")

        quiz = next((i for i in items if i.kind == "assessment"), None)
        if quiz:
            a = client.assessment(code, quiz.id)
            by_column = client.assessment(code, column_id=quiz.column_id) if quiz.column_id else None
            print(f"7) an assessment: status {a.status}, release {a.release.state}, has instructions "
                  f"{a.instructions is not None}, due known {a.due_at is not None}, attempts known "
                  f"{a.attempts_allowed is not None}, password {a.password_required}, secure browser "
                  f"{a.secure_browser_required}, late attempts {a.late_attempts_allowed}; "
                  f"same item by column id: {by_column is not None and by_column.content_id == a.content_id}")
        else:
            print("7) no assessment in this course")
        print(f"8) temporary cache: {client._cache.stats()} (deleted now; the real cache wasn't touched)")


if __name__ == "__main__":
    _main()
