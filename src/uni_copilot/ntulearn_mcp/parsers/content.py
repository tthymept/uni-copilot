"""
Course content JSON -> the Phase 4 models (ContentItem, Release, EmbeddedFile)
and assessment details. Pure functions: no network, so they're tested offline.

In plain words:
  NTULearn describes a course as a tree of items, each with a "content
  handler" saying what it is (folder, file, test link, ...). This file turns
  those raw items into the shapes the tools return, and works out three
  things NTULearn doesn't say directly:
  - which folders are really Ultra "Document" pages (a folder holding one
    hidden document titled "ultraDocumentBody", whose body is the page);
  - whether an item is open to you (release rules, see below);
  - which files are embedded inside a page, and their download links. The
    links are signed (like a temporary password), so they're returned only
    by embedded_links(), for the download code, and never put in a model.

Verified facts this builds on (probe, 29 Oct; plan Appendix A):
- GET .../contents?recursive=true gives every item with parentId, title,
  contentHandler, availability, body (pages), modified.
- availability.available: Yes or PartiallyVisible ("has release rules";
  whether you meet them isn't said). The internal API's item and folder
  list add adaptiveReleaseRules (rules[] with criteria[]: type Pre_Content
  = "after another item", Date_Range with start/end) and visibility
  (VISIBLE = you meet the rules, PARTIALLY_VISIBLE = held back).
- Files embedded in pages: <a data-bbfile='{...}'> with linkName/mimeType
  and a signed resourceUrl or viewerUrl, both .../bbcswebdav/.../xid-N_N?...
  data-bbtype "embedded-app" and "video" are videos/tools (Phase 5).
- Test links carry gradeColumnId (= a deadline's column id), password,
  proctoring (secure browser, webcam), isLateAttemptCreationDisallowed.

Untrusted: titles, descriptions, rule titles, file names and page text are
written by other people. This file only reads them; nothing is executed.
"""

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from uni_copilot.ntulearn_mcp.config import BASE_URL, FILE_HOST_SUFFIXES
from uni_copilot.ntulearn_mcp.models import ContentItem, ContentKind, EmbeddedFile, Link, Release
from uni_copilot.ntulearn_mcp.parsers.html import parse_html

ULTRA_BODY_TITLE = "ultraDocumentBody"
DESCRIPTION_MAX_CHARS = 500

# NTULearn's content handler -> our kind. Seen on your 16 sites (29 Oct).
HANDLER_KINDS: dict[str, ContentKind] = {
    "resource/x-bb-folder": "folder",
    "resource/x-bb-document": "page",
    "resource/x-bb-file": "file",
    "resource/x-bb-asmt-test-link": "assessment",
    "resource/x-bb-asmt-survey-link": "survey",
    "resource/x-bb-lesson": "module",
    "resource/x-bb-externallink": "link",
    "resource/x-bb-blti-link": "external_tool",
    "resource/x-bb-bltiplacement-NewUCVGalleryLTI_1_1": "video_gallery",
}

# Embedded things that aren't readable files: videos and tools (Phase 5).
SKIPPED_BBTYPES = {"embedded-app", "video"}
BBFILE_RE = re.compile(r"""data-bbfile=(["'])(.*?)\1""", re.S)
BBTYPE_RE = re.compile(r"""data-bbtype=["']([^"']+)["']""")
XID_RE = re.compile(r"xid-\d+_\d+")
HREF_RE = re.compile(r"""\shref=(["'])(.*?)\1""", re.S)
# Paths on NTULearn whose links carry signed values (file store, session files).
SIGNED_PATH_PREFIXES = ("/bbcswebdav/", "/sessions/")


# ---------- Small helpers ----------

# The content handler id of a raw item, e.g. "resource/x-bb-file", or "".
def handler(raw: dict[str, Any]) -> str:
    return str((raw.get("contentHandler") or {}).get("id") or "")


# A time string from NTULearn ("2026-11-02T01:00:00.000Z") -> a UTC datetime,
# or None if missing or unreadable (one bad date shouldn't break a listing).
def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# True for a link that carries signed values and must never be shown or
# stored: anything on Blackboard's file server, and NTULearn's file-store and
# session-file paths. Used to clean page text (hide_signed_links).
def is_signed_file_link(url: str) -> bool:
    parts = urlparse(urljoin(BASE_URL + "/", url))
    host = parts.hostname or ""
    if host.endswith(FILE_HOST_SUFFIXES):
        return True
    return host == urlparse(BASE_URL).hostname and parts.path.startswith(SIGNED_PATH_PREFIXES)


# ---------- The tree: Ultra pages and kinds ----------

# Finds the Ultra "Document" pages in a course tree: folders whose only child
# is a document titled ultraDocumentBody. Returns {folder id: that document},
# so the folder can be shown as one "page" whose text is the document's body.
def find_ultra_pages(raws: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    children: dict[str, list[dict[str, Any]]] = {}
    for raw in raws:
        if raw.get("parentId"):
            children.setdefault(raw["parentId"], []).append(raw)
    return {
        parent: kids[0]
        for parent, kids in children.items()
        if len(kids) == 1 and handler(kids[0]) == "resource/x-bb-document"
        and kids[0].get("title") == ULTRA_BODY_TITLE
    }


# Our kind for one raw item: an Ultra page's folder is a "page"; otherwise
# the handler decides, and an unknown handler is "other".
def content_kind(raw: dict[str, Any], ultra_pages: dict[str, dict[str, Any]]) -> ContentKind:
    if raw.get("id") in ultra_pages:
        return "page"
    return HANDLER_KINDS.get(handler(raw), "other")


# ---------- Release: is it open to you? ----------

# From the public tree alone: "Yes" -> open; "PartiallyVisible" -> it has
# release rules, but whether you meet them isn't known here -> conditional.
# Anything else (e.g. "No", not seen on your courses) -> held back, to be safe.
def release_from_public(raw: dict[str, Any]) -> Release:
    available = (raw.get("availability") or {}).get("available")
    if available == "Yes":
        return Release(state="open")
    if available == "PartiallyVisible":
        return Release(state="conditional")
    return Release(state="held_back")


# From the internal API's item (or an item of its folder list), which has
# the rules and NTULearn's own verdict:
# - no rules -> open;
# - visibility VISIBLE -> open_to_you, PARTIALLY_VISIBLE -> held_back,
#   anything else -> conditional;
# - `rule` = the first rule's title (written by the prof; untrusted);
# - opens/closes from the first date-range criterion, if any (rare: 8 of
#   142 rules on 29 Oct).
def release_from_internal(internal: dict[str, Any]) -> Release:
    rules = ((internal.get("adaptiveReleaseRules") or {}).get("rules")) or []
    if not rules:
        return Release(state="open")
    visibility = str(internal.get("visibility") or "").upper()
    state = {"VISIBLE": "open_to_you", "PARTIALLY_VISIBLE": "held_back"}.get(visibility, "conditional")
    title = next((str(r["title"]).strip() for r in rules if isinstance(r, dict) and r.get("title")), None)
    opens = closes = None
    for rule in rules:
        for crit in (rule.get("criteria") or []) if isinstance(rule, dict) else []:
            if isinstance(crit, dict) and (crit.get("start") or crit.get("end")) and not crit.get("negate"):
                opens, closes = parse_time(crit.get("start")), parse_time(crit.get("end"))
                break
        if opens or closes:
            break
    return Release(state=state, rule=title or None, opens_at=opens, closes_at=closes)


# ---------- Files embedded in pages ----------

# One embedded file as found in a page body: the safe part (EmbeddedFile,
# for tools and Claude) and its signed link (for the download code only).
@dataclass(frozen=True)
class _Embedded:
    file: EmbeddedFile
    link: str | None


# True for a link into NTULearn's own file store that names a file (its path
# starts /bbcswebdav/ and contains xid-N_N). The only kind of link accepted
# from a tag's href as a fallback: never another site, never anything else.
def _file_store_link(url: str) -> bool:
    parts = urlparse(urljoin(BASE_URL + "/", url))
    return (parts.scheme == "https" and parts.hostname == urlparse(BASE_URL).hostname
            and parts.path.startswith("/bbcswebdav/") and bool(XID_RE.search(parts.path)))


# Walks a page body's <a data-bbfile='{...}'> tags. Skips videos and tools
# (data-bbtype embedded-app / video) and pictures; keeps documents. The link
# is the JSON's resourceUrl or viewerUrl; if the JSON has neither (the
# probe's 38, including the course notebooks), the tag's own href is used,
# but only if it's a file-store link (_file_store_link). A file with no
# usable link gets has_link=False and an id "nolink-<n>": it can be listed
# but not read. Duplicates (the same xid twice on a page) are kept once.
def _scan_embedded(body: str | None) -> list[_Embedded]:
    found: list[_Embedded] = []
    seen: set[str] = set()
    text = body or ""
    for n, m in enumerate(BBFILE_RE.finditer(text), 1):
        start = text.rfind("<", 0, m.start())
        tag = text[start:text.find(">", m.end()) + 1] if start >= 0 else ""
        bbtype = BBTYPE_RE.search(tag)
        if bbtype and bbtype.group(1) in SKIPPED_BBTYPES:
            continue
        try:
            meta = json.loads(html.unescape(m.group(2)))
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue
        mime = meta.get("mimeType") if isinstance(meta.get("mimeType"), str) else None
        if mime and mime.startswith(("image/", "video/", "audio/")):
            continue
        name = next((str(meta[k]).strip() for k in ("linkName", "displayName", "fileName")
                     if isinstance(meta.get(k), str) and meta[k].strip()), "")
        link = next((meta[k] for k in ("resourceUrl", "viewerUrl") if isinstance(meta.get(k), str) and meta[k]), None)
        if not link:
            href = HREF_RE.search(tag)
            candidate = html.unescape(href.group(2)) if href else ""
            link = candidate if candidate and _file_store_link(candidate) else None
        xid_match = XID_RE.search(link or "")
        xid = xid_match.group(0) if xid_match else f"nolink-{n}"
        if xid in seen:
            continue
        seen.add(xid)
        found.append(_Embedded(
            file=EmbeddedFile(xid=xid, name=name or "(unnamed file)", mime_type=mime, has_link=bool(xid_match)),
            link=urljoin(BASE_URL + "/", link) if xid_match and link else None,
        ))
    return found


# The files embedded in a page, safe to show (no links).
def embedded_files(body: str | None) -> list[EmbeddedFile]:
    return [e.file for e in _scan_embedded(body)]


# {xid: signed link} for one page's embedded files. ONLY for the download
# code (client/contents.py -> NtuLearnHttp.get_file), always from a page body
# fetched just now. Never cache, log, print or return these.
def embedded_links(body: str | None) -> dict[str, str]:
    return {e.file.xid: e.link for e in _scan_embedded(body) if e.link}


# ---------- Page text ----------

# What read_page shows of a page body: light Markdown (Phase 1's parse_html,
# which already writes every <a data-bbfile> as "[file: name]" without its
# link) and its links, with one more safety pass: any remaining signed file
# link (e.g. one a prof pasted by hand) becomes "[file: text]" and leaves the
# links list, so no signed link reaches Claude or the cache.
def page_text(body: str | None) -> tuple[str, list[Link]]:
    parsed = parse_html(body)
    text = parsed.text
    links: list[Link] = []
    for link_text, href in parsed.links:
        if is_signed_file_link(href):
            text = text.replace(f"[{link_text}]({href})", f"[file: {link_text}]").replace(href, "[file link hidden]")
        else:
            links.append(Link(text=link_text, url=href))
    return text, links


# A short plain description (items can have an HTML description), at most
# DESCRIPTION_MAX_CHARS; None if empty.
def short_description(raw_html: Any) -> str | None:
    if not isinstance(raw_html, str) or not raw_html.strip():
        return None
    text, _ = page_text(raw_html)
    text = " ".join(text.split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        text = text[:DESCRIPTION_MAX_CHARS].rstrip() + " …"
    return text or None


# ---------- Items ----------

# One raw item -> ContentItem. Returns None for an Ultra page's hidden body
# document (it's shown through its folder, as the page). `release` comes from
# the caller: release_from_public (cached tree) or release_from_internal (a
# folder listed with its rules).
def to_content_item(raw: dict[str, Any], course_code: str, site_id: str,
                    ultra_pages: dict[str, dict[str, Any]], release: Release) -> ContentItem | None:
    if handler(raw) == "resource/x-bb-document" and raw.get("title") == ULTRA_BODY_TITLE:
        return None
    kind = content_kind(raw, ultra_pages)
    ch = raw.get("contentHandler") or {}
    file_info = ch.get("file") or {}
    body_doc = ultra_pages.get(raw.get("id", ""))
    body = (body_doc or raw).get("body") if kind == "page" else None
    return ContentItem(
        id=str(raw["id"]),
        course_code=course_code,
        course_site_id=site_id,
        parent_id=raw.get("parentId"),
        title=str(raw.get("title") or "(untitled)"),
        kind=kind,
        description=short_description(raw.get("description")),
        release=release,
        has_children=bool(raw.get("hasChildren")) and kind != "page",
        file_name=file_info.get("fileName") if kind == "file" else None,
        mime_type=file_info.get("mimeType") if kind == "file" else None,
        column_id=ch.get("gradeColumnId") if kind in ("assessment", "survey") else None,
        embedded_files=embedded_files(body) if kind == "page" else [],
        modified_at=parse_time(raw.get("modified")),
    )


# The page body (HTML) of an Ultra page or plain document item, from the
# tree: the hidden document's body for a page folder, else the item's own.
def page_body(raw: dict[str, Any], ultra_pages: dict[str, dict[str, Any]]) -> str | None:
    doc = ultra_pages.get(raw.get("id", ""))
    return (doc or raw).get("body")


# ---------- Assessments ----------

# What get_assessment shows about a test or assignment, from the public
# item, its gradebook column (public v2) and the internal item, whichever
# are available. Never a score, points or "grades released".
# Returns plain values; client/contents.py builds the Assessment model.
def assessment_details(raw: dict[str, Any], column: dict[str, Any] | None,
                       internal: dict[str, Any] | None) -> dict[str, Any]:
    ch = raw.get("contentHandler") or {}
    proctoring = ch.get("proctoring") or {}
    grading = (column or {}).get("grading") or {}
    instructions_html = ch.get("instructions") or raw.get("description")
    due = parse_time(grading.get("due")) or parse_time(((internal or {}).get("genericReadOnlyData") or {}).get("dueDate"))
    attempts = grading.get("attemptsAllowed")
    late_disallowed = ch.get("isLateAttemptCreationDisallowed")
    return {
        "column_id": ch.get("gradeColumnId"),
        "instructions": page_text(instructions_html)[0] or None if isinstance(instructions_html, str) else None,
        "due_at": due,
        "attempts_allowed": attempts if isinstance(attempts, int) else None,
        "password_required": (ch.get("password") or {}).get("enabled"),
        "secure_browser_required": proctoring.get("secureBrowserRequiredToTake"),
        "webcam_required": proctoring.get("webcamRequired"),
        "late_attempts_allowed": (not late_disallowed) if isinstance(late_disallowed, bool) else None,
    }


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.parsers.content
# Offline, made-up JSON only. Shows each conversion and checks that no
# signed link ends up in anything a tool returns.
def _main() -> None:
    signed = "https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1-dt-content-rid-2_1/xid-555_1?sig1=SECRET&sig2=SECRET"
    meta = json.dumps({"linkName": "week10-slides.pdf", "mimeType": "application/pdf", "viewerUrl": signed})
    body = (
        "<p>Slides for this week: "
        f"<a href=\"#\" data-bbfile='{meta}'>week10-slides.pdf</a></p>"
        "<p><a data-bbtype=\"embedded-app\" data-bbfile='{\"url\": \"https://example.org/video\"}'>video</a></p>"
        f"<p>Also <a href=\"{signed}\">the old notes</a> and <a href=\"https://example.org/x\">a site</a>.</p>"
        "<p>No link: <a data-bbfile='{\"linkName\": \"lost.pdf\", \"mimeType\": \"application/pdf\"}'>lost</a></p>"
        # The link only in href (like the course notebooks): accepted, it's a file-store link.
        "<p><a href=\"https://ntulearn.ntu.edu.sg/bbcswebdav/pid-3/xid-777_1?sig=SECRET\" "
        "data-bbfile='{\"linkName\": \"lab4.ipynb\", \"mimeType\": \"application/octet-stream\"}'>lab4.ipynb</a></p>"
        # An href to another site: never accepted as a file link.
        "<p><a href=\"https://example.org/xid-888_1\" "
        "data-bbfile='{\"linkName\": \"elsewhere.pdf\", \"mimeType\": \"application/pdf\"}'>elsewhere</a></p>"
    )
    tree = [
        {"id": "_10_1", "title": "Week 10: Graphs", "contentHandler": {"id": "resource/x-bb-folder"},
         "availability": {"available": "PartiallyVisible"}, "hasChildren": True, "modified": "2026-10-20T02:00:00.000Z"},
        {"id": "_11_1", "parentId": "_10_1", "title": ULTRA_BODY_TITLE, "body": body,
         "contentHandler": {"id": "resource/x-bb-document"}, "availability": {"available": "PartiallyVisible"}},
        {"id": "_12_1", "title": "Tutorial 10.pdf", "availability": {"available": "Yes"},
         "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "T10.pdf", "mimeType": "application/pdf"}}},
        {"id": "_13_1", "title": "Quiz 10", "availability": {"available": "PartiallyVisible"},
         "contentHandler": {"id": "resource/x-bb-asmt-test-link", "gradeColumnId": "_99_1",
                            "password": {"enabled": False}, "isLateAttemptCreationDisallowed": True,
                            "proctoring": {"secureBrowserRequiredToTake": True, "webcamRequired": False}},
         "description": "<p>Covers <b>weeks 8-10</b>. Bring a calculator.</p>"},
    ]
    pages = find_ultra_pages(tree)
    items = [it for raw in tree if (it := to_content_item(raw, "XX1000", "_1_1", pages, release_from_public(raw)))]
    print(f"1) {len(tree)} raw items -> {len(items)} content items (the page body is shown through its folder):")
    for it in items:
        files = [(f.name, f.xid, f.has_link) for f in it.embedded_files]
        print(f"   {it.kind:10} {it.title!r:22} release={it.release.state:11} column={it.column_id} files={files}")

    internal = {"visibility": "PARTIALLY_VISIBLE", "adaptiveReleaseRules": {"rules": [
        {"title": "Finish Quiz 9 first", "type": "User", "criteria": [{"type": "Pre_Content"}]},
        {"title": "Week 10 window", "type": "User",
         "criteria": [{"type": "Date_Range", "start": "2026-11-02T01:00:00.000Z", "end": "2026-11-09T15:59:00.000Z"}]},
    ]}}
    r = release_from_internal(internal)
    print(f"2) internal rules -> {r.state}, rule={r.rule!r}, opens {r.opens_sgt}, closes {r.closes_sgt}")
    print(f"   no rules -> {release_from_internal({'visibility': 'VISIBLE'}).state}; "
          f"rules met -> {release_from_internal({**internal, 'visibility': 'VISIBLE'}).state}")

    text, links = page_text(page_body(tree[0], pages))
    print("3) page text:", " / ".join(line for line in text.splitlines() if line.strip()))
    print("   links kept:", [(link.text, link.url) for link in links])

    print("4) assessment:", assessment_details(tree[3], {"grading": {"attemptsAllowed": 1, "due": "2026-11-06T15:59:00.000Z"}}, None))

    links_for_download = embedded_links(body)
    shown = repr(items) + text + repr(links) + repr(assessment_details(tree[3], None, None))
    print(f"5) signed links: {len(links_for_download)} kept for the download code only; "
          f"'SECRET' in anything shown: {'SECRET' in shown}")


if __name__ == "__main__":
    _main()
