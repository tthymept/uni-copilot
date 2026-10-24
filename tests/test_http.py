"""
Offline tests for client/http.py: GET-only, pacing, retries, re-login, paging.

In plain words:
  We replace the network call (`requests.Session.get`) with a fake that hands
  out made-up responses from a list, and replace time.sleep with a recorder,
  so retries and waits are checked instantly without touching NTULearn.
"""

from typing import Any

import pytest
import requests
from requests.cookies import RequestsCookieJar

from uni_copilot.ntulearn_mcp.auth.session import LoginRequired, LoginResult
from uni_copilot.ntulearn_mcp.client import http as http_mod
from uni_copilot.ntulearn_mcp.client.http import NtuLearnError, NtuLearnHttp, _Pacer
from uni_copilot.ntulearn_mcp.config import BASE_URL


# A made-up HTTP response with just what NtuLearnHttp reads.
class FakeResponse:
    def __init__(self, status: int, body: Any = None, url: str | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.url = url or BASE_URL + "/learn/api/x"
        self.headers = headers or {}

    # Like requests: ok means status below 400.
    @property
    def ok(self) -> bool:
        return self.status_code < 400

    # Returns the body, or raises ValueError like requests does for non-JSON.
    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


# Builds an NtuLearnHttp whose network, sign-in and clock are fakes. `responses`
# are handed out in order (an Exception is raised instead of returned). Time only
# moves when something "sleeps", so the pacer's 1 s spacing is already used up by
# any retry wait and `sleeps` shows exactly the retry waits. Returns the client
# plus lists recording sign-ins, requests and sleeps.
@pytest.fixture
def make_http(monkeypatch):
    def build(responses: list[Any]):
        sign_ins: list[bool] = []
        requests_seen: list[tuple[str, Any]] = []
        sleeps: list[float] = []
        clock = [1000.0]

        # Fake sleep: records the wait and moves the fake clock forward.
        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        # Fake sign-in: records whether a window was allowed, returns empty cookies.
        def signer(allow_window: bool = False) -> LoginResult:
            sign_ins.append(allow_window)
            return LoginResult(cookies=RequestsCookieJar(), used_window=allow_window, seconds=0.0)

        client = NtuLearnHttp(signer=signer)

        # Fake network: records the URL and params, returns the next response.
        def fake_get(url, params=None, timeout=None):
            requests_seen.append((url, params))
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(client._session, "get", fake_get)
        monkeypatch.setattr(http_mod.time, "sleep", fake_sleep)
        monkeypatch.setattr(http_mod.time, "monotonic", lambda: clock[0])
        return client, sign_ins, requests_seen, sleeps
    return build


# The class has no way to send anything but GET.
def test_get_only_by_design():
    for name in ("post", "put", "patch", "delete", "request"):
        assert not hasattr(NtuLearnHttp, name)


# The first call signs in silently (never with a window), then returns the JSON.
def test_signs_in_silently_on_first_call(make_http):
    client, sign_ins, seen, _ = make_http([FakeResponse(200, {"id": "_u_1"})])
    assert client.is_signed_in() is False  # no cookies yet, and no request made
    assert client.get_json("/learn/api/v1/users/me") == {"id": "_u_1"}
    assert sign_ins == [False]
    assert seen[0][0] == BASE_URL + "/learn/api/v1/users/me"


# 401 -> one silent re-login -> the call is repeated and succeeds.
def test_401_relogin_once(make_http):
    client, sign_ins, _, _ = make_http([FakeResponse(401), FakeResponse(200, {"ok": 1})])
    assert client.get_json("/a") == {"ok": 1}
    assert sign_ins == [False, False]  # first sign-in + one re-login


# Still 401 after the re-login -> LoginRequired (Claude will suggest the login tool).
def test_401_twice_raises_login_required(make_http):
    client, sign_ins, _, _ = make_http([FakeResponse(401), FakeResponse(401)])
    with pytest.raises(LoginRequired):
        client.get_json("/a")
    assert len(sign_ins) == 2


# Being sent to Microsoft's sign-in page counts as signed out, like a 401.
def test_redirect_away_counts_as_signed_out(make_http):
    client, sign_ins, _, _ = make_http([
        FakeResponse(200, {}, url="https://login.microsoftonline.com/abc"),
        FakeResponse(200, {"ok": 1}),
    ])
    assert client.get_json("/a") == {"ok": 1}
    assert len(sign_ins) == 2


# 429 with Retry-After: wait exactly that long, then retry.
def test_429_honours_retry_after(make_http):
    client, _, _, sleeps = make_http([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {"ok": 1})])
    assert client.get_json("/a") == {"ok": 1}
    assert sleeps == [7.0]


# 5xx: retry with 2, 4, 8 s waits, then give up with NtuLearnError.
def test_5xx_backoff_then_error(make_http):
    client, _, seen, sleeps = make_http([FakeResponse(503)] * 4)
    with pytest.raises(NtuLearnError) as err:
        client.get_json("/a")
    assert err.value.status == 503
    assert len(seen) == 4
    assert sleeps == [2.0, 4.0, 8.0]


# A network error is retried like a 5xx.
def test_network_error_retried(make_http):
    client, _, _, _ = make_http([requests.ConnectionError("reset"), FakeResponse(200, {"ok": 1})])
    assert client.get_json("/a") == {"ok": 1}


# 404 is not retried; the error carries status and path, never the body.
def test_404_no_retry(make_http):
    client, _, seen, _ = make_http([FakeResponse(404, {"secret": "body text"})])
    with pytest.raises(NtuLearnError) as err:
        client.get_json("/learn/api/v1/missing")
    assert len(seen) == 1
    assert err.value.status == 404 and "missing" in str(err.value)
    assert "secret" not in str(err.value)


# A 200 that isn't JSON (e.g. an HTML page) is an error, not a crash.
def test_non_json_is_error(make_http):
    client, _, _, _ = make_http([FakeResponse(200, None)])
    with pytest.raises(NtuLearnError, match="not JSON"):
        client.get_json("/a")


# get_all follows nextPage; the first call's params are not sent again.
def test_get_all_follows_paging(make_http):
    client, _, seen, _ = make_http([
        FakeResponse(200, {"results": [1, 2], "paging": {"nextPage": "/list?offset=2"}}),
        FakeResponse(200, {"results": [3]}),
    ])
    assert client.get_all("/list", {"limit": 2}) == [1, 2, 3]
    assert seen == [(BASE_URL + "/list", {"limit": 2}), (BASE_URL + "/list?offset=2", None)]


# Paths must start with "/" (the base URL is always added here).
def test_path_must_be_relative(make_http):
    client, _, _, _ = make_http([])
    with pytest.raises(ValueError):
        client.get_json("https://elsewhere.example/x")


# The pacer spaces requests 1 s apart, using a fake clock.
def test_pacer_spacing(monkeypatch):
    clock = [100.0]
    sleeps: list[float] = []

    # Fake sleep: advances the fake clock instead of waiting.
    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(http_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http_mod.time, "sleep", fake_sleep)
    pacer = _Pacer(1.0)
    for _ in range(3):
        pacer.wait()
    assert sleeps == [1.0, 1.0]
