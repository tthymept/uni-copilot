"""
Your courses: which sites you're in, which term is current, and which sites
belong to the same subject.

Verified facts this builds on (plan, Appendix A):
- GET /learn/api/v1/users/{userId}/memberships?expand=course&limit=100 lists every
  site you're a member of, each with its course and term embedded.
- Terms have names (26S1, 25S2, Non-curriculum) but no dates, so the current
  term is the newest YYS1/YYS2 name, unless NTULEARN_TERM overrides it.
- displayId looks like 26S1-<CODE>-<SECTION>, so one subject = several sites
  (e.g. a lecture site and your tutorial site).

The pure functions at the bottom (no network) do the thinking; CoursesClient
only fetches and caches. That split makes the logic easy to test offline.
"""

import argparse
import logging
import re
import threading
import time
from typing import Any

from uni_copilot.ntulearn_mcp.client.http import ME_PATH, NtuLearnHttp
from uni_copilot.ntulearn_mcp.config import Settings, load_settings
from uni_copilot.ntulearn_mcp.models import Course, CourseList, CourseSite

log = logging.getLogger(__name__)

# Your memberships rarely change during a day, so keep them this long in memory.
MEMBERSHIPS_TTL_S = 60 * 60
TERM_RE = re.compile(r"^(\d{2})S([12])$")


# Fetches and caches your user id and memberships. Everything else in the
# project asks this class "what are my courses?" instead of calling the API.
class CoursesClient:
    # Keeps the shared HTTP client and settings; the cache starts empty.
    def __init__(self, http: NtuLearnHttp, settings: Settings):
        self._http = http
        self._settings = settings
        self._lock = threading.Lock()
        self._user_id: str | None = None
        self._raw_courses: list[dict[str, Any]] = []
        self._fetched_at = 0.0

    # Your NTULearn user id (like _123456_1). Needed by memberships and the
    # gradebook. Asked once from users/me, then remembered.
    def user_id(self) -> str:
        with self._lock:
            if self._user_id is None:
                self._user_id = self._http.get_json(ME_PATH)["id"]
            return self._user_id

    # The raw course objects for every site you're in, from memberships
    # (cached for MEMBERSHIPS_TTL_S). Follows paging if there are >100.
    def raw_courses(self) -> list[dict[str, Any]]:
        uid = self.user_id()
        with self._lock:
            if self._raw_courses and time.monotonic() - self._fetched_at < MEMBERSHIPS_TTL_S:
                return self._raw_courses
            memberships = self._http.get_all(
                f"/learn/api/v1/users/{uid}/memberships", {"expand": "course", "limit": 100}
            )
            courses = [m["course"] for m in memberships if m.get("course")]
            self._raw_courses, self._fetched_at = courses, time.monotonic()
            log.info("memberships: %d sites", len(courses))
            return courses

    # The term to use: NTULEARN_TERM if set, else the newest YYS1/YYS2 name.
    def current_term(self) -> str:
        return pick_current_term(
            [term_name(c) for c in self.raw_courses()], self._settings.term_override
        )

    # Subjects for a term (default: current), each with all its sites.
    # include_organizations adds sites that aren't semester courses: organizations
    # and NTU's "Non-curriculum" sites (training modules etc.), whatever their term.
    def list_courses(self, term: str | None = None, include_organizations: bool = False) -> CourseList:
        term = term or self.current_term()
        raw = self.raw_courses()
        courses = group_sites(raw, term)
        notes = []
        if include_organizations:
            courses += other_sites(raw)
        hidden = sum(1 for c in raw if term_name(c) == term and not c.get("isAvailable", True))
        if hidden:
            notes.append(f"{hidden} site(s) in {term} are unavailable to students and were left out.")
        return CourseList(term=term, courses=courses, notes=notes)

    # Turns what you typed ("IE2108", "ie2108", "data structures") into
    # matching subjects this term. Can return 0, 1 or several.
    def find(self, query: str, term: str | None = None) -> list[Course]:
        return match_courses(self.list_courses(term).courses, query)

    # What every tool's `course` argument goes through: None -> all of this
    # term's subjects; text -> exactly one subject. 0 or 2+ matches raise
    # ValueError listing the codes, which the tool passes on to Claude.
    def pick(self, course: str | None) -> list[Course]:
        all_courses = self.list_courses().courses
        if course is None:
            return all_courses
        found = match_courses(all_courses, course)
        if not found:
            codes = ", ".join(c.code for c in all_courses)
            raise ValueError(f"No course matches '{course}'. This term: {codes}")
        if len(found) > 1:
            raise ValueError(f"'{course}' matches several courses: {', '.join(c.code for c in found)}")
        return found


# ---------- Pure functions (no network; tested offline in step 15) ----------

# The term name of a raw course, or "" if it has none.
def term_name(course: dict[str, Any]) -> str:
    return ((course.get("term") or {}).get("name") or "").strip()


# Sort key for term names: 26S1 -> (26, 1). Anything else sorts lowest.
def term_order(name: str) -> tuple[int, int]:
    m = TERM_RE.match(name)
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


# Chooses the current term: the override if given, else the newest YYS1/YYS2
# name among your sites. Raises if there's no such term at all.
def pick_current_term(names: list[str], override: str | None = None) -> str:
    if override:
        return override
    semesters = [n for n in set(names) if TERM_RE.match(n)]
    if not semesters:
        raise ValueError("No term like 26S1 found in your courses. Set NTULEARN_TERM.")
    return max(semesters, key=term_order)


# The subject code inside a displayId: "26S1-IE2108-LEC" -> "IE2108".
# If the format is different, the whole displayId is used as the code.
def subject_code(display_id: str) -> str:
    parts = display_id.split("-")
    return parts[1] if len(parts) >= 3 else display_id


# Turns one raw course into a CourseSite.
def to_site(course: dict[str, Any]) -> CourseSite:
    return CourseSite(
        id=course["id"],
        display_id=course.get("displayId") or course["id"],
        name=course.get("displayName") or course.get("name") or "",
    )


# Groups a term's available, non-organization sites by subject code, so
# IE2108's lecture and tutorial sites become one Course with two sites.
# Sorted by code; sites sorted by displayId. The subject's name is the
# shortest site name (usually the plainest one).
def group_sites(raw: list[dict[str, Any]], term: str) -> list[Course]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in raw:
        if term_name(c) != term or c.get("isOrganization") or not c.get("isAvailable", True):
            continue
        groups.setdefault(subject_code(c.get("displayId") or ""), []).append(c)
    courses = []
    for code in sorted(groups):
        sites = sorted((to_site(c) for c in groups[code]), key=lambda s: s.display_id)
        name = min((s.name for s in sites if s.name), key=len, default=code)
        courses.append(Course(code=code, name=name, term=term, sites=sites))
    return courses


# Every available site that isn't a semester course, each as its own Course
# (code = its displayId). NTU marks none as isOrganization (verified 24 Oct);
# its extra sites sit in a term named "Non-curriculum" instead.
def other_sites(raw: list[dict[str, Any]]) -> list[Course]:
    extra = [
        c for c in raw
        if (c.get("isOrganization") or not TERM_RE.match(term_name(c))) and c.get("isAvailable", True)
    ]
    return [
        Course(code=s.display_id, name=s.name, term=term_name(c) or "-", sites=[s])
        for c, s in sorted(((c, to_site(c)) for c in extra), key=lambda p: p[1].display_id)
    ]


# Finds subjects matching what you typed, in this order:
# 1. exact code ("ie2108"), 2. a site id ("_2706657_1"),
# 3. every word you typed appears in the subject or a site name ("data struct").
# Returns [] if nothing matches, several if it's ambiguous.
def match_courses(courses: list[Course], query: str) -> list[Course]:
    q = query.strip().lower()
    if not q:
        return []
    exact = [c for c in courses if c.code.lower() == q]
    if exact:
        return exact
    by_site = [c for c in courses if any(s.id.lower() == q for s in c.sites)]
    if by_site:
        return by_site
    words = q.split()
    return [
        c for c in courses
        if all(any(w in text.lower() for text in [c.name, *(s.name for s in c.sites)]) for w in words)
    ]


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.courses
# Live, GET only. Prints counts and subject codes, never names or content.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of the courses client (GET only).").parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    client = CoursesClient(NtuLearnHttp(), load_settings())
    raw = client.raw_courses()
    print(f"1) memberships: {len(raw)} sites")
    print(f"2) current term: {client.current_term()}")
    result = client.list_courses()
    print(f"3) subjects: {len(result.courses)}")
    for c in result.courses:
        print(f"   {c.code}: {len(c.sites)} site(s)")
    print(f"   notes: {result.notes or 'none'}")

    first = result.courses[0]
    print("4) matching")
    print(f"   '{first.code.lower()}' -> {[c.code for c in client.find(first.code.lower())]}")
    word = next((w for w in first.name.split() if len(w) > 3 and w.isalpha()), None)
    if word:
        hits = client.find(word.lower())
        print(f"   a word from {first.code}'s name -> {len(hits)} match(es), includes it: {first in hits}")
    print(f"   'zzzz' -> {client.find('zzzz')}")
    print(f"5) cached: second call made no request: {client.raw_courses() is raw}")
    extra = len(client.list_courses(include_organizations=True).courses) - len(result.courses)
    print(f"6) extra non-semester sites with include_organizations: {extra}")


if __name__ == "__main__":
    _main()
