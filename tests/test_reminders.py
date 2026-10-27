"""
Offline tests for Phase 3's reminders: the schema upgrade (store/db.py), which
reminders are due and the message (store/reminders.py), and the `remind` and
`done` commands (cli.py). Made-up deadlines only; Telegram is faked.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import T0, deadline_event
from uni_copilot import cli
from uni_copilot.notify.telegram import Sent, TelegramError, TelegramSettings
from uni_copilot.store import reminders as rem
from uni_copilot.store.db import (
    EVENT_COLUMNS,
    MIGRATIONS,
    event_to_row,
    marked_done_times,
    migrate,
    open_db,
    schema_version,
    set_marked_done,
    set_state,
    table_names,
    time_to_db,
)
from uni_copilot.store.events import save_events
from uni_copilot.store.queries import SOURCES


# A made-up deadline due `hours` after `base` (T0 by default).
def due_in(n: int, hours: float, base: datetime = T0, **kw):
    return replace(deadline_event(n, 0, **kw), due_at=base + timedelta(hours=hours))


# Saves events as one sync at `synced`, and marks every source as synced then
# with no error, like a good sync does.
def sync(conn, events, synced: datetime) -> None:
    save_events(conn, events, synced)
    for s in SOURCES:
        set_state(conn, f"last_sync:{s}", time_to_db(synced))
        set_state(conn, f"last_error:{s}", "")
    conn.commit()


# What a plan reminds about, as (title, tier) pairs, soonest first.
def reminded(plan: rem.ReminderPlan) -> list[tuple[str, str]]:
    return [(r.event.title, r.reminder) for r in plan.reminders]


# ---------- schema version 2 ----------

# A Phase 2 store (version 1) upgrades to version 2 and keeps its events.
def test_migration_keeps_data():
    import sqlite3

    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    placeholders = ", ".join(f":{c}" for c in EVENT_COLUMNS)
    old.execute(f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({placeholders})",
                event_to_row(replace(deadline_event(1, 3), first_seen_at=T0, last_seen_at=T0, modified_at=T0)))
    old.commit()
    assert migrate(old) == 2 and schema_version(old) == 2
    assert {"marked_done", "notifications_sent"} <= set(table_names(old))
    assert old.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


# ---------- which reminders are due ----------

# Each open deadline gets the smallest tier it fits; handed-in, marked-done,
# removed, past and far-off deadlines get none.
def test_tiers_and_filters(db):
    sync(db, [
        due_in(1, 1.5, title="Two hours"),
        due_in(2, 10, title="Day"),
        due_in(3, 30, title="Too far"),
        due_in(4, 5, status="submitted", title="Handed in"),
        due_in(5, 5, title="Gradescope"),
        due_in(6, 5, title="Removed"),
        due_in(7, -1, title="Past"),
    ], T0 - timedelta(hours=1))
    set_marked_done(db, "ntulearn_gradebook:_5_1", T0)
    db.execute("UPDATE events SET removed_at = ? WHERE event_id = 'ntulearn_gradebook:_6_1'", (time_to_db(T0),))
    db.commit()
    assert reminded(rem.plan_reminders(db, T0)) == [("Two hours", "2h"), ("Day", "24h")]


# Never twice for the same deadline, tier and due time; then the 2h one comes.
def test_send_once_then_2h(db):
    sync(db, [due_in(1, 10)], T0)
    plan = rem.plan_reminders(db, T0)
    assert reminded(plan) == [("Made-up Assignment 1", "24h")]
    rem.record_sent(db, plan, T0)
    assert rem.plan_reminders(db, T0 + timedelta(minutes=30)).is_empty
    later = T0 + timedelta(hours=8, minutes=30)
    assert reminded(rem.plan_reminders(db, later)) == [("Made-up Assignment 1", "2h")]


# A deadline that moves is reminded again, for its new due time.
def test_moved_deadline_reminded_again(db):
    sync(db, [due_in(1, 10)], T0)
    rem.record_sent(db, rem.plan_reminders(db, T0), T0)
    sync(db, [due_in(1, 30)], T0 + timedelta(hours=1))
    assert reminded(rem.plan_reminders(db, T0 + timedelta(hours=7))) == [("Made-up Assignment 1", "24h")]


# The PC was off during the 24 h window: only the 2h reminder goes.
def test_pc_off_only_2h(db):
    sync(db, [due_in(1, 1)], T0 - timedelta(hours=1))
    assert reminded(rem.plan_reminders(db, T0)) == [("Made-up Assignment 1", "2h")]


# ---------- the sync alert ----------

# Sets one source's last success and error, as sync would.
def source_state(conn, source: str, last_ok: datetime | None, error: str) -> None:
    if last_ok is not None:
        set_state(conn, f"last_sync:{source}", time_to_db(last_ok))
    set_state(conn, f"last_error:{source}", error)
    conn.commit()


# No alert for the night gap or a short failure; one per failure streak; a new
# streak after a success alerts again; a source that never worked alerts too.
def test_sync_alert_streaks(db):
    last_ok = T0 - timedelta(hours=20)
    sync(db, [], last_ok)
    assert rem.sync_alert(db, T0) is None  # 20 h old, but nothing failed: the night gap

    for s in SOURCES:
        source_state(db, s, None, "AuthError: Browser failed")
    alert = rem.sync_alert(db, T0)
    assert alert.sources == SOURCES and alert.failing_since == last_ok
    rem.record_sent(db, rem.ReminderPlan([], alert, last_ok), T0)
    assert rem.sync_alert(db, T0 + timedelta(minutes=30)) is None  # same streak

    source_state(db, "ntulearn_gradebook", T0 + timedelta(hours=1), "")  # deadlines recover
    assert rem.sync_alert(db, T0 + timedelta(hours=2)) is None
    source_state(db, "ntulearn_gradebook", None, "AuthError: Browser failed")
    assert rem.sync_alert(db, T0 + timedelta(hours=4)) is None  # last success only 3 h ago
    assert rem.sync_alert(db, T0 + timedelta(hours=8)).sources == ("ntulearn_gradebook",)  # a new streak


# A source that never synced and fails alerts with no "since".
def test_sync_alert_never_worked(db):
    source_state(db, "ntulearn_message", None, "AuthError: x")
    alert = rem.sync_alert(db, T0)
    assert alert.sources == ("ntulearn_message",) and alert.failing_since is None


# ---------- the message ----------

# Your layout: summary line, tier headings, one box per deadline with the title
# escaped, the "done" hint with the first course code, and the footer.
def test_format_message(db):
    sync(db, [due_in(1, 1.5, title="Quiz <draft> & notes", course="XX1001"), due_in(2, 10, course="YY2002")],
         T0 - timedelta(hours=1))
    text = rem.format_message(rem.plan_reminders(db, T0), T0)
    lines = text.splitlines()
    assert lines[0] == "<b>2 deadlines not submitted on NTULearn</b>"
    assert "🔴 <b>Within 2 hours</b>" in lines and "🟡 <b>Within 24 hours</b>" in lines
    assert "<b>XX1001</b> · Quiz &lt;draft&gt; &amp; notes" in text and "<draft>" not in text
    assert "1 h 30 min left · today 13:30</blockquote>" in text
    assert lines[-3] == "" and 'Reply "done XX1001"' in lines[-2] and lines[-1] == "Updated 11:00 SGT</i>"


# One deadline says "1 deadline"; data from an earlier day shows its date.
def test_format_singular_and_old_data(db):
    sync(db, [due_in(1, 10)], T0 - timedelta(days=1))
    text = rem.format_message(rem.plan_reminders(db, T0), T0)
    assert text.startswith("<b>1 deadline not submitted") and "Updated Sat 24 Oct 12:00 SGT" in text


# A sync alert alone: the warning, a box with the error, the footer; empty plan -> "".
def test_format_alert_only(db):
    alert = rem.SyncAlert(("ntulearn_gradebook",), T0 - timedelta(hours=8), "Error <x>", {})
    text = rem.format_message(rem.ReminderPlan([], alert, T0 - timedelta(hours=8)), T0)
    assert text.startswith("⚠️ <b>Sync failing</b> since Sun 25 Oct 04:00 SGT")
    assert "<blockquote>deadlines: Error &lt;x&gt;" in text and text.endswith("Updated 04:00 SGT</i>")
    assert rem.format_message(rem.ReminderPlan([], None, T0), T0) == ""


# "… left" and "today / tomorrow / date", in Singapore time.
def test_time_texts():
    assert rem.time_left(timedelta(minutes=45)) == "45 min left"
    assert rem.time_left(timedelta(hours=1, minutes=50)) == "1 h 50 min left"
    assert rem.time_left(timedelta(hours=2)) == "2 h left"
    assert rem.time_left(timedelta(hours=9, minutes=59)) == "10 h left"
    assert rem.day_and_time(T0 + timedelta(hours=3), T0) == "today 15:00"
    assert rem.day_and_time(T0 + timedelta(hours=13), T0) == "tomorrow 01:00"
    assert rem.day_and_time(T0 + timedelta(days=3), T0) == "Wed 28 Oct 12:00"


# ---------- `uni-copilot remind` and `done` ----------

SETTINGS = TelegramSettings(token="123456:FAKE", chat_id="42")


# A store file with 3 open deadlines due 1 h, 10 h and 30 h from the real
# "now" (the commands use the real clock), synced 10 minutes ago.
@pytest.fixture
def remind_store(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "store.sqlite"
    conn = open_db(path)
    sync(conn, [due_in(1, 1, now, course="XX1001"), due_in(2, 10, now, course="XX1002"),
                due_in(3, 30, now, course="XX1003")], now - timedelta(minutes=10))
    conn.close()
    return path


# How many reminders the store has recorded as sent.
def sent_count(path) -> int:
    conn = open_db(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM notifications_sent").fetchone()[0]
    finally:
        conn.close()


# Replaces the CLI's TelegramBot with a fake that records messages, or raises.
@pytest.fixture
def fake_telegram(monkeypatch):
    messages: list[tuple[str, bool]] = []
    failure: list[Exception] = []

    class FakeBot:
        def __init__(self, settings):
            pass

        # Like TelegramBot.send_message: records, or raises the set failure.
        def send_message(self, text: str, html: bool = False) -> Sent:
            if failure:
                raise failure[0]
            messages.append((text, html))
            return Sent(parts=1, plain_fallbacks=0)

    monkeypatch.setattr(cli, "TelegramBot", FakeBot)
    monkeypatch.setattr(cli, "load_telegram_settings", lambda: SETTINGS)
    return messages, failure


# --dry-run shows what would go, in plain text, and sends and records nothing.
def test_remind_dry_run(remind_store, fake_telegram, capsys):
    assert cli.main(["--db", str(remind_store), "remind", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would send: XX1001 (2h), XX1002 (24h)" in out and "<b>" not in out
    assert fake_telegram[0] == [] and sent_count(remind_store) == 0


# A real run sends one HTML message, records it, and the next run sends nothing.
def test_remind_sends_once(remind_store, fake_telegram, capsys):
    assert cli.main(["--db", str(remind_store), "remind"]) == 0
    assert len(fake_telegram[0]) == 1 and fake_telegram[0][0][1] is True
    assert sent_count(remind_store) == 2 and "Sent: XX1001 (2h), XX1002 (24h)" in capsys.readouterr().out
    assert cli.main(["--db", str(remind_store), "remind"]) == 0
    assert "Nothing to send." in capsys.readouterr().out and len(fake_telegram[0]) == 1


# A failed send records nothing (so the next run tries again) and exits 1.
def test_remind_failure_records_nothing(remind_store, fake_telegram, capsys):
    fake_telegram[1].append(TelegramError("sendMessage", 502, "Bad Gateway"))
    assert cli.main(["--db", str(remind_store), "remind"]) == 1
    assert "Not sent" in capsys.readouterr().out and sent_count(remind_store) == 0


# `done` by words or id: several matches mark nothing; one is marked, then
# "already"; --undo takes it back; reminders skip a marked deadline.
def test_done(remind_store, capsys):
    db = str(remind_store)
    assert cli.main(["--db", db, "done", "made-up"]) == 1
    assert "3 upcoming deadlines match" in capsys.readouterr().out
    assert cli.main(["--db", db, "done", "xx1002 assignment"]) == 0
    assert "Marked done: XX1002" in capsys.readouterr().out
    assert cli.main(["--db", db, "done", "_2_1"]) == 0
    assert "Already marked done" in capsys.readouterr().out

    conn = open_db(remind_store)
    assert list(marked_done_times(conn)) == ["ntulearn_gradebook:_2_1"]
    plan = rem.plan_reminders(conn, datetime.now(timezone.utc))
    assert [r.event.course_code for r in plan.reminders] == ["XX1001"]
    conn.close()

    assert cli.main(["--db", db, "done", "--undo", "xx1002"]) == 0
    assert "Undone" in capsys.readouterr().out
    assert cli.main(["--db", db, "done", "nothing like this"]) == 1
