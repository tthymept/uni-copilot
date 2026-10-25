# Phase 1: ntulearn-mcp core

Status (24 Oct 2026): all 16 steps done. **Phase 1 is complete.** Full details of every step: `phase-1-build-log.md` in this folder. Next: Phase 2 (event store).

## What Phase 1 builds

`ntulearn-mcp`, a small local server that Claude Code can call. An MCP server is a program that offers "tools" (Python functions) to Claude. Claude Code starts it in the background and talks to it through stdin/stdout.

It has 6 read-only tools: `login_status`, `login`, `list_courses`, `upcoming_deadlines`, `recent_announcements` and `recent_messages`.

Done when: you ask Claude Code "what's due this week?" and the answer matches NTULearn's Due dates page.

## Steps

One file per step. You test each step before the next.

0. Housekeeping: `.gitignore`, plan doc updated with the probe findings, `watch_log.txt` removed. **Done.**
1. `pyproject.toml`: `mcp>=2.3,<3`, `tzdata`, `pytest`. **Done.**
2. `ntulearn_mcp/config.py` and the package folders. **Done.**
3. `auth/session.py`: hidden sign-in first, visible window as fallback. **Done.**
4. `client/http.py`: the only door to NTULearn. GET only, paced, retries, silent re-login. **Done.**
5. `models.py`: typed result shapes. **Done.**
6. `client/courses.py`: memberships, the current term (newest YYS1/S2, or `NTULEARN_TERM`), grouping lecture and tutorial sites under one subject code, and matching a course from text you type ("IE2108", "data structures"). **Done.**
7. `client/calendar.py`: calendar items in windows of 16 weeks or less. **Done.**
8. `client/gradebook.py`: columns with due dates, plus your status. Reads only `columnId`, `status` and `exempt`; grade fields are never read. **Done.**
9. `client/deadlines.py`: combines the calendar and gradebook lists, removes duplicates by column id, filters by exact due time, converts times from UTC to SGT. **Done.**
10. `parsers/html.py`: announcement and message HTML to light Markdown, plus links and files as separate lists (changed from plain text in step 11, your suggestion), standard library only. **Done.**
11. `client/announcements.py`: announcements from the last N days, edited ones included, duplicates across lecture and tutorial sites merged. **Done.**
12. `client/messages.py`: course conversations from the last N days; only threads active in the window are fetched in full. **Done.**
13. `server.py`: creates the MCP server and the 6 tools, each marked `read_only_hint=True`. Text from other people is labelled untrusted. Errors are raised as `ToolError` so Claude sees them. Also adds the `ntulearn-mcp` command (moved here from step 1). Test: `uv run mcp dev ...` opens the MCP Inspector, where you can click each tool. **Done.**
14. `.mcp.json` and `.claude/settings.json`: register the server with Claude Code and pre-allow its read-only tools (all except `login`). Settings like `NTULEARN_TERM` can go under `"env"` in `.mcp.json`. **Done.**
15. `tests/`: offline tests with made-up data (term picking, merging deadlines, status labels, SGT times, HTML to text, 429/5xx retries against a fake server), plus an opt-in live smoke test that prints counts only. 78 offline + 4 live. **Done.**
16. Final check: ask Claude Code "what's due this week?" and compare with NTULearn's Due dates page. Matched; one bug found and fixed (message senders all showed as "GIVEN_NAME"). **Done.**

Commit status: all steps committed up to `90bdd5d`. Moving these docs into `docs/phase 1/` (and the fixes after a VS Code save overwrote two files) is in the next commit.

## Decisions

Recorded in the plan doc during step 0.

- **Pace:** one step at a time. Claude builds it and explains it, you run the test, then say "next".
- **Submission status:** read your gradebook row, one request per course, and drop the grade fields immediately. Nothing is ever returned or saved. (Not chosen: the attempts endpoint, which has no grades but needs about 30 to 90 requests per call.) This touches grade data in memory even when grades are off.
- **Silent re-login:** hidden browser first, a window only if you need to sign in. Verified working in step 3.
- **"Opens/closes" entries:** moved to Phase 4. They need content availability windows, which aren't verified, and the content tree crawler.
- **Course nicknames ("DSA" → IE2108):** match by code or by words in the course name for now. Personal alias file later.

## Changes from the original Phase 1 plan

- **No custom thread code.** The plan said MCP servers run on asyncio and Playwright's sync API crashes inside an asyncio loop, so login and HTTP would need a worker thread. Step 1 found the SDK already runs plain `def` tools in a worker thread (`anyio.to_thread.run_sync` in `resolve.py`). So normal synchronous code with `requests` and sync Playwright works. The catch: two tool calls can run at the same time in different threads, so the rate limiter needs a lock (done in step 4) and each login opens and closes its own browser (done in step 3).
- **`ntulearn-mcp` command moved from step 1 to step 13.** Its `[project.scripts]` entry points at `server.py:main`, which doesn't exist until step 13.
- **`not_submitted` instead of "not started"** (step 5). NTULearn can only tell us nothing was submitted, not whether you've started.
- More changes came later (an `exempt` status, Markdown bodies, shared helpers, small in-memory caches, and others): the full list of 12 is in "Changes from the original Phase 1 plan" in `phase-1-build-log.md`.

## Connection probe results (24 Oct 2026, your account)

Three throwaway test scripts (in Claude's scratch folder, not your repo) borrowed your saved login from `.auth/profile`, copied the cookies into `requests`, and sent 72 GET requests in total, about one per second. They printed only status codes, counts and field names. No content, grades or cookie values.

- **Login:** silent sign-in in 4 s, `users/me` returned 200. `watch_log.txt` showed the session lasting 4 hours with a check every 10 min, so the Phase 0 spike passed.
- **Courses:** 57 memberships in total. 26S1 has 16 sites covering 8 subjects: CC0006, CL0105, DA3000, IE0005, IE2106, IE2108, IE2110, ML0004.
- **Terms:** the API has `startDate`/`endDate` fields, but they're empty. "Current term" has to come from the term name (26S1), as planned.
- **Calendar API:** 200. The next 14 days have 6 items, all of type `GradebookColumn`. Asking for 17 weeks returns 400, which confirms the 16-week limit.
- **Gradebook columns:** 200 on all 16 sites, 88 columns with due dates.
- **Submission status:** the "my gradebook row" endpoint gives `NeedsGrading` (submitted, not marked yet), `Graded`, or no row at all (nothing submitted). For future deadlines: 29 have no row and 1 is `NeedsGrading`. The attempts endpoint also works and has a status without a score, but costs one request per assignment.
- **Announcements:** 200 on both the public v1 endpoint and the internal one, with the same counts.
- **Messages:** `/courses/{id}/messages` returns 404. The working endpoint is `GET /learn/api/v1/courses/{id}/conversations` (undocumented), which returned 5 conversations, each with a list of messages. (Corrected in step 12: that list holds only each conversation's newest message, as a preview; the full thread comes from `/conversations/{id}/messages`.)
- **NTULearn's own PUT:** when the browser opens `/ultra/course`, NTULearn's web page sends a `PUT /utilities/batch` by itself, the same as opening NTULearn in Chrome. None of our requests did it. The login step leaves that page as soon as you're signed in, and our HTTP client has no method except GET.