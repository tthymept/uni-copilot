# Phase 6 build log

What each finished step changed, how to test it, and its commit. Unverified points from each step are collected in the "Open items" section at the bottom, and new terms in the "Concepts" section. The plan, steps and decisions are in `phase-6-core.md` in this folder. (Phases 6 and 7 were swapped on 7 Nov: Phase 6 reads and plans your schedule, Phase 7 writes with your permission. This log started as "Phase 7".)

## How it all connects (read this first when you come back)

### Step 0: the sign-in retry

Carried over from Phase 3 (your decision, 7 Nov). A sync that starts in the minute the laptop leaves standby often fails at the sign-in; the fix is to wait about a minute and try once more.

```
uni-copilot sync / the digest's sync
  └─ store/sync.py: run_sync()
       └─ NtuLearnHttp.ensure_signed_in()   (client/http.py)
            └─ sign_in()                    (auth/session.py)
                 └─ _attempt()              one browser run

What sign_in can raise, and whether waiting could help:
  BrowserFailed    the browser itself failed       maybe: try again (new, step 0a)
  LoginRequired    you must sign in                no
  AuthError        another program held the lock   no: sign_in already waited 2 minutes

Since step 0b, run_sync signs in through _sign_in_with_retry():
  ensure_signed_in() ── ok ─────────────────────────────► fetch the 3 sources
        │ BrowserFailed
        ▼
  log "trying once more in 60 s", wait 60 s
  ensure_signed_in() ── ok ─────────────────────────────► fetch the 3 sources
        │ any error
        ▼
  all 3 sources FAILED, old data kept (as before)
```

## Step 0a: auth/session.py (`BrowserFailed`, a failure worth retrying)

**Why this comes first:** the retry may only repeat a browser failure. Until now that failure and "another program held the profile for over 2 minutes" were both a plain `AuthError`, so code couldn't tell them apart.

**`src/uni_copilot/ntulearn_mcp/auth/session.py`:**

- **a. New class `BrowserFailed(AuthError)`.** `_attempt()` raises it when Playwright raises an error. Playwright's `TimeoutError` is one kind of its `Error`, so both errors in the sync log land here.
- **b. New error text.** "Is another program using .auth/profile?" was misleading for this failure (Phase 3 wrap-up). Now: `Browser failed (hidden, Error). Mostly seen just after the laptop leaves standby: try again in a minute. If it keeps failing, close anything else using .auth/profile.` It still carries only the error's type, never its message, because Playwright messages can contain URLs.
- **c. Comments:** `sign_in`'s docstring and the comment above `_attempt` say what's raised.

**What stays the same:** `BrowserFailed` is still an `AuthError`, so `server.py`, the indexer and the self-tests catch it as before. Nothing retries yet.

**What looks different:** `.data\sync.log`, `uni-copilot status` and pilot's `sync_status` will say `BrowserFailed: Browser failed (hidden, Error)...` instead of `AuthError: Browser failed...`. Nothing reads that first word (checked `src/`; `cli.py` only looks for `LoginRequired`). Older log lines keep the old word, so search the log for `Browser failed` to find both.

**New file `tests/test_session.py`** (offline, no browser, doesn't touch the real `.auth` folder): both Playwright errors become `BrowserFailed` and no URL gets into the text; `BrowserFailed` is an `AuthError`; `LoginRequired` and the lock timeout are not `BrowserFailed`.

Checks (7 Nov, offline): `uv run --no-sync pytest` → 277 passed, 4 skipped (272 before, plus these 5). Not run on the live site: the normal sign-in path wasn't changed.

Check (PowerShell):

```
uv run --no-sync pytest tests/test_session.py -v
uv run --no-sync pytest
uv run --no-sync python -m uni_copilot.ntulearn_mcp.auth.session
```

Expect 5 passed, then "277 passed, 4 skipped", then `Signed in with a hidden browser in N s.` and cookie names with expiry dates (live, your account; no values shown).

Seen live (11 Nov 20:56, `.data\sync.log`): all three sources failed with `BrowserFailed: Browser failed (hidden, Error)...`, so the new name and text reach the log. The 22:39 sync worked.

Commit: together with steps 0b and 0c (see the end of step 0c).

## Step 0b: store/sync.py (the retry)

**`src/uni_copilot/store/sync.py`:**

- **a. New constant `SIGN_IN_RETRY_S = 60`.** The standby failures happened in the minute after waking, so one minute.
- **b. New function `_sign_in_with_retry(http, sleep)`.** Calls `ensure_signed_in()`. On `BrowserFailed` only, it logs a warning, waits 60 s and calls it once more. Any other error, or a second `BrowserFailed`, goes up unchanged, so everything after it works as before (all three sources marked failed, old data kept, the ⚠️ alert after 6 h). The second call really starts a new browser: `NtuLearnHttp` keeps no record of the failure (its cookie version is still 0).
- **c. `run_sync` has a new parameter `sleep`** (default `time.sleep`). Tests pass a fake that only records the wait, so they don't pause for a minute.
- **d. The module docstring** names the new rule.

**Why in the sync and not in `sign_in()`:** the sync signs in once and stops if that fails, so the retry costs at most one minute per sync. The wait holds no lock on `.auth/profile`, and the live `ntulearn` tools in Claude still answer at once instead of hanging for a minute.

**Timing:** a sync that needs the retry takes about 2 minutes (60 s wait + a ~6 s sign-in + ~64 s of fetching). The digest waits up to 3 minutes for a running sync, so it still gets the fresh data; if it ever waits longer, it goes ahead with the store as it is (unchanged). The sync task's 15-minute limit is far off.

**What looks different in `.data\sync.log`:** a sync that needed the second try has one extra line before the source lines:

```
  [uni_copilot.store.sync] sign-in: the browser failed; trying once more in 60 s
```

If the source lines after it say `seen`, the retry worked; if they say `FAILED: BrowserFailed`, both tries failed.

**`tests/test_sync.py`:** the fake's `sign_in_error` became `sign_in_errors` (a list, raised one per sign-in, then it works) plus a `sign_ins` counter. New tests: one browser failure → one 60 s wait, two sign-ins, a normal sync; two failures → one wait, every source `BrowserFailed`, nothing saved; `LoginRequired` and the profile-lock `AuthError` → no wait, one sign-in.

**Also tidied:** the reminders self-test (`store/reminders.py`) used the old text `AuthError: Browser failed...` for its made-up sync failure; now `BrowserFailed: ...`, as the real log says. A trailing space in `auth/session.py`.

Checks (12 Nov, offline): `uv run --no-sync pytest` → 281 passed, 4 skipped (277 before, plus 4). The reminders self-test still shows the sync alert. Not tried live: the retry only runs when the browser fails, which can't be caused on purpose; the log will show it.

Check (PowerShell):

```
uv run --no-sync pytest tests/test_sync.py -v
uv run --no-sync pytest
uv run uni-copilot sync
```

Expect the new tests passed, then "281 passed, 4 skipped", then a normal sync (no retry line when nothing fails).

## Step 0c: docs

- **`docs/how-it-runs.md`:** sync's "If it fails" now has a "Sign-in retry" line with the log text; the "Known weak spot" says the retry exists and how to check it; the "When is it down?" row for standby says the same.
- **`docs/phase 3/phase-3-build-log.md`:** the open item points here.
- **`docs/uni-copilot-project-plan.md`:** status line and the Phase 6 roadmap item (✓, being checked).
- **`docs/phase 6/phase-6-core.md`:** status, steps 0b and 0c.

Commit (steps 0a–0c and the 7 Nov housekeeping together):

```
git add README.md docs src/uni_copilot/ntulearn_mcp/auth/session.py src/uni_copilot/store/sync.py src/uni_copilot/store/reminders.py tests/test_session.py tests/test_sync.py
git commit
```

```
feat(sync): retry the sign-in once when the browser fails

- auth/session.py: BrowserFailed(AuthError) for Playwright failures,
  with a "try again in a minute" message (type only, never the URL)
- store/sync.py: on BrowserFailed, wait 60 s and sign in once more
  before marking the sources failed; LoginRequired isn't retried
- tests: which error each sign-in failure raises; the retry (once,
  twice, not for other errors)
- docs: phase 6 plan and build log, how-it-runs; roadmap reordered
  (6 reads and plans the schedule, 7 writes with permission)
```

## Open items

- **Does the retry fix the standby failures? Not verified yet.** Over the next days, look in `.data\sync.log` for `trying once more` and check that the source lines after it worked. If the second try also fails often, a longer wait (or a second retry) is the next idea; if Windows cuts the run off (`0x41306`), no retry can help.
- **Likely cause of the failures is not verified:** the network not being back yet is a guess supported by one log match (2 Nov 17:56).
- **A sign-in that fails later, in the middle of a sync** (a 401 → new sign-in inside a source), isn't retried: that source fails as before. Not seen in the log so far.

## Concepts

- **Exception subclass:** `class BrowserFailed(AuthError)` makes a more specific kind of error. `except AuthError` still catches it, and `except BrowserFailed` catches only it, so old code keeps working while new code can pick out this one case. `LoginRequired` was already built this way.
- **Passing a function in (`sleep=time.sleep`):** `run_sync` doesn't call `time.sleep` directly; it calls whatever it was given, and the real `time.sleep` is the default. Tests give it `waits.append` instead, which just writes down "60" and returns at once, so a test can check the wait happened without waiting.
