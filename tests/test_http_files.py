"""
Offline tests for NtuLearnHttp.get_file (Phase 4): following the download
redirects by hand, only to allowed hosts, without NTULearn cookies on the
file server, and stopping at the size limit.

In plain words:
  A real download goes NTULearn -> NTULearn (signed link) -> Blackboard's file
  server (alt-....blackboard.com) -> the file. Here two fake sessions answer
  each hop from a list: one stands in for the signed-in NTULearn session, the
  other for the fresh cookie-less session used on the file server. The links
  and the bytes are made up; nothing goes to the network.
"""

import io

import pytest
from requests.cookies import RequestsCookieJar

from uni_copilot.ntulearn_mcp.auth.session import LoginResult
from uni_copilot.ntulearn_mcp.client import http as http_mod
from uni_copilot.ntulearn_mcp.client.http import FileTooLarge, NtuLearnError, NtuLearnHttp

START = "/learn/api/public/v1/courses/_1_1/contents/_13_1/attachments/_77_1/download"
SIGNED = "https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1/xid-5_1?sig=SECRET"
FILE_HOST = "https://alt-5f00.blackboard.com/files/xid-5_1?Expires=1&Signature=SECRET"


# One made-up answer to one hop: a status, headers, and (for a 200) a body.
class Hop:
    def __init__(self, status: int, location: str | None = None, body: bytes = b"", length: str | None = None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        if length:
            self.headers["Content-Length"] = length
        self.headers["Content-Type"] = "application/pdf"
        self._body = body
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        self.closed = True


# A fake session: records the URLs it was asked for, answers from a list.
class FakeSession:
    def __init__(self, answers):
        self.answers = list(answers)
        self.urls: list[str] = []

    def get(self, url, allow_redirects=True, stream=False, timeout=None, headers=None):
        assert allow_redirects is False and stream is True     # every hop by hand, body streamed
        self.urls.append(url)
        return self.answers.pop(0)

    def close(self):
        pass


# An NtuLearnHttp whose NTULearn session and file-server session are fakes,
# sign-in is a recorder and nothing sleeps.
@pytest.fixture
def make(monkeypatch):
    def build(ntulearn_answers, file_answers=()):
        sign_ins = []

        def signer(allow_window=False):
            sign_ins.append(allow_window)
            return LoginResult(cookies=RequestsCookieJar(), used_window=False, seconds=0.0)

        file_session = FakeSession(file_answers)
        client = NtuLearnHttp(signer=signer, file_session_factory=lambda: file_session)
        ntulearn = FakeSession(ntulearn_answers)
        monkeypatch.setattr(client, "_session", ntulearn)
        monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)
        return client, ntulearn, file_session, sign_ins
    return build


# The whole chain: two NTULearn hops on the signed-in session, the file
# server on the cookie-less one; only host names come back, never links.
def test_redirect_chain(make):
    pdf = b"%PDF-1.4 made up" * 100
    client, ntulearn, files, _ = make([Hop(302, SIGNED), Hop(302, FILE_HOST)], [Hop(200, body=pdf)])
    sink = io.BytesIO()
    info = client.get_file(START, 10_000, sink)
    assert sink.getvalue() == pdf and info.size == len(pdf) and info.content_type == "application/pdf"
    assert len(ntulearn.urls) == 2 and files.urls == [FILE_HOST]
    assert info.hosts == ("ntulearn.ntu.edu.sg", "ntulearn.ntu.edu.sg", "alt-5f00.blackboard.com")


# Too big: stopped as soon as the bytes pass the limit, or at once when the
# server says the size up front.
@pytest.mark.parametrize("hop", [Hop(200, body=b"x" * 5000), Hop(200, body=b"x", length="5000")])
def test_size_limit(make, hop):
    client, _, _, _ = make([hop])
    with pytest.raises(FileTooLarge):
        client.get_file(START, 1000, io.BytesIO())


# The file server sends us somewhere else: refused (never followed).
def test_unknown_host_refused(make):
    client, _, files, _ = make([Hop(302, FILE_HOST)], [Hop(302, "https://evil.example.com/x")])
    with pytest.raises(NtuLearnError, match="isn't allowed"):
        client.get_file(START, 10_000, io.BytesIO())
    assert files.urls == [FILE_HOST]


# NTULearn sends us away (to a sign-in page): signed out -> one silent
# sign-in, then the download is tried again.
def test_signed_out_signs_in_again(make):
    client, _, _, sign_ins = make([Hop(302, "https://login.microsoftonline.com/x"), Hop(200, body=b"%PDF-")])
    assert client.get_file(START, 10_000, io.BytesIO()).size == 5
    assert sign_ins == [False, False]          # the first sign-in, then one again


# A download must start on NTULearn; endless redirects stop.
def test_start_and_redirect_limits(make):
    client, _, _, _ = make([Hop(302, SIGNED)] * 10)
    with pytest.raises(ValueError):
        client.get_file("https://example.com/file.pdf", 10_000, io.BytesIO())
    with pytest.raises(NtuLearnError, match="redirects"):
        client.get_file(START, 10_000, io.BytesIO())


# The error a tool shows for a signed link names no link.
def test_errors_name_no_link(make):
    client, _, _, _ = make([Hop(404)])
    with pytest.raises(NtuLearnError) as caught:
        client.get_file(SIGNED, 10_000, io.BytesIO())
    assert "SECRET" not in str(caught.value) and "an embedded file" in str(caught.value)
