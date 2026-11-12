"""
Offline tests for auth/session.py: which error each sign-in failure raises.

In plain words:
  The retry in the sync must only repeat a browser failure, so the three kinds
  of failure have to stay different errors. We replace Playwright, the lock and
  the attempt with fakes, so no browser opens and the real .auth folder isn't touched.
"""

from contextlib import contextmanager

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from uni_copilot.ntulearn_mcp.auth import session
from uni_copilot.ntulearn_mcp.auth.session import AuthError, BrowserFailed, LoginRequired


# Stands in for sync_playwright: entering its `with` raises the given error,
# as if the browser couldn't start.
def failing_playwright(error: Exception):
    @contextmanager
    def fake():
        raise error
        yield

    return fake


# Keeps the profile folder and its lock file away from the real .auth folder.
@pytest.fixture(autouse=True)
def temp_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(session, "PROFILE_LOCK", tmp_path / "profile.lock")


# Both errors seen in the sync log become BrowserFailed, keeping the error's type
# in the text but not its message (Playwright messages can contain full URLs).
@pytest.mark.parametrize("error, type_name", [
    (PlaywrightError("net::ERR_FAILED at https://example.org/secret"), "Error"),
    (PlaywrightTimeout("Timeout 30000ms exceeded at https://example.org/secret"), "TimeoutError"),
])
def test_browser_error_becomes_browser_failed(monkeypatch, error, type_name):
    monkeypatch.setattr(session, "sync_playwright", failing_playwright(error))
    with pytest.raises(BrowserFailed) as caught:
        session.sign_in()
    text = str(caught.value)
    assert f"Browser failed (hidden, {type_name})" in text
    assert "example.org" not in text


# Other code catches AuthError (server.py), so BrowserFailed has to be one.
def test_browser_failed_is_an_auth_error():
    assert issubclass(BrowserFailed, AuthError)


# "You must sign in" isn't a browser failure: waiting wouldn't help.
def test_needing_you_is_not_browser_failed(monkeypatch):
    monkeypatch.setattr(session, "_attempt", lambda headless, timeout_s: None)
    with pytest.raises(LoginRequired) as caught:
        session.sign_in()
    assert not isinstance(caught.value, BrowserFailed)


# Another program holding the profile isn't one either: sign_in already waited for it.
def test_profile_lock_timeout_is_not_browser_failed(monkeypatch):
    @contextmanager
    def lock_not_got(path, wait_s=0):
        yield False

    monkeypatch.setattr(session, "file_lock", lock_not_got)
    with pytest.raises(AuthError) as caught:
        session.sign_in()
    assert not isinstance(caught.value, BrowserFailed)
