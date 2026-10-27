"""
Which reminders are due right now, and the one Telegram message that says so.

In plain words:
  `uni-copilot remind` (step 5) runs every 30 minutes. Each run asks this file
  "anything to send now?". Most runs: nothing. Otherwise:

  Reminders. Each open deadline gets at most two:
    24h  the first run after it comes within 24 hours of being due
    2h   the first run after it comes within 2 hours
  "Open" = NTULearn says not handed in, you didn't mark it done, it wasn't
  removed. Never twice for the same deadline, tier and due time (the
  notifications_sent table, step 3), so a deadline that MOVES is reminded
  again at its new time. If the PC was off and a deadline is already within
  2 hours, only the 2h reminder goes (the 24h one would say the same thing).
  No quiet hours (your decision): day or night, the first run after it's due.

  Sync alert. When a source's last sync attempt FAILED and its last success is
  more than 6 hours old, one alert, once per failure streak. The streak is
  remembered in `state` as the last-success time it was sent for; when a sync
  works again, that time changes and a later streak can alert again. The
  planned night gap (no syncs 23:00-07:00) never alerts: nothing failed.

Three parts, and nothing here sends anything:
  plan_reminders  what to send now (reads the store)
  format_message  the Telegram message (HTML, your layout from 27 Oct: a
                  summary line, then per tier a coloured dot and one
                  <blockquote> box per deadline; every title escaped first)
  record_sent     writes down what was sent; the caller runs it only after
                  Telegram accepted the message, so a failed send is retried
                  next run instead of being lost
So it all runs on a made-up clock in tests: `now` is always passed in.
"""

import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from uni_copilot.ntulearn_mcp.config import SGT
from uni_copilot.ntulearn_mcp.models import sgt_text
from uni_copilot.store.db import get_state, record_reminder, reminder_sent, row_to_event, set_state, time_to_db
from uni_copilot.store.models import Event, Reminder
from uni_copilot.store.queries import SOURCES, freshness

# The two windows, smallest first: a deadline gets the first tier it fits in.
TIERS: tuple[tuple[Reminder, timedelta], ...] = (("2h", timedelta(hours=2)), ("24h", timedelta(hours=24)))
HEADINGS: dict[Reminder, str] = {"2h": "🔴 <b>Within 2 hours</b>", "24h": "🟡 <b>Within 24 hours</b>"}

SYNC_ALERT_AFTER = timedelta(hours=6)
ERROR_MAX_CHARS = 200

# Friendly names for the sources, in the order they're shown (same as the CLI).
SOURCE_NAMES = {
    "ntulearn_gradebook": "deadlines",
    "ntulearn_announcement": "announcements",
    "ntulearn_message": "messages",
}


# One reminder to send: which deadline, and which tier.
@dataclass(frozen=True)
class DueReminder:
    event: Event
    reminder: Reminder


# "Sync is failing": which sources, since when (their oldest last success,
# None if they never worked), the first error text, and the notes to save in
# `state` once it's sent (source -> the last-success time this alert is for).
@dataclass(frozen=True)
class SyncAlert:
    sources: tuple[str, ...]
    failing_since: datetime | None
    error: str
    marks: dict[str, str]


# Everything one `remind` run should send. `deadlines_as_of` = when the
# deadlines last synced, shown at the bottom of the message.
@dataclass(frozen=True)
class ReminderPlan:
    reminders: list[DueReminder]
    sync_alert: SyncAlert | None
    deadlines_as_of: datetime | None

    # True if there's nothing to send this run (the usual case).
    @property
    def is_empty(self) -> bool:
        return not self.reminders and self.sync_alert is None


# What to send now. Reads the store only; changes nothing.
def plan_reminders(conn: sqlite3.Connection, now: datetime) -> ReminderPlan:
    return ReminderPlan(
        reminders=due_reminders(conn, now),
        sync_alert=sync_alert(conn, now),
        deadlines_as_of=freshness(conn).last_sync.get("ntulearn_gradebook"),
    )


# The reminders due now, soonest deadline first. Looks at open deadlines due
# in the next 24 hours that you didn't mark done, picks each one's tier, and
# skips it if that tier was already sent for its current due time.
def due_reminders(conn: sqlite3.Connection, now: datetime) -> list[DueReminder]:
    largest = TIERS[-1][1]
    rows = conn.execute(
        """
        SELECT * FROM events
        WHERE kind = 'deadline' AND status = 'open' AND removed_at IS NULL
          AND due_at > ? AND due_at <= ?
          AND event_id NOT IN (SELECT event_id FROM marked_done)
        ORDER BY due_at
        """,
        (time_to_db(now), time_to_db(now + largest)),
    )
    due = []
    for row in rows:
        event = row_to_event(row)
        tier = next(name for name, window in TIERS if event.due_at - now <= window)
        if not reminder_sent(conn, event.event_id, tier, event.due_at):
            due.append(DueReminder(event, tier))
    return due


# The sync alert to send now, or None. A source counts as failing when its
# last attempt left an error and its last success is over 6 hours old (or it
# never worked). Sources already alerted for this streak are left out.
def sync_alert(conn: sqlite3.Connection, now: datetime) -> SyncAlert | None:
    fresh = freshness(conn)
    failing: list[str] = []
    marks: dict[str, str] = {}
    for source in SOURCES:
        error = fresh.errors.get(source)
        last_ok = fresh.last_sync.get(source)
        if not error or (last_ok is not None and now - last_ok <= SYNC_ALERT_AFTER):
            continue
        streak = time_to_db(last_ok) if last_ok else "never"
        if get_state(conn, f"sync_alert:{source}") == streak:
            continue  # already alerted for this streak
        failing.append(source)
        marks[source] = streak
    if not failing:
        return None
    times = [fresh.last_sync[s] for s in failing]
    since = None if None in times else min(times)
    return SyncAlert(tuple(failing), since, fresh.errors[failing[0]], marks)


# The Telegram message for a plan, in Telegram's HTML mode, or "" if the plan
# is empty. Layout (your design, 27 Oct):
#   <b>2 deadlines not submitted on NTULearn</b>
#
#   🔴 <b>Within 2 hours</b>
#   <blockquote><b>XX1001</b> · <a href="...">Quiz 1</a>
#   1 h 27 min left · today 15:59</blockquote>
#   🟡 <b>Within 24 hours</b>
#   ...
#
#   <i>Handed in somewhere else? Reply "done XX1001" to stop reminders for it.
#   Updated 14:15 SGT</i>
# The sync alert, if any, comes first in the same style. Titles and error
# texts are escaped. Some tags span two lines; send_message(html=True) never
# splits inside an open tag.
def format_message(plan: ReminderPlan, now: datetime) -> str:
    lines: list[str] = []
    if plan.sync_alert:
        lines += _alert_lines(plan.sync_alert)
    if plan.reminders:
        if lines:
            lines.append("")
        count = len(plan.reminders)
        lines += [f"<b>{count} deadline{'s' if count > 1 else ''} not submitted on NTULearn</b>", ""]
        for tier, _ in TIERS:
            items = [r for r in plan.reminders if r.reminder == tier]
            if items:
                lines.append(HEADINGS[tier])
                lines += [_deadline_block(r.event, now) for r in items]
        code = html.escape(plan.reminders[0].event.course_code or "", quote=False)
        lines.append("")
        lines.append(f'<i>Handed in somewhere else? Reply "done {code}" to stop reminders for it.')
        lines.append(f"{_updated(plan.deadlines_as_of, now)}</i>")
    elif lines:
        lines += ["", f"<i>{_updated(plan.deadlines_as_of, now)}</i>"]
    return "\n".join(lines)


# Writes down what was sent: one notifications_sent row per reminder, and the
# sync alert's streak notes in `state`. One transaction. Call only after
# Telegram accepted the message.
def record_sent(conn: sqlite3.Connection, plan: ReminderPlan, sent_at: datetime) -> None:
    with conn:
        for r in plan.reminders:
            record_reminder(conn, r.event.event_id, r.reminder, r.event.due_at, sent_at)
        if plan.sync_alert:
            for source, streak in plan.sync_alert.marks.items():
                set_state(conn, f"sync_alert:{source}", streak)


# One deadline as a two-line box:
#   <blockquote><b>IE2108</b> · <a href="...">Lab 4</a>
#   1 h 50 min left · today 23:59</blockquote>
# The title is a link to the course when there is one.
def _deadline_block(event: Event, now: datetime) -> str:
    title = html.escape(event.title, quote=False)
    if event.url:
        title = f'<a href="{html.escape(event.url, quote=True)}">{title}</a>'
    course = html.escape(event.course_code or "", quote=False)
    when = f"{time_left(event.due_at - now)} · {day_and_time(event.due_at, now)}"
    return f"<blockquote><b>{course}</b> · {title}\n{when}</blockquote>"


# The sync alert: what's failing since when, then a box with the error and what to do.
def _alert_lines(alert: SyncAlert) -> list[str]:
    names = ", ".join(SOURCE_NAMES.get(s, s) for s in alert.sources)
    since = f"since {sgt_text(alert.failing_since)}" if alert.failing_since else "(never worked)"
    error = alert.error if len(alert.error) <= ERROR_MAX_CHARS else alert.error[: ERROR_MAX_CHARS - 3] + "..."
    return [
        f"⚠️ <b>Sync failing</b> {since}",
        f"<blockquote>{names}: {html.escape(error, quote=False)}",
        "Run <code>uv run uni-copilot sync</code>, or ask Claude to run the ntulearn login tool.</blockquote>",
    ]


# "Updated 14:15 SGT" when the data is from today, else with the date
# ("Updated Mon 26 Oct 14:15 SGT"), so old data is easy to spot.
def _updated(as_of: datetime | None, now: datetime) -> str:
    if as_of is None:
        return "Never synced"
    if as_of.astimezone(SGT).date() == now.astimezone(SGT).date():
        return f"Updated {as_of.astimezone(SGT):%H:%M} SGT"
    return f"Updated {sgt_text(as_of)}"


# "today 15:59", "tomorrow 00:29", or "Thu 29 Oct 15:59", in Singapore time.
def day_and_time(due: datetime, now: datetime) -> str:
    due_sgt, today = due.astimezone(SGT), now.astimezone(SGT).date()
    days = (due_sgt.date() - today).days
    day = {0: "today", 1: "tomorrow"}.get(days, f"{due_sgt:%a %d %b}")
    return f"{day} {due_sgt:%H:%M}"


# "45 min left", "1 h 50 min left", "23 h left": how long until the deadline.
# Minutes only matter below 3 hours; above that it's rounded to the nearest
# hour (9 h 59 min -> "10 h left", not "9 h left").
def time_left(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min left"
    hours, rest = divmod(minutes, 60)
    if hours < 3:
        return f"{hours} h {rest} min left" if rest else f"{hours} h left"
    return f"{(minutes + 30) // 60} h left"


# Self-test: uv run python -m uni_copilot.store.reminders
# Made-up deadlines in memory on a made-up clock; nothing is sent.
def _main() -> None:
    from dataclasses import replace

    from uni_copilot.store.db import open_db, set_marked_done
    from uni_copilot.store.events import save_events

    conn = open_db(":memory:")
    t0 = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)  # Tue 12:00 SGT

    def deadline(n: int, hours_ahead: float, title: str, status: str = "open") -> Event:
        return Event(f"ntulearn_gradebook:_{n}_1", "ntulearn_gradebook", "deadline", f"XX100{n}", "_1_1",
                     title, url="https://example.invalid/course", due_at=t0 + timedelta(hours=hours_ahead),
                     status=status, content_hash=f"h{n}")

    events = [
        deadline(1, 1.5, "Made-up Quiz <draft> & notes"),      # within 2 h
        deadline(2, 11.98, "Made-up Lab 4"),                    # within 24 h
        deadline(3, 20, "Made-up Essay", status="submitted"),   # handed in: never
        deadline(4, 30, "Made-up Project"),                     # too far for now
        deadline(5, 5, "Made-up Gradescope HW"),                # you'll mark it done
    ]
    save_events(conn, events, t0 - timedelta(hours=1))
    for s in SOURCES:
        set_state(conn, f"last_sync:{s}", time_to_db(t0 - timedelta(hours=1)))
        set_state(conn, f"last_error:{s}", "")
    set_marked_done(conn, "ntulearn_gradebook:_5_1", t0)
    conn.commit()

    # One pretend `remind` run: plan, print, and record as if Telegram accepted it.
    def show(label: str, now: datetime) -> ReminderPlan:
        plan = plan_reminders(conn, now)
        items = ", ".join(f"{r.event.title} ({r.reminder})" for r in plan.reminders) or "none"
        alert = f"; sync alert for {', '.join(plan.sync_alert.sources)}" if plan.sync_alert else ""
        print(f"{label}: {items}{alert}")
        record_sent(conn, plan, now)
        return plan

    plan = show("1) Tue 12:00 SGT", t0)
    print("\n   The message (HTML, as Telegram receives it):")
    for line in format_message(plan, t0).splitlines():
        print(f"   | {line}")

    show("\n2) Same time again, after recording", t0)
    show("3) Tue 12:30 (Quiz and Lab 4 already reminded)", t0 + timedelta(minutes=30))
    show("4) Tue 22:10 (Lab 4 within 2 h; Project within 24 h)", t0 + timedelta(hours=10, minutes=10))

    moved = replace(events[1], due_at=t0 + timedelta(days=2))
    save_events(conn, [replace(e, due_at=moved.due_at) if e.event_id == moved.event_id else e for e in events], t0)
    show("5) Lab 4 moved to Thu 12:00; Wed 12:30", t0 + timedelta(days=1, minutes=30))

    print("\n6) Sync alert:")
    night = t0 + timedelta(hours=19)  # Wed 07:00 SGT, last success Tue 11:00, nothing failed
    show("   night gap, no failure", night)
    for s in SOURCES:
        set_state(conn, f"last_error:{s}", "AuthError: Browser failed (hidden, Error)")
    conn.commit()
    plan = show("   last attempt failed, last success 20 h ago", night)
    print(f"   alert text: {format_message(plan, night).splitlines()[0]}")
    show("   next run, same streak", night + timedelta(minutes=30))
    set_state(conn, "last_sync:ntulearn_gradebook", time_to_db(night + timedelta(hours=1)))
    set_state(conn, "last_error:ntulearn_gradebook", "")
    conn.commit()
    show("   deadlines synced again, others still failing", night + timedelta(hours=1, minutes=10))
    set_state(conn, "last_error:ntulearn_gradebook", "AuthError: Browser failed (hidden, Error)")
    conn.commit()
    show("   deadlines fail again, 7 h later (a new streak)", night + timedelta(hours=8))
    conn.close()


if __name__ == "__main__":
    _main()
