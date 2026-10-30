"""
Offline tests for parsers/content.py (course trees, pages, embedded files,
release conditions, quiz details) and parsers/files.py (file text).

In plain words:
  The content tree and the page body are made-up JSON and HTML shaped like
  NTULearn's (fake_ntulearn.py); the PDF and PPTX are tiny real files built
  here. The key safety check: an embedded file's signed link (its secret part
  is "SECRET") is only ever handed to the download code, never to anything a
  tool returns.
"""

import json

import pytest

from fake_ntulearn import CODE, SITE, page_body, tree
from uni_copilot.ntulearn_mcp.parsers.content import (
    assessment_details,
    content_kind,
    embedded_files,
    embedded_links,
    find_ultra_pages,
    page_text,
    release_from_internal,
    release_from_public,
    to_content_item,
)
from uni_copilot.ntulearn_mcp.parsers.files import (
    UnsupportedFile,
    _tiny_pdf,
    _tiny_pptx,
    choose_pages,
    detect_type,
    extract_text,
)


# ---------- Trees and items ----------

# Each kind is recognised; the hidden Ultra page body isn't an item of its own.
def test_kinds_and_hidden_page_body():
    raws = tree()
    pages = find_ultra_pages(raws)
    items = [to_content_item(r, CODE, SITE, pages, release_from_public(r)) for r in raws]
    kinds = {i.id: i.kind for i in items if i}
    assert kinds == {"_10_1": "folder", "_11_1": "page", "_13_1": "file", "_14_1": "assessment"}
    assert to_content_item(raws[2], CODE, SITE, pages, release_from_public(raws[2])) is None
    assert content_kind(raws[0], pages) == "folder"


# A file item keeps its file name and type; a quiz its gradebook column.
def test_file_and_assessment_fields():
    raws = tree()
    pages = find_ultra_pages(raws)
    by_id = {r["id"]: to_content_item(r, CODE, SITE, pages, release_from_public(r)) for r in raws}
    assert (by_id["_13_1"].file_name, by_id["_13_1"].mime_type) == ("T10.pdf", "application/pdf")
    assert by_id["_14_1"].column_id == "_99_1"
    assert by_id["_14_1"].description == "Bring a calculator."


# The public API only says Yes / PartiallyVisible; the internal one has the rules.
def test_release_conditions():
    assert release_from_public({"availability": {"available": "Yes"}}).state == "open"
    assert release_from_public({"availability": {"available": "PartiallyVisible"}}).state == "conditional"
    assert release_from_internal({}).state == "open"
    rule = {"adaptiveReleaseRules": {"rules": [{"title": "After Lab 9", "criteria": [{"type": "Pre_Content"}]}]}}
    assert release_from_internal({**rule, "visibility": "VISIBLE"}).state == "open_to_you"
    held = release_from_internal({**rule, "visibility": "PARTIALLY_VISIBLE"})
    assert (held.state, held.rule) == ("held_back", "After Lab 9")
    dated = release_from_internal({"visibility": "VISIBLE", "adaptiveReleaseRules": {"rules": [
        {"title": "Week 12", "criteria": [{"start": "2026-11-02T00:00:00.000Z", "end": "2026-11-09T00:00:00.000Z"}]}]}})
    assert dated.opens_at.day == 2 and dated.closes_at.day == 9


# ---------- Embedded files and signed links ----------

# The file is listed by name, type and xid; its signed link only comes out of
# embedded_links (for the download code), never out of the page text or item.
def test_embedded_file_link_never_leaks():
    body = page_body()
    files = embedded_files(body)
    assert [(f.xid, f.name, f.mime_type, f.has_link) for f in files] == [
        ("xid-555_1", "week10-slides.pdf", "application/pdf", True)]
    assert "SECRET" in embedded_links(body)["xid-555_1"]
    text, links = page_text(body)
    assert "Dijkstra this week." in text
    assert "SECRET" not in text and all("SECRET" not in link.url for link in links)
    raws = tree()
    pages = find_ultra_pages(raws)
    item = to_content_item(raws[1], CODE, SITE, pages, release_from_public(raws[1]))
    assert "SECRET" not in item.model_dump_json()


# A signed file link a prof pasted by hand into the text is hidden too.
def test_hand_pasted_signed_link_hidden():
    text, links = page_text("<p>Notes: https://ntulearn.ntu.edu.sg/bbcswebdav/pid-9/xid-9_1?sig=SECRET</p>")
    assert "SECRET" not in text and all("SECRET" not in link.url for link in links)


# Quiz details: attempts, lockdown browser, late attempts, due; never a score.
def test_assessment_details_have_no_score():
    raw = tree()[4]
    column = {"grading": {"attemptsAllowed": 2, "due": "2026-11-06T15:59:00.000Z"}, "score": {"possible": 10}}
    details = assessment_details(raw, column, None)
    assert details["attempts_allowed"] == 2
    assert details["secure_browser_required"] is True
    assert details["late_attempts_allowed"] is False
    assert details["due_at"].day == 6
    assert not any("score" in key or "grade" in key or "point" in key for key in details)


# ---------- File text ----------

# A tiny real PDF: one entry per page, an empty page stays (counted as empty).
def test_pdf_text():
    result = extract_text(_tiny_pdf(["Shortest paths", "", "Dijkstra uses a priority queue"]), "w10.pdf")
    assert result.file_type == "pdf"
    assert len(result.pages) == 3 and result.empty_pages == 1
    assert "Dijkstra" in result.pages[2]


# A tiny real PPTX: one entry per slide, with tables, groups and speaker notes.
def test_pptx_text():
    result = extract_text(_tiny_pptx(), "w10.pptx")
    assert result.file_type == "pptx" and len(result.pages) == 2
    assert "Dijkstra's algorithm" in result.pages[0] and "quiz on Friday" in result.pages[0]
    assert "O(E log V)" in result.pages[1] and "Text inside a group" in result.pages[1]


# A notebook: markdown and code cells become text.
def test_notebook_text():
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Lab 3\n", "Load the data."]},
                    {"cell_type": "code", "source": "import pandas as pd", "outputs": []}],
          "nbformat": 4, "metadata": {}}
    result = extract_text(json.dumps(nb).encode(), "lab3.ipynb")
    assert result.file_type == "ipynb"
    assert "Lab 3" in " ".join(result.pages) and "import pandas" in " ".join(result.pages)


# The first bytes decide, not the name: a "PDF" that's really a zip isn't a PDF.
def test_detect_type_by_content():
    assert detect_type(_tiny_pdf(["x"]), "notes.txt") == "pdf"
    assert detect_type(b"hello", "notes.txt") == "text"
    with pytest.raises(UnsupportedFile):
        detect_type(b"PK\x03\x04 not a deck", "slides.zip")


# Page ranges: a few pages at a time, with what to ask for next.
def test_choose_pages():
    pages = [f"page {n}" for n in range(1, 31)]
    assert choose_pages(None, pages, max_pages=20, max_chars=10_000) == (list(range(1, 21)), True, "21-30")
    assert choose_pages("5", pages, 20, 10_000) == ([5], False, "6-25")
    assert choose_pages("28-", pages, 20, 10_000) == ([28, 29, 30], False, None)
    with pytest.raises(ValueError, match="30 pages"):
        choose_pages("31", pages, 20, 10_000)
    with pytest.raises(ValueError):
        choose_pages("abc", pages, 20, 10_000)
