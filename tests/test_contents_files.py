"""
Offline tests for client/contents.py's file handling: reading a file's text
through the cache, saving a file (download_file), and fetching one for
Telegram (send_course_file), with the made-up course of fake_ntulearn.py.

In plain words:
  FakeContentHttp answers like NTULearn and writes a tiny PDF for every
  download; the cache and the downloads folder are temporary. The checks:
  file names from other people can't escape the folder or hit Windows'
  reserved names, nothing is ever overwritten, videos and non-PDF/PPTX files
  are refused, and a file's text is downloaded once, then read from the cache.
"""

import pytest

from fake_ntulearn import NOW, FakeContentHttp, FakeCourses, page_body
from uni_copilot.ntulearn_mcp.cache import ContentCache
from uni_copilot.ntulearn_mcp.client.contents import MAX_NAME_CHARS, ContentsClient, safe_file_name, unique_path


@pytest.fixture
def http():
    return FakeContentHttp()


@pytest.fixture
def client(http, tmp_path):
    return ContentsClient(http, FakeCourses(), ContentCache(tmp_path / "content.sqlite"), now=lambda: NOW)


# ---------- Names ----------

@pytest.mark.parametrize("name, expected", [
    ("..\\..\\Windows\\evil.pdf", "evil.pdf"),
    ("../../etc/passwd", "passwd"),
    ("CON.pdf", "_CON.pdf"),
    ("nul", "_nul"),
    ('Lab: "1" <draft>?.pdf', "Lab_ _1_ _draft__.pdf"),
    ("notes. ", "notes"),
    ("...", "file"),
])
def test_safe_file_name(name, expected):
    assert safe_file_name(name) == expected


def test_long_name_keeps_extension():
    name = safe_file_name("x" * 300 + ".pptx")
    assert name.endswith(".pptx") and len(name) == MAX_NAME_CHARS


def test_unique_path_never_overwrites(tmp_path):
    (tmp_path / "T10.pdf").write_bytes(b"mine")
    (tmp_path / "T10 (2).pdf.part").write_bytes(b"half")
    assert unique_path(tmp_path, "T10.pdf").name == "T10 (3).pdf"


# ---------- Reading text ----------

# The first read downloads and caches; the second comes from the cache with no download.
def test_file_text_cached(client, http):
    first = client.file_text("XX1000", "_13_1")
    assert "Dijkstra" in first.pages[0].text and first.from_cache is False
    downloads = sum(c.startswith("FILE:") for c in http.calls)
    again = client.file_text("XX1000", "_13_1")
    assert again.from_cache is True and sum(c.startswith("FILE:") for c in http.calls) == downloads


# A file embedded in a page: its link comes from the page fetched just now.
def test_embedded_file_text(client, http):
    result = client.file_text("XX1000", "_11_1", "xid-555_1")
    assert result.file_name == "week10-slides.pdf" and "FILE:embedded" in http.calls


# ---------- download_file ----------

def test_save_file(client, tmp_path):
    folder = tmp_path / "downloads"
    first = client.save_file("XX1000", "_13_1", download_dir=folder)
    second = client.save_file("XX1000", "_13_1", download_dir=folder)
    assert first.saved_to.endswith("T10.pdf") and second.saved_to.endswith("T10 (2).pdf")
    assert (folder / "XX1000" / "T10.pdf").read_bytes().startswith(b"%PDF-")
    assert not list((folder / "XX1000").glob("*.part"))


# A broken download leaves nothing behind (no half file under the real name).
def test_failed_download_leaves_nothing(client, http, tmp_path):
    http.missing = True
    with pytest.raises(ValueError, match="404"):
        client.save_file("XX1000", "_13_1", download_dir=tmp_path)
    assert not any((tmp_path / "XX1000").iterdir())


def test_videos_refused(client, http, tmp_path):
    http.tree[3]["contentHandler"]["file"]["fileName"] = "lecture.mp4"
    http.attachment_name = "lecture.mp4"
    with pytest.raises(ValueError, match="Videos"):
        client.save_file("XX1000", "_13_1", download_dir=tmp_path)


# ---------- send_course_file ----------

def test_file_for_sending(client):
    code, name, kind, data = client.file_bytes_for_sending("XX1000", "_11_1", "xid-555_1")
    assert (code, name, kind) == ("XX1000", "week10-slides.pdf", "pdf") and data.startswith(b"%PDF-")


# Only PDF and PPTX, judged by name and then by the first bytes.
def test_sending_refuses_other_files(http, tmp_path):
    http.attachment_name = "lab3.ipynb"
    client = ContentsClient(http, FakeCourses(), ContentCache(tmp_path / "a.sqlite"), now=lambda: NOW)
    with pytest.raises(ValueError, match="Only PDF and PPTX"):
        client.file_bytes_for_sending("XX1000", "_13_1")
    fake_pdf = FakeContentHttp(file_bytes=b"MZ this is not a pdf")
    client = ContentsClient(fake_pdf, FakeCourses(), ContentCache(tmp_path / "b.sqlite"), now=lambda: NOW)
    with pytest.raises(ValueError, match="doesn't look like a real PDF"):
        client.file_bytes_for_sending("XX1000", "_13_1")


# Nothing about the page body's signed link reaches what a tool returns.
def test_signed_link_not_in_results(client):
    page = client.read_page("XX1000", "_11_1")
    listing = client.list_folder("XX1000", "_10_1")
    assert "SECRET" not in page.model_dump_json() and "SECRET" not in listing.model_dump_json()
    assert "SECRET" in page_body()            # it was there in the made-up page to begin with
