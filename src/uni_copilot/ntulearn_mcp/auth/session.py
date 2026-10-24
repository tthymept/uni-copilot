"""
Sign in to NTULearn with a real browser and hand the cookies to `requests`.

How it works:
- Playwright opens Chromium with a *persistent profile* (.auth/profile). Like your
  everyday Chrome, that profile remembers Microsoft's "Stay signed in", so NTU's
  single sign-on usually finishes by itself in a few seconds.
- As soon as NTULearn says we're signed in (users/me -> 200), we copy the NTULearn
  cookies out of the browser and close it. All data calls then use plain
  `requests` with those cookies (client/http.py). The browser never reads data.
- Your password never passes through this code: if a sign-in is needed, you type
  it yourself in the visible window.

Safety:
- While the page is open, any non-GET call the NTULearn web app makes to /learn/api/
  is blocked (the Ultra page sends a PUT batch on load by itself).
- Logs show cookie names and expiry only, never values.
"""

import argparse
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from requests.cookies import RequestsCookieJar

from uni_copilot.ntulearn_mcp.config import (
    BASE_URL,
    LOGIN_TIMEOUT_S,
    PROFILE_DIR,
    SILENT_LOGIN_TIMEOUT_S,
)

log = logging.getLogger(__name__)

# Any Ultra page works: it redirects to NTU sign-in when needed.
START_URL = f"{BASE_URL}/ultra/course"
ME_URL = f"{BASE_URL}/learn/api/v1/users/me"
POLL_MS = 2000

# Microsoft's sign-in form (email or password box). Not verified on NTU's page;
# if it doesn't match, the silent attempt simply waits for its timeout instead.
SIGN_IN_FORM = "input[name=loginfmt], input[type=password]"

# Chromium allows one process per profile folder, so only one sign-in at a time.
_browser_lock = threading.Lock()


class AuthError(Exception):
    """The browser could not start, or something else went wrong while signing in."""


class LoginRequired(AuthError):
    """Signing in needs you (a visible window), or you didn't finish in time."""


@dataclass(frozen=True)
class LoginResult:
    cookies: RequestsCookieJar
    used_window: bool  # True if you signed in in a visible window
    seconds: float


def sign_in(allow_window: bool = False) -> LoginResult:
    """
    Get fresh NTULearn cookies.

    Always tries a hidden browser first. If that can't finish by itself and
    allow_window is True, opens a visible window for you to sign in.
    Raises LoginRequired if a sign-in needs you but allow_window is False.
    """
    with _browser_lock:
        start = time.monotonic()
        jar = _attempt(headless=True, timeout_s=SILENT_LOGIN_TIMEOUT_S)
        if jar is not None:
            return LoginResult(jar, used_window=False, seconds=time.monotonic() - start)
        if not allow_window:
            raise LoginRequired("NTULearn needs you to sign in. Run the login tool.")

        log.info("silent sign-in not possible, opening a window")
        jar = _attempt(headless=False, timeout_s=LOGIN_TIMEOUT_S)
        if jar is None:
            raise LoginRequired(f"Sign-in not finished within {LOGIN_TIMEOUT_S // 60} minutes.")
        return LoginResult(jar, used_window=True, seconds=time.monotonic() - start)


def cookie_summary(jar: RequestsCookieJar) -> list[str]:
    """Cookie names and expiry, safe to show. 'session' = gone when the browser closes."""
    lines = []
    for c in sorted(jar, key=lambda c: c.name):
        if c.expires:
            when = datetime.fromtimestamp(c.expires, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        else:
            when = "session"
        lines.append(f"{c.name} (expires: {when})")
    return lines

# One complete browser run: open the profile, block non-GET API calls, go to NTULearn, wait, copy cookies, close. 
# It returns the jar, or None if it didn't get signed in. sign_in calls it once hidden, then maybe once with a window.
def _attempt(headless: bool, timeout_s: float) -> RequestsCookieJar | None:
    """One browser run. Returns cookies if signed in, None if not within timeout_s."""
    mode = "hidden" if headless else "window"
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless)
            try:
                context.route("**/learn/api/**", _allow_get_only)
                page = context.pages[0] if context.pages else context.new_page()
                # "commit" returns as soon as navigation starts; the loop below does the waiting.
                page.goto(START_URL, wait_until="commit")
                if not _wait_until_signed_in(context, page, timeout_s, give_up_on_form=headless):
                    log.info("sign-in (%s): not signed in", mode)
                    return None
                page.goto("about:blank")  # leave the Ultra app so it stops making calls
                jar = _to_jar(context)
                log.info("sign-in (%s): ok, %d cookies", mode, len(jar))
                return jar
            finally:
                context.close()
    except PlaywrightError as e:
        # Only the error type: Playwright messages can contain full URLs.
        raise AuthError(
            f"Browser failed ({mode}, {type(e).__name__}). "
            "Is another program (e.g. the spike script) using .auth/profile?"
        ) from None


# Waiting loop. Every 2 s it asks users/me: 200 means done. In hidden mode it also quits early if Microsoft shows a sign-in box.
def _wait_until_signed_in(
    context: BrowserContext, page: Page, timeout_s: float, give_up_on_form: bool
) -> bool:
    deadline = time.monotonic() + timeout_s
    told_user = False
    while time.monotonic() < deadline:
        if context.request.get(ME_URL).status == 200:
            return True
        if give_up_on_form and _sign_in_form_showing(page):
            return False  # a human has to type something; no point waiting
        if not give_up_on_form and not told_user:
            log.info("waiting for you to sign in in the browser window")
            told_user = True
        # Not time.sleep(): Playwright only handles events (like our route
        # handler) while one of its own calls is running.
        page.wait_for_timeout(POLL_MS)
    return False


# answers "is the browser on Microsoft's page with an email or password box visible?"
def _sign_in_form_showing(page: Page) -> bool:
    if "login.microsoftonline.com" not in (urlparse(page.url).hostname or ""):
        return False
    try:
        return page.locator(SIGN_IN_FORM).first.is_visible()
    except PlaywrightError:
        return False  # page was mid-navigation


# The guard called for every API request the NTULearn page makes: GET goes through, anything else is aborted.
def _allow_get_only(route: Route) -> None:
    method = route.request.method
    if method == "GET":
        route.continue_()
    else:
        log.info("blocked a %s from the NTULearn web page", method)
        route.abort()


# Copies the browser's NTULearn cookies (name, value, domain, path, expiry) into a RequestsCookieJar.
def _to_jar(context: BrowserContext) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    for c in context.cookies(BASE_URL):
        expires = c.get("expires", -1)
        jar.set(
            c["name"],
            c["value"],
            domain=c["domain"],
            path=c["path"],
            secure=c.get("secure", False),
            expires=int(expires) if expires and expires > 0 else None,
        )
    return jar


# The self-test you ran with python -m ...auth.session. It isn't used by the server.
def _main() -> None:
    """Self-test: uv run python -m uni_copilot.ntulearn_mcp.auth.session [--window]"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", action="store_true",
                    help="allow a visible window if a silent sign-in isn't possible")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="  [%(name)s] %(message)s")

    try:
        result = sign_in(allow_window=args.window)
    except AuthError as e:
        print(f"Not signed in: {e}")
        return
    how = "visible window" if result.used_window else "hidden browser"
    print(f"Signed in with a {how} in {result.seconds:.0f} s. Cookies (names + expiry only):")
    for line in cookie_summary(result.cookies):
        print(f"  {line}")


if __name__ == "__main__":
    _main()
