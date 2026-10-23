"""
Spike: test the NTULearn login and session assumptions before building the MCP server.

What it checks:
  1. Does the saved browser profile sign you in silently, or do you have to sign in?
  2. Do cookies copied out of the browser work with plain `requests`? (NTULearn)
  3. Same question for the lecture video site (NTULearnVideo / Kaltura).
  4. (optional, --watch) How long do those copied sessions keep working?

Safety: GET requests only, your own account. Prints status codes, cookie NAMES and
expiry dates only. Never prints cookie values, tokens or download links.

This is a throwaway test script, not part of the server.
"""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "https://ntulearn.ntu.edu.sg"
VIDEO = "https://ntulearnv1.ntu.edu.sg"

# Browser profile that remembers your sign-in. Contains login cookies: never commit it.
PROFILE_DIR = Path(".auth/profile")

# IE2108 Media Gallery (the course checked earlier). Change these to test another course.
COURSE_ID = "_2706657_1"
MEDIA_GALLERY_CONTENT_ID = "_5786611_1"
PLACEMENT_ID = "_1327_1"
ENTRY_ID = "0_wo0y67pt"  # one recording in that gallery

LAUNCH_URL = (
    f"{BASE}/webapps/blackboard/execute/blti/launchPlacement"
    f"?blti_placement_id={PLACEMENT_ID}&content_id={MEDIA_GALLERY_CONTENT_ID}"
    f"&course_id={COURSE_ID}&wrapped=true"
)
ATTACHMENTS_URL = f"{VIDEO}/attachments/index/index/entryid/{ENTRY_ID}/entryView/1?format=ajax"


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def describe_cookies(context, url: str) -> None:
    """Print cookie names and expiry only. 'session' = deleted when the browser closes."""
    for c in sorted(context.cookies(url), key=lambda c: c["name"]):
        exp = c.get("expires", -1)
        if exp is None or exp <= 0:
            when = "session"
        else:
            when = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"     {c['name']:<34} expires: {when}")


def to_requests_session(context, url: str) -> requests.Session:
    """Copy the browser's cookies for `url` into a plain requests session."""
    s = requests.Session()
    for c in context.cookies(url):
        s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return s


def bb_signed_in(context) -> bool:
    r = context.request.get(f"{BASE}/learn/api/v1/users/me")
    return r.status == 200


def check_video(session: requests.Session) -> tuple[int, int]:
    """Return (status code, number of transcript download links seen)."""
    r = session.get(ATTACHMENTS_URL, timeout=30)
    links = r.text.count("attachment_attachmentasset") if r.status_code == 200 else 0
    return r.status_code, links


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--watch", type=int, default=0,
        help="minutes to keep re-checking with plain requests every 10 min (0 = off)",
    )
    args = ap.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        # 1) Sign-in
        print(f"[{now()}] 1) Opening NTULearn...")
        page.goto(f"{BASE}/ultra/course")
        start = time.time()
        told_user = False
        while not bb_signed_in(context):
            waited = time.time() - start
            # Give silent single sign-on ~15 s to finish its redirects before asking.
            if waited > 15 and not told_user:
                print("     Not signed in. Sign in in the browser window.")
                print("     Tick 'Yes' on 'Stay signed in?' (this browser is separate from your Chrome).")
                told_user = True
            if waited > 300:
                print("     Gave up after 5 minutes.")
                context.close()
                return
            time.sleep(3)
        print(f"     Signed in after {time.time() - start:.0f} s. "
              f"Manual sign-in needed: {'YES' if told_user else 'NO (silent)'}")

        # 2) NTULearn cookies -> plain requests
        print(f"[{now()}] 2) NTULearn cookies (names + expiry only):")
        describe_cookies(context, BASE)
        bb = to_requests_session(context, BASE)
        r = bb.get(f"{BASE}/learn/api/v1/users/me", timeout=30)
        print(f"     Plain requests -> users/me: {r.status_code} "
              f"({'works' if r.status_code == 200 else 'does NOT work'})")

        # 3) Video site: launch the Media Gallery, then plain requests
        print(f"[{now()}] 3) Opening the Media Gallery (video site)...")
        page.goto(LAUNCH_URL)
        try:
            page.wait_for_url(f"{VIDEO}/**", timeout=60_000)
            page.wait_for_load_state("load")
        except Exception:
            host = page.url.split("/")[2] if "://" in page.url else page.url
            print(f"     Did not reach the video site (stuck on {host}).")
        print("     Video-site cookies (names + expiry only):")
        describe_cookies(context, VIDEO)
        vid = to_requests_session(context, VIDEO)
        status, links = check_video(vid)
        print(f"     Plain requests -> transcript list: {status}, download links found: {links} "
              f"({'works' if status == 200 and links else 'does NOT work'})")

        # 4) Optional: how long do the copied sessions last?
        if args.watch:
            # Leave the NTULearn page so the browser itself doesn't keep the session alive.
            page.goto("about:blank")
            print(f"[{now()}] 4) Re-checking every 10 min for {args.watch} min. "
                  "Leave this running; don't let the PC sleep.")
            end = time.time() + args.watch * 60
            while time.time() < end:
                time.sleep(600)
                r = bb.get(f"{BASE}/learn/api/v1/users/me", timeout=30)
                status, links = check_video(vid)
                print(f"     [{now()}] users/me: {r.status_code} | transcript list: {status} ({links} links)")

        context.close()
        print(f"[{now()}] Done. Browser closed.")


if __name__ == "__main__":
    main()