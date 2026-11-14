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

### Step 0 results so far (log check, 14 Nov)

From `.data\sync.log`, 12 Nov 14:48 to 14 Nov 19:26 (the first 18 syncs with the retry):

- **16 worked at the first sign-in** (no retry line).
- **2 needed the retry, and neither recovered:**
  - **Fri 13 Nov 05:13:** the retry line, then nothing: no source lines, no `exit code`. The run was cut off during the 60 s wait (the next sync is 09:38). A retry can't help a run that is cut off.
  - **Fri 13 Nov 21:17:** the second try failed too (`BrowserFailed` on all three sources, exit code 1). The 21:38 sync, 21 minutes later, worked.
- So the retry runs as built, but **it hasn't rescued a sync yet (0 of 2)**. Two cases are too few to judge. Not compared with Windows' standby events yet.

## Step 2: the calendar trial

No code. You connected Claude's Google Calendar connector in the terminal session (`/mcp` → "claude.ai Google Calendar", 14 Nov). Shape only below: no dates, titles or ids from the calendar.

### Part 1: the read-only look (14 Nov)

Read with the connector's `list_calendars` and `list_events` (two weeks back, four weeks ahead). Nothing was written.

- **Four calendars:** your main one, a separate school-timetable calendar, a public-holidays calendar and a club calendar. **`list_events` reads only the main calendar unless told otherwise**, so a schedule question has to ask for the timetable calendar too (by its id from `list_calendars`).
- **Classes:** in the timetable calendar, as weekly repeating events. Title `[CODE] TYPE (COURSE NAME)` (TYPE = LEC/STUDIO, TUT, LAB), room in the location. They stop after teaching week 13. A few single classes were moved by hand (one to a different week, one to a later start), which the connector returns correctly as changed instances.
- **Exams:** in the timetable calendar, as single events titled `[CODE] EXAM (COURSE NAME)`, with start and end times. **No venue** (location empty) and **no notification of their own** (the calendar's default only).
- **Quizzes, presentations, personal things:** in the main calendar, typed by hand. Titles are free text and often use a short course name instead of the code; some have a location. Default notification: one pop-up 30 minutes before.
- **Free/busy flag:** at least one test is marked "free" (the event doesn't block time). A free-time answer that trusts that flag would plan over it.
- **Teaching weeks, recess, exam period:** not in any calendar.
- **Time zones:** events carry mixed zone labels (an import left some as UTC), but every time comes back with the +08:00 offset and is correct in SGT.

What this means for the decisions (answered after part 2):

- **Decision 5 (teaching weeks):** the calendar can't answer "which week is it?"; that needs the academic calendar file and `week_info`, if wanted.
- **Decision 6 (exam reminders):** Google gives no "exam tomorrow" today: only the default pop-up shortly before, and no venue. Either add notifications and venues to the exam events by hand, or import the exams into the store for `remind`.
- **Decision 7 (free time):** has to read two calendars and ignore the free/busy flag; whether Claude gets it right is part 2.

### Part 2: your questions

Open: 4–5 real questions from you, answers checked by you.

### Part 3: the Telegram session

Open: whether the locked Telegram session can be allowed the connector's read tools (decision 4). Not tried.

## Open items

- **The calendar trial is half done:** the look is finished; your questions and the Telegram check are open (step 2, parts 2 and 3).

- **Does the retry fix the standby failures? Not so far** (0 of 2 by 14 Nov, see "Step 0 results so far"). Keep looking in `.data\sync.log` for `trying once more` and whether the source lines after it worked. If the second try also fails often, a longer wait (or a second retry) is the next idea; if Windows cuts the run off (`0x41306`), no retry can help.
- **Likely cause of the failures is not verified:** the network not being back yet is a guess supported by one log match (2 Nov 17:56).
- **A sign-in that fails later, in the middle of a sync** (a 401 → new sign-in inside a source), isn't retried: that source fails as before. Not seen in the log so far.

## Concepts

- **Exception subclass:** `class BrowserFailed(AuthError)` makes a more specific kind of error. `except AuthError` still catches it, and `except BrowserFailed` catches only it, so old code keeps working while new code can pick out this one case. `LoginRequired` was already built this way.
- **Passing a function in (`sleep=time.sleep`):** `run_sync` doesn't call `time.sleep` directly; it calls whatever it was given, and the real `time.sleep` is the default. Tests give it `waits.append` instead, which just writes down "60" and returns at once, so a test can check the wait happened without waiting.
