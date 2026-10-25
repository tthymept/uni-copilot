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
"""

import argparse
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import requests

from uni_copilot.ntulearn_mcp.auth.session import LoginRequired, LoginResult, cookie_summary, sign_in
from uni_copilot.ntulearn_mcp.config import (
    BASE_URL,
    MAX_CONCURRENT,
    MAX_RETRIES,
    REQUEST_INTERVAL_S,
    REQUEST_TIMEOUT_S,
)

log = logging.getLogger(__name__)

ME_PATH = "/learn/api/v1/users/me"
BASE_HOST = urlparse(BASE_URL).hostname


# Raised when NTULearn answers with an error we can't fix by retrying
# (e.g. 403 no access, 404 not found, or still 5xx after all retries).
# Carries only the status and the path, never the response body.
class NtuLearnError(Exception):
    def __init__(self, path: str, status: int | None, reason: str = ""):
        self.path = path
        self.status = status
        super().__init__(f"NTULearn {status or 'no response'} on {path}" + (f": {reason}" if reason else ""))


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
    def __init__(self, signer: Callable[..., LoginResult] = sign_in):
        self._signer = signer
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


# Self-test: uv run python -m uni_copilot.ntulearn_mcp.client.http
# Prints statuses, counts and timings only, never content.
def _main() -> None:
    argparse.ArgumentParser(description="Live self-test of the NTULearn HTTP client (GET only).").parse_args()
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
