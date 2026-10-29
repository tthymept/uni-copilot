"""
Phase 4 probe: learn the shape of NTULearn's course-content answers before
building on them. Live, GET only, your own account.

In plain words:
  The plan has things about course content that were never checked on your
  account: can one request list a whole course? What does a file download do?
  Do quizzes carry "opens / closes" dates? Can students read Discussions and
  Groups? This script asks NTULearn a few read-only questions per course site
  and prints only what the ANSWERS LOOK LIKE: how many items, which status
  codes, which field names, which item types. Never titles, bodies, file
  names, dates, ids or URLs. The results go into the plan's Appendix A, and
  the later Phase 4 steps are built on them.

What it asks, per course site (about 8-12 requests each, ~1 per second):
  1. the top level of the course content, and the same with recursive=true
     (one request for the whole tree, if NTULearn allows it)
  2. if recursive isn't allowed: one level down in up to 3 folders
  3. the attachment list of up to 2 file items
  4. Discussions and Groups (status and counts only)
Once in total:
  5. one PDF download: status, redirect hosts (host names only), headers.
     The file itself is not read: the connection is closed after the headers.
  6. up to 3 quizzes/assignments: their fields, and their gradebook column
  7. "viewed" check: one item's tracking fields before and after the download
     (changed yes/no; values never shown)

Run:  uv run python -m uni_copilot.ntulearn_mcp.probe_content [--sites N] [--no-download]
      ... --follow-up   (second run, 29 Oct: locked items, embedded files, attachment lists by kind)
      ... --follow-up-2 (third run, 29 Oct: release rules item by item, how embedded files download)
      ... --follow-up-3 (fourth run, 29 Oct: how embedded files' viewerUrl links are built, masked)

Rules: GET only (all requests go through NtuLearnHttp's session, which has no
other method); NTULearn's pace of ~1 request/s; nothing is saved to disk.
"""

import argparse
import html
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from uni_copilot.ntulearn_mcp.auth.session import AuthError
from uni_copilot.ntulearn_mcp.client.calendar import parse_time
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.client.http import BASE_HOST, NtuLearnError, NtuLearnHttp
from uni_copilot.ntulearn_mcp.config import BASE_URL, REQUEST_TIMEOUT_S, load_settings

PUBLIC = "/learn/api/public/v1/courses"
INTERNAL = "/learn/api/v1/courses"
PAGE_LIMIT = 200          # items per page asked for
MAX_PAGES = 10            # stop following nextPage after this many pages
MAX_REDIRECTS = 3         # how many download redirects we follow by hand
# Field names worth showing for assessments and the "viewed" check (names only).
ASSESSMENT_WORDS = ("attempt", "due", "time", "limit", "avail", "start", "end", "release", "grad", "column")
TRACKING_WORDS = ("view", "review", "progress", "read", "access", "track", "complet")


# Counts every request this probe sends, so the summary can say how many.
class Budget:
    def __init__(self) -> None:
        self.requests = 0


BUDGET = Budget()


# One GET through the shared NTULearn client. Returns (status, json body) and
# never raises for an HTTP error: an error status is exactly what we want to
# record. A sign-in problem still stops the probe (raised as AuthError).
def probe_get(http: NtuLearnHttp, path: str, params: dict[str, Any] | None = None) -> tuple[int | None, Any]:
    BUDGET.requests += 1
    try:
        return 200, http.get_json(path, params)
    except NtuLearnError as e:
        return e.status, None


# GETs a list endpoint and follows paging.nextPage, at most MAX_PAGES pages.
# Returns (status of the first page, all results, whether pages were left over).
# If NTULearn rejects our `limit`, tries once more without it.
def probe_list(http: NtuLearnHttp, path: str, params: dict[str, Any]) -> tuple[int | None, list[Any], bool]:
    status, body = probe_get(http, path, {**params, "limit": PAGE_LIMIT})
    if status == 400:
        status, body = probe_get(http, path, params)
    if body is None:
        return status, [], False
    results = list(body.get("results", []))
    next_path = (body.get("paging") or {}).get("nextPage")
    pages = 1
    while next_path and pages < MAX_PAGES:
        _, more = probe_get(http, next_path)
        if more is None:
            break
        results += more.get("results", [])
        next_path = (more.get("paging") or {}).get("nextPage")
        pages += 1
    return status, results, bool(next_path)


# Every field name in a JSON object as a dotted path, e.g.
# "availability.adaptiveRelease.start". Lists become "[]". Only the NAMES are
# returned, never values, so the result is safe to print.
def key_paths(obj: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            paths.add(p)
            paths |= key_paths(v, p)
    elif isinstance(obj, list):
        for v in obj[:3]:
            paths |= key_paths(v, prefix + "[]")
    return paths


# The value at a dotted path ("availability.adaptiveRelease.start"), or None.
# Used for counting and comparing inside the probe; values are never printed.
def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


# The item's content handler, e.g. "resource/x-bb-folder" (a type name, not content).
def handler(item: dict[str, Any]) -> str:
    return str(dig(item, "contentHandler.id") or "(none)")


# Counts how many of `items` have each field path, for printing.
def path_counts(items: list[dict[str, Any]]) -> Counter:
    c: Counter = Counter()
    for item in items:
        c.update(key_paths(item))
    return c


# Prints field paths with how many items had them. `words` keeps only paths
# containing one of those words (for long objects).
def show_paths(counts: Counter, indent: str = "     ", words: tuple[str, ...] | None = None) -> None:
    for path, n in sorted(counts.items()):
        if words and not any(w in path.lower() for w in words):
            continue
        print(f"{indent}{path} ({n})")


# A request to NTULearn sent by hand, without following redirects and without
# reading the body (stream=True, closed right after the headers). Only used
# for the download check: NtuLearnHttp.get_json would follow the redirect and
# then think "another host = signed out". Step 4 builds the proper version
# (get_file); this uses the client's own session (cookies) and pacer.
def raw_get_headers(http: NtuLearnHttp, url: str, with_cookies: bool) -> requests.Response:
    BUDGET.requests += 1
    http._pacer.wait()  # the probe borrows the client's pacer and session on purpose
    session = http._session if with_cookies else requests.Session()
    r = session.get(url, allow_redirects=False, stream=True, timeout=REQUEST_TIMEOUT_S)
    r.close()
    return r


# ---------- The checks ----------

# 1 + 2: the content tree of one site. Tries recursive=true first; if NTULearn
# refuses it, walks one level down into up to 3 folders instead.
# Returns the items found; what happened is counted in `stats` for the summary.
def probe_tree(http: NtuLearnHttp, site_id: str, stats: Counter) -> list[dict[str, Any]]:
    root_status, root, root_more = probe_list(http, f"{PUBLIC}/{site_id}/contents", {})
    stats[f"root status {root_status}"] += 1
    stats["root items"] += len(root)
    if root_more:
        stats["root had more pages than probed"] += 1

    rec_status, rec, rec_more = probe_list(http, f"{PUBLIC}/{site_id}/contents", {"recursive": "true"})
    stats[f"recursive status {rec_status}"] += 1
    if rec_status == 200 and len(rec) > len(root):
        stats["recursive returned more than root (works)"] += 1
        stats["recursive items"] += len(rec)
        if rec_more:
            stats["recursive had more pages than probed"] += 1
        return rec
    if rec_status == 200:
        stats["recursive returned same as root (ignored or flat course)"] += 1

    items = list(root)
    folders = [i for i in root if handler(i) == "resource/x-bb-folder"][:3]
    for folder in folders:
        status, children, _ = probe_list(http, f"{PUBLIC}/{site_id}/contents/{folder['id']}/children", {})
        stats[f"children status {status}"] += 1
        items += children
    return items


# Ultra "Document" pages: a folder whose only child is an x-bb-document
# titled "ultraDocumentBody". Titles are compared here, never printed.
def count_ultra_pages(items: list[dict[str, Any]], stats: Counter) -> None:
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        if i.get("parentId"):
            by_parent.setdefault(i["parentId"], []).append(i)
    for i in items:
        if handler(i) == "resource/x-bb-document":
            stats["documents"] += 1
            if i.get("title") == "ultraDocumentBody":
                stats["documents titled ultraDocumentBody"] += 1
            if i.get("body"):
                stats["documents with a non-empty body"] += 1
    for kids in by_parent.values():
        if len(kids) == 1 and kids[0].get("title") == "ultraDocumentBody":
            stats["folders that are Ultra pages (one ultraDocumentBody child)"] += 1


# Availability: Yes / No / PartiallyVisible counts, and date windows
# (adaptiveRelease start/end): how many, on which item types, and how many
# are still in the future. No dates are printed.
def count_windows(items: list[dict[str, Any]], stats: Counter, window_kinds: Counter) -> None:
    now = datetime.now(timezone.utc)
    for i in items:
        stats[f"availability.available = {dig(i, 'availability.available')}"] += 1
        start = parse_time(dig(i, "availability.adaptiveRelease.start"))
        end = parse_time(dig(i, "availability.adaptiveRelease.end"))
        if start or end:
            stats["items with a date window"] += 1
            window_kinds[handler(i)] += 1
        if start:
            stats["  with a start (opens)"] += 1
            stats["  start still in the future"] += start > now
        if end:
            stats["  with an end (closes)"] += 1
            stats["  end still in the future"] += end > now
        if dig(i, "availability.available") == "PartiallyVisible":
            stats["locked (PartiallyVisible)"] += 1
            stats["  locked with an empty body"] += not i.get("body")
            stats["  locked with a date window"] += bool(start or end)


# 3: the attachment lists of up to 2 file items on this site.
# Returns the attachments found (with the item they belong to) for step 5.
def probe_attachments(http: NtuLearnHttp, site_id: str, items: list[dict[str, Any]],
                      stats: Counter, fields: Counter, mimes: Counter) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    files = [i for i in items if handler(i) in ("resource/x-bb-file", "resource/x-bb-document")
             and i.get("title") != "ultraDocumentBody"][:2]
    for item in files:
        status, atts, _ = probe_list(http, f"{PUBLIC}/{site_id}/contents/{item['id']}/attachments", {})
        stats[f"attachments status {status}"] += 1
        fields.update(path_counts(atts))
        for a in atts:
            mimes[str(a.get("mimeType") or "(none)")] += 1
            found.append((site_id, item["id"], a))
    return found


# 4: Discussions and Groups: which endpoints answer, and with how many items.
def probe_discussions_groups(http: NtuLearnHttp, site_id: str, first: bool,
                             stats: Counter, fields: Counter) -> None:
    endpoints = [("discussions (public v1)", f"{PUBLIC}/{site_id}/discussions"),
                 ("groups (public v2)", f"/learn/api/public/v2/courses/{site_id}/groups")]
    if first:  # less common forms: only on the first site, to keep the request count down
        endpoints += [("groups (public v1)", f"{PUBLIC}/{site_id}/groups"),
                      ("discussions (internal v1)", f"{INTERNAL}/{site_id}/discussions")]
    for name, path in endpoints:
        status, results, _ = probe_list(http, path, {})
        stats[f"{name}: status {status}"] += 1
        stats[f"{name}: items"] += len(results)
        fields.update({f"{name}: {p}": n for p, n in path_counts(results).items()})


# 6: up to 3 quizzes/assignments: their contentHandler fields, whether they
# point at a gradebook column, and which fields that column and the internal
# content item have about attempts, times and availability (names only).
def probe_assessments(http: NtuLearnHttp, items_by_site: dict[str, list[dict[str, Any]]]) -> None:
    picks = [(site, i) for site, items in items_by_site.items() for i in items
             if any(w in handler(i) for w in ("asmt", "assignment", "assessment", "test"))][:3]
    handlers = Counter(handler(i) for items in items_by_site.values() for i in items
                       if any(w in handler(i) for w in ("asmt", "assignment", "assessment", "test")))
    with_column = sum(1 for items in items_by_site.values() for i in items
                      if dig(i, "contentHandler.gradeColumnId"))
    print(f"6) Assessments: {sum(handlers.values())} items; handlers {dict(handlers)}; "
          f"with contentHandler.gradeColumnId: {with_column}")
    for n, (site, item) in enumerate(picks, 1):
        print(f"   pick {n}: {handler(item)}; contentHandler fields: {sorted(key_paths(item.get('contentHandler') or {}))}")
        column_id = dig(item, "contentHandler.gradeColumnId")
        if column_id:
            status, col = probe_get(http, f"/learn/api/public/v2/courses/{site}/gradebook/columns/{column_id}")
            print(f"     gradebook column: status {status}")
            if col:
                show_paths(Counter(key_paths(col)), "       ", ASSESSMENT_WORDS)
        status, internal = probe_get(http, f"{INTERNAL}/{site}/contents/{item['id']}")
        print(f"     internal content item: status {status}")
        if internal:
            show_paths(Counter(key_paths(internal)), "       ", ASSESSMENT_WORDS)


# 5 + 7: one PDF download, headers only, plus the "viewed" check on the item
# it belongs to. Prints status codes, host names and yes/no; never the URL,
# the file name or any value.
def probe_download(http: NtuLearnHttp, attachments: list[tuple[str, str, dict[str, Any]]]) -> None:
    pdfs = [a for a in attachments if a[2].get("mimeType") == "application/pdf"]
    if not pdfs:
        print("5) Download: no PDF attachment found among those probed; skipped.")
        return
    site, item_id, att = pdfs[0]

    # 7, before: the internal item's tracking-looking fields (compared later).
    s_before, before = probe_get(http, f"{INTERNAL}/{site}/contents/{item_id}")
    tracking = sorted(p for p in key_paths(before or {}) if any(w in p.lower() for w in TRACKING_WORDS))

    url = f"{BASE_URL}{PUBLIC}/{site}/contents/{item_id}/attachments/{att['id']}/download"
    print("5) Download (headers only, body not read):")
    follow_download(http, url)

    # 7, after.
    s_after, after = probe_get(http, f"{INTERNAL}/{site}/contents/{item_id}")
    changed = [p for p in tracking if dig(before, p) != dig(after, p)]
    print(f"7) 'Viewed' check (internal item, status {s_before}/{s_after}): tracking-looking fields {tracking or 'none'}; "
          f"changed after the download: {changed or 'none'}")


# Follows a download's redirects by hand, headers only, and prints what each
# hop looked like: status, host name, whether the link carries a query string
# (signed), whether it worked without NTULearn cookies. Never the URL itself.
# Used by probe_download (attachments) and the second follow-up (embedded files).
def follow_download(http: NtuLearnHttp, url: str) -> None:
    chain: list[str] = []
    r = raw_get_headers(http, url, with_cookies=True)
    print(f"   first answer {r.status_code}")
    hops = 0
    while r.is_redirect and hops < MAX_REDIRECTS:
        hops += 1
        target = urljoin(url, r.headers.get("Location", ""))
        host = urlparse(target).hostname or "(none)"
        chain.append(host)
        print(f"   redirect {hops}: -> host {host}; link has a query string (signed?): {bool(urlparse(target).query)}")
        same_host = host == BASE_HOST
        r = raw_get_headers(http, target, with_cookies=same_host)
        print(f"   that host {'with' if same_host else 'WITHOUT'} NTULearn cookies: {r.status_code}")
        if not same_host and r.status_code in (401, 403):
            r = raw_get_headers(http, target, with_cookies=True)
            print(f"   same host with the cookie jar: {r.status_code}")
        url = target
    size = r.headers.get("Content-Length")
    print(f"   final: status {r.status_code}, Content-Type {r.headers.get('Content-Type', '(none)').split(';')[0]}, "
          f"Content-Length present: {size is not None}"
          + (f" (~{int(size) / 1_000_000:.1f} MB)" if size and size.isdigit() else ""))
    print(f"   hosts in the chain: {chain or ['(no redirect)']}")


# ---------- Follow-up (--follow-up), after the first run on 29 Oct ----------
#
# The first run found: recursive=true lists a whole course; no item has an
# availability date window; 221 items are "PartiallyVisible"; the attachment
# list answered 400 for 4 items (which kind wasn't recorded). These checks
# look closer, ~40 requests in total.

# Words for release-rule fields in the internal item (outside "permissions.",
# which only says what YOU may do, e.g. permissions.adaptiveRelease.edit).
RELEASE_WORDS = ("releas", "rule", "criteri", "condition", "visib", "avail", "lock", "start", "end")
BBFILE_RE = re.compile(r"""data-bbfile=(["'])(.*?)\1""", re.S)
BBTYPE_RE = re.compile(r"""data-bbtype=["']([^"']+)["']""")


# Files embedded inside page bodies (the same <a data-bbfile='{...}'> markup
# announcements use): how many pages have them, their data-bbtype values and
# mime types. Slides may live here rather than as separate file items.
def count_embedded(items: list[dict[str, Any]], stats: Counter, kinds: Counter, mimes: Counter) -> None:
    for i in items:
        body = i.get("body") or ""
        if handler(i) != "resource/x-bb-document" or not body:
            continue
        found = BBFILE_RE.findall(body)
        kinds.update(BBTYPE_RE.findall(body))
        if found:
            stats["pages with embedded files"] += 1
            stats["embedded files"] += len(found)
        for _, raw in found:
            try:
                meta = json.loads(html.unescape(raw))
            except ValueError:
                stats["embedded file JSON not readable"] += 1
                continue
            mimes[str(meta.get("mimeType") or "(none)")] += 1


# What the PartiallyVisible items are: by item type, how many have an empty
# body, and how many items sit inside a PartiallyVisible folder (if the
# recursive list shows their children, they aren't really locked away).
def count_partially_visible(items: list[dict[str, Any]], stats: Counter, kinds: Counter, empty: Counter) -> None:
    pv_ids = {i["id"] for i in items if dig(i, "availability.available") == "PartiallyVisible"}
    for i in items:
        if i["id"] in pv_ids:
            kinds[handler(i)] += 1
            if not i.get("body"):
                empty[handler(i)] += 1
        if i.get("parentId") in pv_ids:
            stats["items inside a PartiallyVisible parent"] += 1
            stats["  of them PartiallyVisible too"] += i["id"] in pv_ids


# The follow-up run: whole trees again (recursive), then a few targeted GETs:
# attachment lists by item kind, children and attachments of
# PartiallyVisible items, and the internal item's release-rule field names.
def run_follow_up(http: NtuLearnHttp, sites: list[tuple[str, str]]) -> None:
    stats: Counter = Counter()
    pv_kinds: Counter = Counter()
    pv_empty: Counter = Counter()
    emb_kinds: Counter = Counter()
    emb_mimes: Counter = Counter()
    all_items: list[tuple[str, dict[str, Any]]] = []
    for n, (code, site_id) in enumerate(sites, 1):
        print(f"   site {n}/{len(sites)} ({code}) ...", file=sys.stderr)
        _, items, more = probe_list(http, f"{PUBLIC}/{site_id}/contents", {"recursive": "true"})
        stats["sites with pages left over"] += more
        all_items += [(site_id, i) for i in items]
        count_partially_visible(items, stats, pv_kinds, pv_empty)
        count_embedded(items, stats, emb_kinds, emb_mimes)

    print(f"A) Whole trees (recursive): {len(all_items)} items; sites with pages left over: {stats['sites with pages left over']}")
    print(f"B) PartiallyVisible by item type: {dict(pv_kinds.most_common())}")
    print(f"   ... with an empty body: {dict(pv_empty.most_common())}")
    print(f"   items inside a PartiallyVisible parent: {stats['items inside a PartiallyVisible parent']} "
          f"(PartiallyVisible too: {stats['  of them PartiallyVisible too']})")
    print(f"C) Files embedded in page bodies: {stats['embedded files']} on {stats['pages with embedded files']} pages; "
          f"data-bbtype values {dict(emb_kinds.most_common())}; mime types {dict(emb_mimes.most_common())}"
          + (f"; unreadable JSON {stats['embedded file JSON not readable']}" if stats["embedded file JSON not readable"] else ""))

    # D) Which kinds of item have an attachment list (the 400s of the first run).
    def is_pv(i: dict[str, Any]) -> bool:
        return dig(i, "availability.available") == "PartiallyVisible"

    groups = {
        "x-bb-file": [x for x in all_items if handler(x[1]) == "resource/x-bb-file" and not is_pv(x[1])][:3],
        "x-bb-file, PartiallyVisible": [x for x in all_items if handler(x[1]) == "resource/x-bb-file" and is_pv(x[1])][:2],
        "x-bb-document (ultraDocumentBody)": [x for x in all_items if handler(x[1]) == "resource/x-bb-document"
                                              and x[1].get("title") == "ultraDocumentBody"][:3],
        "x-bb-document (other)": [x for x in all_items if handler(x[1]) == "resource/x-bb-document"
                                  and x[1].get("title") != "ultraDocumentBody"][:3],
    }
    print("D) Attachment list status by item kind:")
    for name, picks in groups.items():
        statuses: Counter = Counter()
        count = 0
        for site_id, item in picks:
            status, atts, _ = probe_list(http, f"{PUBLIC}/{site_id}/contents/{item['id']}/attachments", {})
            statuses[status] += 1
            count += len(atts)
        print(f"     {name}: {dict(statuses) or 'none to try'}, attachments {count}")

    # E) Can a PartiallyVisible folder be opened?
    pv_folders = [x for x in all_items if handler(x[1]) == "resource/x-bb-folder" and is_pv(x[1])][:3]
    statuses = Counter()
    for site_id, item in pv_folders:
        status, _, _ = probe_list(http, f"{PUBLIC}/{site_id}/contents/{item['id']}/children", {})
        statuses[status] += 1
    print(f"E) Children of PartiallyVisible folders: {dict(statuses) or 'none to try'}")

    # F) Release-rule fields on the internal item, one per PartiallyVisible kind.
    seen: set[str] = set()
    print("F) Internal item fields about release rules (outside 'permissions.'), one PartiallyVisible item per kind:")
    for site_id, item in all_items:
        if not is_pv(item) or handler(item) in seen or len(seen) >= 4:
            continue
        seen.add(handler(item))
        status, internal = probe_get(http, f"{INTERNAL}/{site_id}/contents/{item['id']}")
        paths = sorted(p for p in key_paths(internal or {})
                       if not p.startswith("permissions.") and any(w in p.lower() for w in RELEASE_WORDS))
        print(f"     {handler(item)}: status {status}; fields {paths or 'none'}")


# ---------- Second follow-up (--follow-up-2), after the follow-up on 29 Oct ----------
#
# The follow-up found: release rules with start/end criteria live on the
# INTERNAL item (adaptiveReleaseRules), one request per item; and 283 files
# are embedded in page bodies (126 PDFs), not separate file items. These
# checks count what the rules contain and how embedded files are reached.
# ~150-200 requests (one per PartiallyVisible item, capped).

MAX_RULE_ITEMS = 150      # internal item requests for release rules, at most
TAG_RE = re.compile(r"<(\w+)")


# Release rules of one internal item, counted: rule and criterion types (enum
# words like DATE), how many criteria have a start / end, and whether those
# are past, open now or still to come. Rule titles and dates are never printed.
def count_rules(internal: dict[str, Any], kind: str, stats: Counter, types: Counter, now: datetime) -> None:
    rules = dig(internal, "adaptiveReleaseRules.rules") or []
    stats[f"{kind}: with rules"] += bool(rules)
    for rule in rules:
        types[f"rule.type={rule.get('type')}"] += 1
        for crit in rule.get("criteria") or []:
            types[f"criterion.type={crit.get('type')}"] += 1
            stats["criteria negated"] += bool(crit.get("negate"))
            start, end = parse_time(crit.get("start")), parse_time(crit.get("end"))
            if start or end:
                stats["date criteria"] += 1
                stats["  with a start"] += bool(start)
                stats["  with an end"] += bool(end)
                if start and start > now:
                    stats["  not open yet (start in the future)"] += 1
                elif end and end < now:
                    stats["  closed (end in the past)"] += 1
                else:
                    stats["  open now"] += 1
                stats["  end still to come"] += bool(end and end > now)
    types[f"visibility={internal.get('visibility')}"] += 1
    codes = dig(internal, "genericReadOnlyData.restrictions.contentVisibility.partiallyVisible.reasonCodes") or []
    types.update(f"reasonCode={c}" for c in codes if isinstance(c, str))


# Embedded files in page bodies: the tag and data-bbtype around each
# data-bbfile, the JSON field names, file extensions (".pdf", never the
# name), and the shape of resourceUrl (host, first path segment, query or
# not; never the URL). Returns resourceUrls of PDFs for the download check.
def survey_embedded(items: list[tuple[str, dict[str, Any]]], stats: Counter, shapes: Counter,
                    fields: Counter) -> list[str]:
    pdf_urls: list[str] = []
    for _, item in items:
        body = item.get("body") or ""
        for m in BBFILE_RE.finditer(body):
            start = body.rfind("<", 0, m.start())
            end = body.find(">", m.end())
            tag_text = body[start:end + 1] if start >= 0 else ""
            tag = TAG_RE.match(tag_text)
            bbtype = BBTYPE_RE.search(tag_text)
            shapes[f"tag=<{tag.group(1) if tag else '?'}> data-bbtype={bbtype.group(1) if bbtype else '(none)'}"] += 1
            try:
                meta = json.loads(html.unescape(m.group(2)))
            except ValueError:
                stats["JSON not readable"] += 1
                continue
            fields.update(key_paths(meta))
            name = str(meta.get("fileName") or meta.get("linkName") or meta.get("displayName") or "")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "(none)"
            shapes[f"extension=.{ext}"] += 1
            url = meta.get("resourceUrl")
            if not isinstance(url, str) or not url:
                stats["no resourceUrl"] += 1
                continue
            full = urljoin(BASE_URL + "/", url)
            parts = urlparse(full)
            segment = parts.path.strip("/").split("/")[0] if parts.path.strip("/") else "(root)"
            shapes[f"resourceUrl: host={parts.hostname}, absolute={url.startswith('http')}, "
                   f"first segment=/{segment}, query={bool(parts.query)}"] += 1
            if ext == "pdf" or meta.get("mimeType") == "application/pdf":
                pdf_urls.append(full)
    return pdf_urls


# The second follow-up run: release rules on PartiallyVisible items (and a
# few visible ones, for comparison), two guesses at a list request that
# includes the rules, and how embedded files are described and downloaded.
def run_follow_up_2(http: NtuLearnHttp, sites: list[tuple[str, str]]) -> None:
    all_items: list[tuple[str, dict[str, Any]]] = []
    for n, (code, site_id) in enumerate(sites, 1):
        print(f"   trees: site {n}/{len(sites)} ({code}) ...", file=sys.stderr)
        _, items, _ = probe_list(http, f"{PUBLIC}/{site_id}/contents", {"recursive": "true"})
        all_items += [(site_id, i) for i in items]
    print(f"A) Whole trees: {len(all_items)} items")

    # G1) A list request that includes the rules would save one request per item.
    site0 = sites[0][1]
    print("G1) Internal list with rules (first site):")
    for params in ({}, {"expand": "adaptiveReleaseRules"}):
        status, results, _ = probe_list(http, f"{INTERNAL}/{site0}/contents/ROOT/children", params)
        with_rules = sum(1 for r in results if isinstance(r, dict) and "adaptiveReleaseRules" in r)
        print(f"     ROOT/children {params or '(no expand)'}: status {status}, items {len(results)}, "
              f"items carrying adaptiveReleaseRules {with_rules}")

    # G2) Rules item by item. Ultra page bodies (ultraDocumentBody) are left
    # out: they sit inside their page's folder, which carries the rule.
    now = datetime.now(timezone.utc)
    pv = [x for x in all_items if dig(x[1], "availability.available") == "PartiallyVisible"
          and x[1].get("title") != "ultraDocumentBody"]
    visible = [x for x in all_items if dig(x[1], "availability.available") == "Yes"]
    sample_visible = [next(x for x in visible if handler(x[1]) == h) for h in
                      {handler(x[1]) for x in visible}][:5]
    stats: Counter = Counter()
    types: Counter = Counter()
    picks = pv[:MAX_RULE_ITEMS]
    print(f"G2) Release rules: {len(pv)} PartiallyVisible items (not page bodies), checking {len(picks)}; "
          f"plus {len(sample_visible)} visible items for comparison", file=sys.stderr)
    for n, (site_id, item) in enumerate(picks + sample_visible, 1):
        if n % 25 == 0:
            print(f"   rules: {n}/{len(picks) + len(sample_visible)} ...", file=sys.stderr)
        status, internal = probe_get(http, f"{INTERNAL}/{site_id}/contents/{item['id']}")
        kind = "PartiallyVisible" if n <= len(picks) else "visible"
        stats[f"{kind}: status {status}"] += 1
        if internal:
            count_rules(internal, kind, stats, types if kind == "PartiallyVisible" else Counter(), now)
    print(f"G2) Release rules on {len(picks)} PartiallyVisible items (of {len(pv)}) and {len(sample_visible)} visible ones:")
    for k, v in sorted(stats.items()):
        print(f"     {k}: {v}")
    print(f"   types (PartiallyVisible items): {dict(types.most_common())}")

    # H) Embedded files.
    e_stats: Counter = Counter()
    shapes: Counter = Counter()
    fields: Counter = Counter()
    pdf_urls = survey_embedded([x for x in all_items if handler(x[1]) == "resource/x-bb-document"],
                               e_stats, shapes, fields)
    print("H) Embedded files in page bodies:")
    for k, v in sorted(shapes.items()):
        print(f"     {k}: {v}")
    for k, v in sorted(e_stats.items()):
        print(f"     {k}: {v}")
    print("   JSON fields (how many files had each):")
    show_paths(fields)
    if pdf_urls:
        print("H2) One embedded PDF, headers only (body not read):")
        try:
            follow_download(http, pdf_urls[0])
        except requests.RequestException as e:
            print(f"   network error ({type(e).__name__})")
    else:
        print("H2) No embedded PDF with a resourceUrl; download check skipped.")


# ---------- Third follow-up (--follow-up-3), after the second on 29 Oct ----------
#
# The second follow-up found: only 39 of 283 embedded files have a
# resourceUrl (those download like attachments); 158 have only a viewerUrl.
# These checks show how viewerUrl links are built (masked) and what one gives.
# ~20 requests.

# A link with everything identifying taken out: digits -> N, a path part with
# a dot (a file name) -> <name>.ext, very long parts -> <long>, and only the
# NAMES of query parameters. E.g. "/bbcswebdav/xid-N_N?<query: a, b>".
def masked_shape(url: str) -> str:
    parts = urlparse(url)
    segments = []
    for seg in parts.path.split("/"):
        if "." in seg:
            seg = "<name>." + seg.rsplit(".", 1)[-1].lower()[:6]
        elif len(seg) > 24:
            seg = "<long>"
        segments.append(re.sub(r"\d+", "N", seg))
    keys = sorted({k.split("=", 1)[0] for k in parts.query.split("&") if k})
    return f"{parts.hostname}{'/'.join(segments)}" + (f" ?<query: {', '.join(keys)}>" if keys else "")


# The third follow-up run: for embedded files, which fields come together per
# kind of tag, the masked shapes of viewerUrl and resourceUrl, and one
# viewerUrl-only PDF followed by hand (headers only).
def run_follow_up_3(http: NtuLearnHttp, sites: list[tuple[str, str]]) -> None:
    bodies: list[str] = []
    for n, (code, site_id) in enumerate(sites, 1):
        print(f"   trees: site {n}/{len(sites)} ({code}) ...", file=sys.stderr)
        _, items, _ = probe_list(http, f"{PUBLIC}/{site_id}/contents", {"recursive": "true"})
        bodies += [i.get("body") or "" for i in items if handler(i) == "resource/x-bb-document"]

    field_sets: Counter = Counter()
    shapes: Counter = Counter()
    viewer_pdfs: list[str] = []
    for body in bodies:
        for m in BBFILE_RE.finditer(body):
            start = body.rfind("<", 0, m.start())
            bbtype = BBTYPE_RE.search(body[start:body.find(">", m.end()) + 1] if start >= 0 else "")
            kind = bbtype.group(1) if bbtype else "(none)"
            try:
                meta = json.loads(html.unescape(m.group(2)))
            except ValueError:
                continue
            has = [k for k in ("resourceUrl", "viewerUrl", "url", "fileSize", "mimeType") if meta.get(k)]
            field_sets[f"data-bbtype={kind}: {has or ['none of these']}"] += 1
            for key in ("viewerUrl", "resourceUrl"):
                if isinstance(meta.get(key), str) and meta[key]:
                    shapes[f"{key} ({kind}): {masked_shape(urljoin(BASE_URL + '/', meta[key]))}"] += 1
            if meta.get("viewerUrl") and not meta.get("resourceUrl") and meta.get("mimeType") == "application/pdf":
                viewer_pdfs.append(urljoin(BASE_URL + "/", meta["viewerUrl"]))

    print(f"I) Embedded files, which link fields they carry, by data-bbtype ({len(bodies)} pages):")
    for k, v in sorted(field_sets.items()):
        print(f"     {k}: {v}")
    print("J) Link shapes (digits -> N, file names -> <name>.ext, query parameter names only):")
    for k, v in shapes.most_common():
        print(f"     {k}: {v}")
    if viewer_pdfs:
        print("K) One viewerUrl-only PDF, followed by hand (headers only, body not read):")
        try:
            follow_download(http, viewer_pdfs[0])
        except requests.RequestException as e:
            print(f"   network error ({type(e).__name__})")
    else:
        print("K) No viewerUrl-only PDF found; skipped.")


# Runs every check and prints the summary. Stops early, with a plain message,
# if you're not signed in. follow_up=1, 2 or 3 runs only that follow-up's checks.
def run(max_sites: int | None, download: bool, follow_up: int = 0) -> None:
    http = NtuLearnHttp()
    try:
        http.ensure_signed_in()
        courses = CoursesClient(http, load_settings()).list_courses().courses
    except AuthError as e:
        print("Not signed in:", e, "- run the ntulearn `login` tool, then try again.")
        return
    BUDGET.requests += 2  # users/me + memberships
    sites = [(c.code, s.id) for c in courses for s in c.sites][:max_sites]
    if follow_up:
        t0 = time.monotonic()
        about = len(sites) + {1: 25, 2: MAX_RULE_ITEMS + 15, 3: 5}[follow_up]
        print(f"Follow-up {follow_up} on {len(sites)} course sites (up to about {about} requests at ~1/s; "
              "counts and field names only)\n")
        {1: run_follow_up, 2: run_follow_up_2, 3: run_follow_up_3}[follow_up](http, sites)
        print(f"\nDone: {BUDGET.requests} requests in {time.monotonic() - t0:.0f} s. Nothing was saved.")
        return
    print(f"Probing {len(sites)} course sites of {len(courses)} subjects "
          f"(about {len(sites) * 10} requests at ~1/s; counts and field names only)\n")

    t0 = time.monotonic()
    tree_stats: Counter = Counter()
    window_stats: Counter = Counter()
    window_kinds: Counter = Counter()
    att_stats: Counter = Counter()
    att_fields: Counter = Counter()
    mimes: Counter = Counter()
    dg_stats: Counter = Counter()
    dg_fields: Counter = Counter()
    items_by_site: dict[str, list[dict[str, Any]]] = {}
    attachments: list[tuple[str, str, dict[str, Any]]] = []

    for n, (code, site_id) in enumerate(sites, 1):
        print(f"   site {n}/{len(sites)} ({code}) ...", file=sys.stderr)
        items = probe_tree(http, site_id, tree_stats)
        items_by_site[site_id] = items
        count_ultra_pages(items, tree_stats)
        count_windows(items, window_stats, window_kinds)
        attachments += probe_attachments(http, site_id, items, att_stats, att_fields, mimes)
        probe_discussions_groups(http, site_id, n == 1, dg_stats, dg_fields)

    all_items = [i for items in items_by_site.values() for i in items]
    print(f"1) Content tree: {len(all_items)} items seen on {len(sites)} sites")
    for k, v in sorted(tree_stats.items()):
        print(f"     {k}: {v}")
    print(f"   item types (content handlers): {dict(Counter(handler(i) for i in all_items).most_common())}")
    print("   item fields (how many items had each):")
    show_paths(path_counts(all_items))

    print("2) Availability and date windows")
    for k, v in window_stats.items():
        print(f"     {k}: {v}")
    print(f"   date windows by item type: {dict(window_kinds)}")

    print("3) Attachments")
    for k, v in sorted(att_stats.items()):
        print(f"     {k}: {v}")
    print(f"   mime types: {dict(mimes.most_common())}")
    print("   attachment fields:")
    show_paths(att_fields)

    print("4) Discussions and Groups")
    for k, v in sorted(dg_stats.items()):
        print(f"     {k}: {v}")
    if dg_fields:
        print("   fields:")
        show_paths(dg_fields)

    if download:
        try:
            probe_download(http, attachments)
        except requests.RequestException as e:
            print(f"5) Download: network error ({type(e).__name__}); the rest of the probe still ran.")
    else:
        print("5) Download: skipped (--no-download). 7) 'Viewed' check: skipped.")
    probe_assessments(http, items_by_site)

    print(f"\nDone: {BUDGET.requests} requests in {time.monotonic() - t0:.0f} s. Nothing was saved.")


# Command line: uv run python -m uni_copilot.ntulearn_mcp.probe_content [--sites N] [--no-download] [--follow-up | --follow-up-2 | --follow-up-3]
def _main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 live probe of NTULearn course content (GET only, counts only).")
    ap.add_argument("--sites", type=int, default=None, help="probe only the first N course sites (default: all)")
    ap.add_argument("--no-download", action="store_true", help="skip the download and 'viewed' checks")
    ap.add_argument("--follow-up", action="store_true",
                    help="only the follow-up checks (locked items, embedded files, attachment lists by kind)")
    ap.add_argument("--follow-up-2", action="store_true",
                    help="only the second follow-up (release rules item by item, how embedded files download)")
    ap.add_argument("--follow-up-3", action="store_true",
                    help="only the third follow-up (how embedded files' viewerUrl links are built, masked)")
    args = ap.parse_args()
    # Warnings only: the client's info lines would name request paths with ids.
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
    follow_up = 3 if args.follow_up_3 else 2 if args.follow_up_2 else int(args.follow_up)
    try:
        run(args.sites, download=not args.no_download, follow_up=follow_up)
    except AuthError as e:
        print(f"Stopped: sign-in problem part-way ({e}). Run the ntulearn `login` tool, then try again.")


if __name__ == "__main__":
    _main()
