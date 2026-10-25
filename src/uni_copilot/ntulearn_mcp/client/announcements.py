"""
Recent announcements for this term's courses.

In plain words:
  Announcements = what your profs posted on each course site ("Quiz moved to
  Friday", "Tutorial 5 solutions uploaded"). Each site has its own list, so
  this asks every site (one call each), keeps the ones posted OR edited in the
  last N days, merges a post that appears on both the lecture and the
  tutorial site, and turns each body into light Markdown + links + files.

Verified facts this builds on (plan, Appendix A; checked 24 Oct, 16 sites):
- GET /learn/api/public/v1/courses/{id}/announcements returns `id`, `title`,
  `body` (HTML string), `created`, `modified`, `draft`, `availability`,
  `creator` (a user id, no name), `position`.
- Newest first by `created`. Default page size is 10 (4 sites needed 2-3
  pages); with limit=100 every site fits in one call (largest had 27).
- availability.duration.type is "Permanent" or "Restricted" (with start/end).
- `modified` is within 1 minute of `created` for unedited posts (63 of 83),
  so "edited" = modified more than 1 minute after created.

Untrusted: titles and bodies are written by other people. They're labelled
UNTRUSTED in the models; this code never acts on what they say.
"""

import argparse
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from uni_copilot.ntulearn_mcp.auth.session import AuthError
from uni_copilot.ntulearn_mcp.client.calendar import parse_time
from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError, NtuLearnHttp
from uni_copilot.ntulearn_mcp.models import Announcement, AnnouncementList, Link
from uni_copilot.ntulearn_mcp.parsers.html import parse_html

log = logging.getLogger(__name__)

MAX_DAYS = 180
# Longest body kept per announcement; longer ones are cut and marked. Only a
# safety net against huge pasted pages: on 25 Oct the longest of 114 posts was
# ~4000 characters (the old limit cut 1 of them), median ~770.
BODY_MAX_CHARS = 20_000


# Fetches announcements for the tool. Holds no cache: announcements should
# always be fresh, and Phase 2's local store will make repeat questions fast.
class AnnouncementsClient:
    # Keeps the shared HTTP and courses clients.
    def __init__(self, http: NtuLearnHttp, courses: CoursesClient):
        self._http = http
        self._courses = courses

    # The main entry point: announcements posted or edited in the last
    # `since_days` days, for this term's courses (or one, e.g. "IE2108"),
    # newest first. A site that fails becomes a note; the rest still come back.
    def recent(self, since_days: int = 21, course: str | None = None) -> AnnouncementList:
        if not 1 <= since_days <= MAX_DAYS:
            raise ValueError(f"since_days must be between 1 and {MAX_DAYS}")
        subjects = self._courses.pick(course)
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=since_days)
        items: list[Announcement] = []
        notes: list[str] = []
        hidden = 0
        for subject in subjects:
            for site in subject.sites:
                try:
                    raws = self._http.get_all(
                        f"/learn/api/public/v1/courses/{site.id}/announcements", {"limit": 100}
                    )
                except NtuLearnError as e:
                    notes.append(f"{subject.code} announcements unavailable on site {site.id} ({e.status}).")
                    continue
                for raw in raws:
                    if not visible_now(raw, now):
                        hidden += 1
                        continue
                    a = to_announcement(raw, subject.code, site.id)
                    if a is not None and is_recent(a, since):
                        items.append(a)
        if hidden:
            log.info("announcements: skipped %d draft/not-yet-visible/expired", hidden)
        merged = merge_duplicates(items)
        log.info("announcements: %d in %d day(s) (%d before merging)", len(merged), since_days, len(items))
        return AnnouncementList(since_days=since_days, announcements=merged, notes=notes)


# ---------- Pure functions (no network; tested offline in step 15) ----------

# False for drafts, and for "Restricted" posts outside their start/end window,
# i.e. ones you wouldn't see on the course page right now. (Whether NTULearn
# even sends those to students isn't verified; this is a safety net.)
def visible_now(raw: dict[str, Any], now: datetime) -> bool:
    if raw.get("draft"):
        return False
    duration = (raw.get("availability") or {}).get("duration") or {}
    if duration.get("type") == "Restricted":
        start, end = parse_time(duration.get("start")), parse_time(duration.get("end"))
        if (start and start > now) or (end and end < now):
            return False
    return True


# Turns one raw announcement into the Announcement model: parses the HTML
# body into Markdown, links and files. Returns None if it has no id or date.
def to_announcement(raw: dict[str, Any], course_code: str, site_id: str) -> Announcement | None:
    created = parse_time(raw.get("created"))
    if not raw.get("id") or created is None:
        return None
    parsed = parse_html(raw.get("body") if isinstance(raw.get("body"), str) else None, BODY_MAX_CHARS)
    return Announcement(
        id=raw["id"],
        course_code=course_code,
        course_site_ids=[site_id],
        title=(raw.get("title") or "").strip(),
        body=parsed.text,
        links=[Link(text=t, url=u) for t, u in parsed.links],
        files=parsed.files,
        created_at=created,
        modified_at=parse_time(raw.get("modified")),
    )


# True if the announcement was posted, or edited, on or after `since`.
# Edits count so a changed old post ("venue changed") still shows up.
def is_recent(a: Announcement, since: datetime) -> bool:
    return a.created_at >= since or (a.edited and a.modified_at is not None and a.modified_at >= since)


# The time that decides the order: the edit time if edited, else the post time.
def latest_time(a: Announcement) -> datetime:
    return a.modified_at if a.edited and a.modified_at else a.created_at


# Merges a post that appears on several sites of the same subject (same
# title and same body) into one item listing all its site ids. Newest first.
# (24 Oct: no such duplicates among your 83 posts, but profs do cross-post.)
def merge_duplicates(items: list[Announcement]) -> list[Announcement]:
    merged: dict[tuple[str, str, str], Announcement] = {}
    for a in sorted(items, key=latest_time, reverse=True):
        key = (a.course_code, a.title.lower(), a.body)
        if key in merged:
            first = merged[key]
            if a.course_site_ids[0] not in first.course_site_ids:
                merged[key] = first.model_copy(
                    update={"course_site_ids": first.course_site_ids + a.course_site_ids}
                )
        else:
            merged[key] = a
    return list(merged.values())


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.announcements
# Live, GET only. Prints counts, codes and dates; never titles or bodies.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of announcements (GET only).").parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    from uni_copilot.ntulearn_mcp.config import load_settings

    http = NtuLearnHttp()
    client = AnnouncementsClient(http, CoursesClient(http, load_settings()))
    try:
        http.ensure_signed_in()
    except AuthError as e:
        print("Not signed in:", e)
        return

    t0 = time.monotonic()
    r = client.recent(since_days=21)
    print(f"1) last 21 days: {len(r.announcements)} announcements ({time.monotonic() - t0:.0f} s)")
    for a in r.announcements:
        print(f"   {a.course_code:7} {a.created_sgt}  edited={a.edited!s:5} "
              f"lines={a.body.count(chr(10)) + 1:3} links={len(a.links)} files={len(a.files)} sites={len(a.course_site_ids)}")
    print(f"   notes: {r.notes or 'none'}")

    r = client.recent(since_days=MAX_DAYS)
    anns = r.announcements
    print(f"2) last {MAX_DAYS} days: {len(anns)} announcements, per course {dict(Counter(a.course_code for a in anns))}")
    print(f"   edited: {sum(a.edited for a in anns)}, with links: {sum(bool(a.links) for a in anns)}, "
          f"with files: {sum(bool(a.files) for a in anns)} ({sum(len(a.files) for a in anns)} files), "
          f"with bold: {sum('**' in a.body for a in anns)}, with table rows: {sum(' | ' in a.body for a in anns)}, "
          f"truncated: {sum(a.body.endswith('[truncated]') for a in anns)}, empty body: {sum(not a.body for a in anns)}")
    try:
        client.recent(since_days=7, course="zzzz")
    except ValueError as e:
        print("3) course='zzzz' ->", str(e).split(".")[0])


if __name__ == "__main__":
    _main()
