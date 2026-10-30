"""
A made-up NTULearn course for the Phase 4 tests (content, files, sending,
the indexer). Imported by the test files (pytest puts this folder on the path).

In plain words:
  FakeContentHttp stands in for NtuLearnHttp: get_json / get_all answer from
  a small made-up course tree, and get_file writes a tiny real PDF into the
  sink. Every path asked for is recorded in `calls`, so a test can check how
  many requests something made. Everything is synthetic: course XX1000,
  site _1_1, a signed link whose secret part is the word SECRET (tests check
  it never shows up anywhere it shouldn't).

The tree:
  _10_1  folder "Week 10"
    _11_1  Ultra page "Lecture 10" (a folder whose hidden body document _12_1
           embeds week10-slides.pdf, xid-555_1, by a signed link)
    _13_1  file item "Tutorial 10" (T10.pdf, attachment _77_1)
    _14_1  test "Quiz 10" (gradebook column _99_1, lockdown browser)
"""

import json
from datetime import datetime, timezone
from typing import Any

from uni_copilot.ntulearn_mcp.client.http import NtuLearnError
from uni_copilot.ntulearn_mcp.models import Course, CourseList, CourseSite
from uni_copilot.ntulearn_mcp.parsers.files import _tiny_pdf

SITE = "_1_1"
CODE = "XX1000"
PUBLIC = f"/learn/api/public/v1/courses/{SITE}"
INTERNAL = f"/learn/api/v1/courses/{SITE}"
SIGNED = "https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1/xid-555_1?sig=SECRET"
NOW = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)


# An Ultra page body embedding one file by a signed link (as NTULearn writes it).
def page_body(xid: str = "xid-555_1", name: str = "week10-slides.pdf") -> str:
    meta = json.dumps({"linkName": name, "mimeType": "application/pdf", "viewerUrl": SIGNED.replace("xid-555_1", xid)})
    return f"<p>Slides: <a href=\"#\" data-bbfile='{meta}'>slides</a></p><p>Dijkstra this week.</p>"


# The made-up course tree, as the recursive contents call returns it.
def tree(modified: str = "2026-10-20T02:00:00.000Z", body: str | None = None) -> list[dict[str, Any]]:
    return [
        {"id": "_10_1", "parentId": "_ROOT_", "title": "Week 10", "contentHandler": {"id": "resource/x-bb-folder"},
         "availability": {"available": "Yes"}, "hasChildren": True},
        {"id": "_11_1", "parentId": "_10_1", "title": "Lecture 10", "contentHandler": {"id": "resource/x-bb-folder"},
         "availability": {"available": "PartiallyVisible"}, "hasChildren": True, "modified": "2026-10-20T02:00:00.000Z"},
        {"id": "_12_1", "parentId": "_11_1", "title": "ultraDocumentBody", "body": body or page_body(),
         "contentHandler": {"id": "resource/x-bb-document"}, "availability": {"available": "PartiallyVisible"}},
        {"id": "_13_1", "parentId": "_10_1", "title": "Tutorial 10", "modified": modified,
         "contentHandler": {"id": "resource/x-bb-file", "file": {"fileName": "T10.pdf", "mimeType": "application/pdf"}},
         "availability": {"available": "Yes"}},
        {"id": "_14_1", "parentId": "_10_1", "title": "Quiz 10", "availability": {"available": "PartiallyVisible"},
         "contentHandler": {"id": "resource/x-bb-asmt-test-link", "gradeColumnId": "_99_1",
                            "password": {"enabled": False}, "isLateAttemptCreationDisallowed": True,
                            "proctoring": {"secureBrowserRequiredToTake": True, "webcamRequired": False}},
         "description": "<p>Bring a calculator.</p>"},
    ]


class FakeContentHttp:
    # `file_bytes`: what get_file writes (a tiny real PDF by default);
    # `missing`: get_file answers 404 (a file removed from NTULearn).
    def __init__(self, file_bytes: bytes | None = None):
        self.tree = tree()
        self.calls: list[str] = []
        self.missing = False
        self.file_bytes = file_bytes if file_bytes is not None else _tiny_pdf(["Dijkstra uses a priority queue"])
        self.attachment_name = "T10.pdf"

    # Same signature as NtuLearnHttp.get_all.
    def get_all(self, path: str, params: dict | None = None) -> list[dict[str, Any]]:
        self.calls.append(path)
        if path == f"{PUBLIC}/contents":
            return self.tree
        if path == f"{PUBLIC}/contents/_13_1/attachments":
            return [{"id": "_77_1", "fileName": self.attachment_name, "mimeType": "application/pdf"}]
        if path == f"{PUBLIC}/contents/_11_1/children":
            return [t for t in self.tree if t["id"] == "_12_1"]
        raise NtuLearnError(path, 404)

    # Same signature as NtuLearnHttp.get_json.
    def get_json(self, path: str, params: dict | None = None) -> Any:
        self.calls.append(path)
        if path == f"{PUBLIC}/contents/_14_1":
            return next(t for t in self.tree if t["id"] == "_14_1")
        raise NtuLearnError(path, 404)

    # Same signature as NtuLearnHttp.get_file: writes the file into the sink.
    def get_file(self, start: str, max_bytes: int, sink) -> None:
        self.calls.append("FILE:" + ("embedded" if start.startswith("https://") else start))
        if self.missing:
            raise NtuLearnError("a file", 404)
        sink.write(self.file_bytes)


# Stands in for CoursesClient: one made-up course with one site.
class FakeCourses:
    def pick(self, course: str | None) -> list[Course]:
        return [Course(code=CODE, name="MADE UP", term="26S1",
                       sites=[CourseSite(id=SITE, display_id=f"26S1-{CODE}-LEC", name="MADE UP")])]

    def list_courses(self, term: str | None = None, include_organizations: bool = False) -> CourseList:
        return CourseList(term="26S1", courses=self.pick(None))

    def user_id(self) -> str:
        return "_me_1"
