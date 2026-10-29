"""
The only door to NTULearn. Every request in the project goes through NtuLearnHttp.

- GET only: this class has no post/put/delete method at all, so nothing that
  uses it can change anything on NTULearn.
- Polite: at least REQUEST_INTERVAL_S between two requests, at most
  MAX_CONCURRENT in flight, retries with growing waits on 429 and 5xx.
- On 401 (session expired) it signs in again silently, once, and retries.
- Logs path, status and timing only. Never cookie values or response bodies.

Tools may run at the same time in different threads (the MCP SDK runs plain
`def` tools in worker threads), so everything shared here is protected by locks.

Phase 4 adds get_file() for course files (still GET only). A download isn't
JSON and leaves NTULearn: verified 29 Oct, it goes NTULearn -> NTULearn (a
signed link) -> alt-<id>.blackboard.com (signed, no cookies needed) -> the
file, with no size header. So get_file follows redirects itself: only to
NTULearn (with your cookies) or a host ending in FILE_HOST_SUFFIXES (without
them), at most MAX_DOWNLOAD_REDIRECTS, counting bytes against a limit as
they arrive. The signed links are secrets like Kaltura's `ks`: never logged,
never in an error message, never returned.
"""

import argparse
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlparse

import requests

from uni_copilot.ntulearn_mcp.auth.session import LoginRequired, LoginResult, cookie_summary, sign_in
from uni_copilot.ntulearn_mcp.config import (
    BASE_URL,
    FILE_HOST_SUFFIXES,
    MAX_CONCURRENT,
    MAX_DOWNLOAD_REDIRECTS,
    MAX_RETRIES,
    REQUEST_INTERVAL_S,
    REQUEST_TIMEOUT_S,
)

log = logging.getLogger(__name__)

ME_PATH = "/learn/api/v1/users/me"
BASE_HOST = urlparse(BASE_URL).hostname
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DOWNLOAD_CHUNK = 64 * 1024   # bytes read at a time while downloading


# Raised when NTULearn answers with an error we can't fix by retrying
# (e.g. 403 no access, 404 not found, or still 5xx after all retries).
# Carries only the status and the path, never the response body.
class NtuLearnError(Exception):
    def __init__(self, path: str, status: int | None, reason: str = ""):
        self.path = path
        self.status = status
        super().__init__(f"NTULearn {status or 'no response'} on {path}" + (f": {reason}" if reason else ""))


# Raised by get_file when a file is bigger than the limit for what we're
# doing with it (reading, downloading, sending). A ValueError, so the tools
# pass its plain message on to Claude as it is (server.py's _as_tool_errors).
class FileTooLarge(ValueError):
    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"The file is larger than {limit / 1_000_000:g} MB, the limit for this; it wasn't fetched.")


# Internal signal inside get_file: NTULearn answered "not signed in" (401, or
# a redirect to a host we don't allow, e.g. Microsoft's sign-in page).
class _SignedOut(Exception):
    pass


# What get_file tells its caller about a finished download. The bytes went
# into the caller's sink; this holds only facts that are safe to show.
@dataclass(frozen=True)
class FileInfo:
    size: int                    # bytes received
    content_type: str | None     # e.g. "application/pdf" (from the file server; may be generic)
    hosts: tuple[str, ...]       # host NAMES the download went through, never the links


# Spaces requests out so that no two of them start less than `interval` seconds
# apart, even when several threads ask at once. Each caller books the next free
# time slot under the lock, then sleeps outside the lock until its slot comes.
class _Pacer:
    def __init__(self, interval: float):
        self._interval = interval
        self._lock = threading.Lock()
        self._next_slot = 0.0

    # Blocks until this caller may start its request.
    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self._interval
        if start > now:
            time.sleep(start - now)


class NtuLearnHttp:
    # Sets up one shared requests.Session (it keeps the cookie jar and reuses connections),
    # the pacer, the concurrency limit and the login lock.
    # `signer` is the sign-in function; tests pass a fake one so no browser opens.
    # `file_session_factory` (Phase 4) makes the cookie-less session get_file
    # uses for Blackboard's file server; tests pass a fake one.
    def __init__(self, signer: Callable[..., LoginResult] = sign_in,
                 file_session_factory: Callable[[], requests.Session] = requests.Session):
        self._signer = signer
        self._file_session_factory = file_session_factory
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self._pacer = _Pacer(REQUEST_INTERVAL_S)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._login_lock = threading.Lock()
        # Goes up by one every time we get new cookies. Lets a thread tell
        # "someone already re-logged in while I was waiting" apart from "I must do it".
        self._cookie_version = 0

    # The main entry point: GET a JSON API path like "/learn/api/v1/users/me" and
    # return the parsed JSON. Signs in first if we have no cookies yet, re-logs in
    # once on 401, retries 429/5xx/network errors, raises NtuLearnError otherwise.
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/"):
            raise ValueError("path must start with '/' (the base URL is added here)")
        self.ensure_signed_in()

        relogged = False
        retries = 0
        while True:
            version = self._cookie_version
            try:
                r = self._send(path, params)
            except requests.RequestException as e:
                # Network trouble (timeout, connection reset): treat like a 5xx.
                if retries < MAX_RETRIES:
                    retries += 1
                    self._backoff(path, None, retries, f"{type(e).__name__}")
                    continue
                raise NtuLearnError(path, None, type(e).__name__) from None

            if self._looks_signed_out(r) and not relogged:
                log.info("GET %s -> %s, session expired, signing in again", path, r.status_code)
                self._refresh_login(version)
                relogged = True
                continue
            if (r.status_code == 429 or r.status_code >= 500) and retries < MAX_RETRIES:
                retries += 1
                self._backoff(path, r, retries)
                continue

            if self._looks_signed_out(r):
                raise LoginRequired("Still signed out after a fresh sign-in. Run the login tool.")
            if not r.ok:
                raise NtuLearnError(path, r.status_code)
            try:
                return r.json()
            except ValueError:
                raise NtuLearnError(path, r.status_code, "response was not JSON") from None

    # For list endpoints: GETs `path` and follows paging.nextPage until the end,
    # returning all `results` together. nextPage is a path with its own query
    # string (verified 24 Oct: ".../announcements?offset=10"), so the first
    # call's params are not sent again.
    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        results: list[Any] = []
        next_path: str | None = path
        while next_path:
            body = self.get_json(next_path, params)
            results += body.get("results", [])
            next_path = (body.get("paging") or {}).get("nextPage")
            params = None
        return results

    # Phase 4: downloads one course file into `sink` (anything with .write():
    # an io.BytesIO to read it in memory, or an open file to save it).
    # `path_or_url` is an API path ("/learn/api/public/v1/.../download") or a
    # full NTULearn link (an embedded file's signed /bbcswebdav/ link from a
    # page body). Signs in first if needed and once more on "signed out".
    # Raises FileTooLarge past `max_bytes` (the sink then holds a partial file
    # the caller must throw away), NtuLearnError for other failures.
    def get_file(self, path_or_url: str, max_bytes: int, sink: BinaryIO) -> FileInfo:
        url, label = _file_start(path_or_url)
        self.ensure_signed_in()
        relogged = False
        while True:
            version = self._cookie_version
            try:
                return self._download(url, label, max_bytes, sink)
            except _SignedOut:
                if relogged:
                    raise LoginRequired("Still signed out after a fresh sign-in. Run the login tool.") from None
                log.info("file download: signed out, signing in again")
                self._refresh_login(version)
                relogged = True

    # One download attempt: follows the redirect chain hop by hop (each hop
    # paced like every other request), then reads the body. NTULearn hops use
    # the signed-in session; other allowed hosts a fresh one without cookies,
    # closed afterwards. Nothing is written to the sink before the final 200,
    # so a sign-in retry starts clean.
    def _download(self, url: str, label: str, max_bytes: int, sink: BinaryIO) -> FileInfo:
        file_session = self._file_session_factory()
        hosts: list[str] = []
        try:
            for _ in range(MAX_DOWNLOAD_REDIRECTS + 1):
                host = urlparse(url).hostname or ""
                on_ntulearn = host == BASE_HOST
                hosts.append(host)
                r = self._send_file_hop(self._session if on_ntulearn else file_session, url, label, len(hosts))
                if on_ntulearn and r.status_code == 401:
                    r.close()
                    raise _SignedOut
                if r.status_code in REDIRECT_STATUSES and r.headers.get("Location"):
                    target = urljoin(url, r.headers["Location"])
                    r.close()
                    if not _allowed_file_host(target):
                        # Sent away from NTULearn to somewhere else: most likely the
                        # sign-in page (a guess, like _looks_signed_out). From a file
                        # host it's simply refused.
                        if on_ntulearn:
                            raise _SignedOut
                        raise NtuLearnError(label, None, "the download led to a host that isn't allowed")
                    url = target
                    continue
                if r.status_code != 200:
                    r.close()
                    raise NtuLearnError(label, r.status_code)
                return self._read_body(r, label, max_bytes, sink, tuple(hosts))
            raise NtuLearnError(label, None, f"more than {MAX_DOWNLOAD_REDIRECTS} redirects")
        finally:
            file_session.close()

    # Sends one hop of a download: no redirect following, body not read yet
    # (stream=True), "Accept: */*" instead of the client's JSON default.
    # Waits for a slot and the pacer like _send; retries 429, 5xx and network
    # errors with the same waits as get_json. Logs the hop number, host and
    # status only: the link holds signed values.
    def _send_file_hop(self, session: requests.Session, url: str, label: str, hop: int) -> requests.Response:
        retries = 0
        while True:
            try:
                with self._slots:
                    self._pacer.wait()
                    r = session.get(url, allow_redirects=False, stream=True,
                                    timeout=REQUEST_TIMEOUT_S, headers={"Accept": "*/*"})
            except requests.RequestException as e:
                if retries < MAX_RETRIES:
                    retries += 1
                    self._backoff(label, None, retries, type(e).__name__)
                    continue
                raise NtuLearnError(label, None, type(e).__name__) from None
            log.debug("file hop %d on %s -> %s", hop, urlparse(url).hostname, r.status_code)
            if (r.status_code == 429 or r.status_code >= 500) and retries < MAX_RETRIES:
                r.close()
                retries += 1
                self._backoff(label, r, retries)
                continue
            return r

    # Reads the final answer's body into the sink in chunks, stopping with
    # FileTooLarge as soon as it passes max_bytes (or at once, if the server
    # did announce a bigger size). Always closes the connection.
    @staticmethod
    def _read_body(r: requests.Response, label: str, max_bytes: int, sink: BinaryIO,
                   hosts: tuple[str, ...]) -> FileInfo:
        try:
            declared = r.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FileTooLarge(max_bytes)
            size = 0
            for chunk in r.iter_content(DOWNLOAD_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLarge(max_bytes)
                sink.write(chunk)
            content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip() or None
            return FileInfo(size=size, content_type=content_type, hosts=hosts)
        except requests.RequestException as e:
            raise NtuLearnError(label, None, f"download broke off ({type(e).__name__})") from None
        finally:
            r.close()

    # Makes sure we have cookies. Does a silent sign-in only if we have none yet
    # (e.g. right after the server starts). Never opens a window.
    def ensure_signed_in(self) -> None:
        if self._cookie_version == 0:
            self._refresh_login(0)

    # For the login tool: always gets fresh cookies, and may open a visible
    # window if a silent sign-in isn't possible. Returns how it went.
    def login(self) -> LoginResult:
        with self._login_lock:
            result = self._signer(allow_window=True)
            self._use_cookies(result)
            return result

    # Names and expiry of the cookies we hold (never values), for the login tools.
    def cookie_summary(self) -> list[str]:
        return cookie_summary(self._session.cookies)

    # For the login_status tool: one users/me call with the cookies we have.
    # True = signed in. Does NOT try to sign in, so it shows the real state.
    def is_signed_in(self) -> bool:
        if self._cookie_version == 0:
            return False
        try:
            r = self._send(ME_PATH, None)
        except requests.RequestException:
            return False
        return r.status_code == 200 and not self._looks_signed_out(r)

    # Sends exactly one GET, after waiting for a free slot (max 2 at once) and
    # for the pacer (1 s apart). Logs path, status and time taken.
    def _send(self, path: str, params: dict[str, Any] | None) -> requests.Response:
        with self._slots:
            self._pacer.wait()
            start = time.monotonic()
            r = self._session.get(BASE_URL + path, params=params, timeout=REQUEST_TIMEOUT_S)
            log.debug("GET %s -> %s (%.1f s)", path, r.status_code, time.monotonic() - start)
            return r

    # Gets new cookies silently, unless another thread already did it while this
    # one was waiting for the lock (then the cookie version has moved on).
    def _refresh_login(self, seen_version: int) -> None:
        with self._login_lock:
            if self._cookie_version != seen_version:
                return
            self._use_cookies(self._signer(allow_window=False))

    # Swaps in the new cookie jar and bumps the version number.
    def _use_cookies(self, result: LoginResult) -> None:
        self._session.cookies = result.cookies
        self._cookie_version += 1
        log.info("signed in (%s, %.0f s)", "window" if result.used_window else "silent", result.seconds)

    # True if the response means "you're not signed in": a 401, or being
    # redirected away from NTULearn (e.g. to Microsoft's sign-in page).
    # The redirect case is a guess for safety; only the 401 is verified.
    @staticmethod
    def _looks_signed_out(r: requests.Response) -> bool:
        return r.status_code == 401 or urlparse(r.url).hostname != BASE_HOST

    # Waits before a retry: Retry-After if NTULearn sent one, else 2, 4, 8 s.
    @staticmethod
    def _backoff(path: str, r: requests.Response | None, attempt: int, why: str = "") -> None:
        wait = 2.0**attempt
        if r is not None:
            why = str(r.status_code)
            retry_after = r.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = float(retry_after)
        log.info("GET %s -> %s, retry %d/%d in %.0f s", path, why, attempt, MAX_RETRIES, wait)
        time.sleep(wait)


# True if a download may go to this link: https, and either NTULearn itself
# or a host ending in one of FILE_HOST_SUFFIXES (Blackboard's file server).
def _allowed_file_host(url: str) -> bool:
    parts = urlparse(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (host == BASE_HOST or host.endswith(FILE_HOST_SUFFIXES))


# Where get_file starts, and the label used for it in logs and errors.
# An API path gets the base URL added and is its own label (it has ids, no
# secrets). A full link must be on NTULearn (an embedded file's signed link);
# its label is a fixed "an embedded file", because the link holds signed values.
def _file_start(path_or_url: str) -> tuple[str, str]:
    if path_or_url.startswith("/") and not path_or_url.startswith("//"):
        return BASE_URL + path_or_url, path_or_url.split("?", 1)[0]
    parts = urlparse(path_or_url)
    if parts.scheme != "https" or parts.hostname != BASE_HOST:
        raise ValueError("A file download must start on NTULearn (an API path or an https://ntulearn.ntu.edu.sg link).")
    return path_or_url, "an embedded file"


# Live check of get_file (--file): one file item's attachment and one file
# embedded in a page, both into memory and thrown away; then the refusals.
# Prints sizes, types and host names only: never names, links or content.
def _file_self_test(http: "NtuLearnHttp") -> None:
    import html
    import io
    import json
    import re

    from uni_copilot.ntulearn_mcp.client.courses import CoursesClient
    from uni_copilot.ntulearn_mcp.config import READ_MAX_BYTES, load_settings

    sites = [s.id for c in CoursesClient(http, load_settings()).list_courses().courses for s in c.sites]
    attachment_path = embedded_url = None
    for site in sites:
        if attachment_path and embedded_url:
            break
        tree = http.get_all(f"/learn/api/public/v1/courses/{site}/contents", {"recursive": "true", "limit": 200})
        for item in tree:
            kind = (item.get("contentHandler") or {}).get("id")
            if kind == "resource/x-bb-file" and not attachment_path:
                atts = http.get_all(f"/learn/api/public/v1/courses/{site}/contents/{item['id']}/attachments")
                pdf = next((a for a in atts if a.get("mimeType") == "application/pdf"), None)
                if pdf:
                    attachment_path = (f"/learn/api/public/v1/courses/{site}/contents/{item['id']}"
                                       f"/attachments/{pdf['id']}/download")
            if kind == "resource/x-bb-document" and not embedded_url:
                for raw in re.findall(r"""data-bbfile=(["'])(.*?)\1""", item.get("body") or "", re.S):
                    meta = json.loads(html.unescape(raw[1]))
                    link = meta.get("resourceUrl") or meta.get("viewerUrl")
                    if link and meta.get("mimeType") == "application/pdf":
                        embedded_url = link
                        break

    for name, start in (("file item's attachment", attachment_path), ("embedded file", embedded_url)):
        if not start:
            print(f"   {name}: none found")
            continue
        sink = io.BytesIO()
        t0 = time.monotonic()
        info = http.get_file(start, READ_MAX_BYTES, sink)
        print(f"   {name}: {info.size / 1_000_000:.2f} MB, {info.content_type}, via {list(info.hosts)}, "
              f"starts like a PDF: {sink.getvalue()[:5] == b'%PDF-'}, {time.monotonic() - t0:.1f} s")

    try:
        http.get_file("https://example.org/file.pdf", READ_MAX_BYTES, io.BytesIO())
    except ValueError as e:
        print("   a link off NTULearn ->", e)
    if attachment_path:
        try:
            http.get_file(attachment_path, 1000, io.BytesIO())
        except FileTooLarge as e:
            print("   limit 1000 bytes ->", e)


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.http [--file]
# Prints statuses, counts and timings only, never content.
def _main() -> None:
    ap = argparse.ArgumentParser(description="Live self-test of the NTULearn HTTP client (GET only).")
    ap.add_argument("--file", action="store_true", help="Phase 4: only check get_file on two real course files")
    args = ap.parse_args()
    if args.file:
        logging.basicConfig(level=logging.WARNING)
        print("get_file: two real course files into memory (GET only, nothing saved):")
        _file_self_test(NtuLearnHttp())
        return
    logging.basicConfig(level=logging.DEBUG, format="  [%(name)s] %(message)s")
    for noisy in ("urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    http = NtuLearnHttp()
    print("1) Before any call, signed in?", http.is_signed_in())

    print("2) users/me (signs in silently first)")
    me = http.get_json(ME_PATH)
    print("   got a user id:", bool(me.get("id")))

    print("3) Three calls in a row; they should be ~1 s apart")
    t0 = time.monotonic()
    for _ in range(3):
        http.get_json(ME_PATH)
    print(f"   3 calls took {time.monotonic() - t0:.1f} s")

    print("4) Simulate an expired session: throw away the cookies, then call again")
    http._session.cookies.clear()
    http.get_json(ME_PATH)
    print("   recovered:", http.is_signed_in())

    print("5) A path that doesn't exist should raise NtuLearnError")
    try:
        http.get_json("/learn/api/v1/this-does-not-exist")
    except NtuLearnError as e:
        print("   NtuLearnError, status", e.status)


if __name__ == "__main__":
    _main()
