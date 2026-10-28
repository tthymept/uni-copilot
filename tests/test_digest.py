"""
Offline tests for the step 13 fixes: the lock between programs (filelock.py),
`uni-copilot sync` skipping while another sync runs, and `uni-copilot digest`
deciding for itself (cli.py). The clock, Claude and sync are all faked.
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from uni_copilot import cli
from uni_copilot.filelock import file_lock
from uni_copilot.store.db import get_state, open_db, set_state, time_to_db
from uni_copilot.store.queries import SOURCES

# Wed 28 Oct 2026 08:00 SGT (00:00 UTC): after 07:30.
MORNING = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)


# ---------- the lock between programs ----------

# A second holder can't get the lock while the first has it, and can once it's free.
def test_file_lock(tmp_path):
    path = tmp_path / "x.lock"
    with file_lock(path) as first:
        assert first is True
        with file_lock(path, wait_s=0.6) as second:
            assert second is False
    with file_lock(path) as again:
        assert again is True


# `uni-copilot sync` skips (exit 0, nothing fetched) while another sync holds the lock.
def test_sync_skips_while_another_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SYNC_LOCK", tmp_path / "sync.lock")
    monkeypatch.setattr(cli, "_sync", lambda db: pytest.fail("must not sync"))
    with file_lock(cli.SYNC_LOCK):
        assert cli.main(["--db", str(tmp_path / "s.sqlite"), "sync"]) == 0
    assert "Another sync is running" in capsys.readouterr().out


# ---------- `uni-copilot digest` deciding for itself ----------

# A store file whose last sync was `synced_hours_ago` before MORNING, a fixed
# clock at `now`, and fakes for Claude and sync that record their calls.
@pytest.fixture
def digest_env(tmp_path, monkeypatch):
    calls: list[str] = []
    claude_answers: list[tuple[int, str]] = []
    path = tmp_path / "store.sqlite"

    def setup(now: datetime = MORNING, synced_hours_ago: float = 1) -> None:
        conn = open_db(path)
        for s in SOURCES:
            set_state(conn, f"last_sync:{s}", time_to_db(MORNING - timedelta(hours=synced_hours_ago)))
            set_state(conn, f"last_error:{s}", "")
        conn.commit()
        conn.close()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        monkeypatch.setattr(cli, "datetime", Clock)

    def fake_claude(claude=None):
        calls.append("claude")
        return claude_answers.pop(0) if claude_answers else (0, "Digest sent: 1 new post.")

    def fake_sync(db):
        calls.append("sync")
        conn = open_db(db)
        set_state(conn, "last_sync:ntulearn_gradebook", time_to_db(MORNING))
        conn.commit()
        conn.close()
        return 0

    monkeypatch.setattr(cli, "SYNC_LOCK", tmp_path / "sync.lock")
    monkeypatch.setattr(cli, "run_claude_digest", fake_claude)
    monkeypatch.setattr(cli, "_sync", fake_sync)
    return path, setup, calls, claude_answers


# Runs `uni-copilot digest` against the test store; returns (exit code, output).
def digest(path, capsys, *extra: str) -> tuple[int, str]:
    code = cli.main(["--db", str(path), "digest", *extra])
    return code, capsys.readouterr().out


# Before 07:30 nothing happens; --force runs it anyway.
def test_digest_not_before_0730(digest_env, capsys):
    path, setup, calls, _ = digest_env
    setup(now=MORNING - timedelta(hours=2))  # 06:00 SGT
    assert digest(path, capsys) == (0, "Not yet: the digest runs after 07:30.\n") and calls == []
    assert digest(path, capsys, "--force")[0] == 0 and calls == ["claude"]


# After 07:30 it runs once; the next pokes that day do nothing.
def test_digest_once_a_day(digest_env, capsys):
    path, setup, calls, _ = digest_env
    setup()
    code, out = digest(path, capsys)
    assert code == 0 and "Digest sent" in out and calls == ["claude"]
    assert digest(path, capsys) == (0, "Already done today.\n") and calls == ["claude"]
    conn = open_db(path)
    assert get_state(conn, "digest_done") == "2026-10-28"
    conn.close()


# "Nothing new" also counts as done for today (the skill sent nothing on purpose).
def test_digest_nothing_new_is_done(digest_env, capsys):
    path, setup, calls, answers = digest_env
    setup()
    answers.append((0, "Nothing new since Tue 27 Oct 15:26 SGT; no digest sent."))
    assert digest(path, capsys)[0] == 0
    assert digest(path, capsys)[1] == "Already done today.\n"


# A failure (Claude error, or the skill's "Not sent:") is tried again at the
# next poke, at most 3 times a day.
def test_digest_retries_then_gives_up(digest_env, capsys):
    path, setup, calls, answers = digest_env
    setup()
    answers += [(1, "error"), (0, "Not sent: Telegram 502."), (1, "error")]
    assert [digest(path, capsys)[0] for _ in range(3)] == [1, 1, 1]
    code, out = digest(path, capsys)
    assert code == 0 and "Gave up for today after 3 failed tries" in out and calls == ["claude"] * 3


# Store older than 3 hours (the PC was asleep): sync first, then Claude.
def test_digest_syncs_old_store_first(digest_env, capsys):
    path, setup, calls, _ = digest_env
    setup(synced_hours_ago=15)
    code, out = digest(path, capsys)
    assert code == 0 and calls == ["sync", "claude"] and "syncing first" in out


# A fresh store: no extra sync.
def test_digest_fresh_store_no_sync(digest_env, capsys):
    path, setup, calls, _ = digest_env
    setup(synced_hours_ago=1)
    assert digest(path, capsys)[0] == 0 and calls == ["claude"]


# ---------- the claude -p command ----------

# Locked down like the Telegram session, no window, never the paid API.
def test_claude_command(monkeypatch):
    seen = {}

    def fake_run(command, **kw):
        seen.update(command=command, **kw)
        return subprocess.CompletedProcess(command, 0, stdout="Digest sent: 1 new post.\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    code, out = cli.run_claude_digest("C:/fake/claude.exe")
    command = seen["command"]
    assert code == 0 and out == "Digest sent: 1 new post."
    assert command[:2] == ["C:/fake/claude.exe", "-p"]
    assert command[command.index("--tools") + 1] == "Skill,ToolSearch" and "--no-chrome" in command
    assert command[command.index("--disallowedTools") + 1] == "mcp__ntulearn__login"
    assert set(cli.DIGEST_ALLOWED) <= set(command)
    assert "ANTHROPIC_API_KEY" not in seen["env"] and seen["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    if os.name == "nt":
        assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW


# UNI_COPILOT_CLAUDE wins; otherwise the exe next to the npm launcher.
def test_find_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("UNI_COPILOT_CLAUDE", "D:/x/claude.exe")
    assert cli.find_claude() == "D:/x/claude.exe"
    monkeypatch.delenv("UNI_COPILOT_CLAUDE")
    exe = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(tmp_path / "claude.cmd"))
    assert cli.find_claude() == str(exe)
