"""Tests for the two dev-agent-sagas enhancements (specs/dev-agent-sagas §7).

Enhancement 1 — proactive rollback announcement (DA_SAGA_ANNOUNCE):
  - _run_compensations populates self._rollback_summary with truthful counts
  - _rollback_notice formats reverted / manual / incomplete (singular + plural)
  - _rollback_notice is silent when the flag is off or no rollback ran
  - _speak_plan_completion appends the notice on the (previously silent) cancel path

Enhancement 2 — git-blob saga snapshot backend (DA_SAGA_GIT_BACKEND):
  - snapshot captures a git blob (no file-copy backup) inside a git work tree
  - round-trip restore recovers the original bytes from the blob
  - handles files larger than the file-copy 256 KB cap (the gap it closes)
  - degrades to the file-copy backend when off / outside a git repo
"""

from __future__ import annotations
from inference.saga_manager import SagaManager

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import AgentResult, DevAgent

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")


# ---------------------------------------------------------------------------
# Helpers (mirror test_saga_compensation.py)
# ---------------------------------------------------------------------------

async def _open_db(tmp_path):
    from storage.db import AgentDB
    db = AgentDB()
    await db.open(tmp_path / "saga.db")
    return db


async def _make_agent(db=None) -> DevAgent:
    return DevAgent(router=MagicMock(), agent_db=db)


async def _seed_run(db) -> int:
    await db.sessions.insert_session(mode="test")
    return await db.runs.start_agent_run("saga test", "plan", "model")


def _git_repo(path: Path) -> Path:
    subprocess.run([_GIT, "init"], cwd=path, capture_output=True)
    subprocess.run([_GIT, "config", "user.email", "t@t.com"], cwd=path, capture_output=True)
    subprocess.run([_GIT, "config", "user.name", "tester"], cwd=path, capture_output=True)
    return path


# ===========================================================================
# Enhancement 1 — rollback announcement
# ===========================================================================

async def test_rollback_summary_counts_reverted(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    target = tmp_path / "doc.py"
    target.write_text("ORIGINAL", encoding="utf-8")
    snap = json.dumps(DevAgent._snapshot_for_write(str(target)))
    target.write_text("OVERWRITTEN", encoding="utf-8")
    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", str(target), None, "ok", True, 1.0,
        compensation_action="RESTORE_FILE", compensation_args=snap,
    )
    await db.sagas.insert_saga_compensation(run_id, step_id, "RESTORE_FILE", snap)

    await agent._run_compensations(run_id, triggered_by="user_cancel")

    assert agent._rollback_summary == {
        "reverted": 1, "manual": 0, "incomplete": 0, "triggered_by": "user_cancel",
    }
    await db.close()


async def test_rollback_summary_counts_manual_and_incomplete(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    # A REVERT_TERMINAL (manual) and an existed-but-no-backup RESTORE (incomplete).
    sid1 = await db.runs.insert_agent_step(
        run_id, 1, "RUN_TERMINAL", "rm x", None, "ok", True, 1.0,
        compensation_action="REVERT_TERMINAL", compensation_args="rm x",
    )
    await db.sagas.insert_saga_compensation(run_id, sid1, "REVERT_TERMINAL", "rm x")

    target = tmp_path / "huge.py"
    target.write_text("OVERWRITTEN", encoding="utf-8")
    snap = json.dumps({"path": str(target), "existed": True, "backup": None})
    sid2 = await db.runs.insert_agent_step(
        run_id, 2, "WRITE_FILE", str(target), None, "ok", True, 1.0,
        compensation_action="RESTORE_FILE", compensation_args=snap,
    )
    await db.sagas.insert_saga_compensation(run_id, sid2, "RESTORE_FILE", snap)

    await agent._run_compensations(run_id, triggered_by="max_replans")

    rb = agent._rollback_summary
    assert rb["manual"] == 1
    assert rb["incomplete"] == 1
    assert rb["reverted"] == 0
    await db.close()


async def test_rollback_summary_stays_none_with_no_compensations(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)
    await agent._run_compensations(run_id, triggered_by="user_cancel")
    assert agent._rollback_summary is None
    await db.close()


async def test_rollback_notice_formats_singular_and_plural():
    agent = await _make_agent()
    agent._saga_announce = True

    agent._rollback_summary = {"reverted": 1, "manual": 0, "incomplete": 0}
    assert agent._rollback_notice() == " Reverted 1 file change."

    agent._rollback_summary = {"reverted": 3, "manual": 0, "incomplete": 0}
    assert agent._rollback_notice() == " Reverted 3 file changes."

    agent._rollback_summary = {"reverted": 2, "manual": 1, "incomplete": 1}
    notice = agent._rollback_notice()
    assert "Reverted 2 file changes." in notice
    assert "1 terminal action need" in notice
    assert "1 change could not be rolled back." in notice


async def test_rollback_notice_silent_when_disabled():
    agent = await _make_agent()
    agent._rollback_summary = {"reverted": 5, "manual": 0, "incomplete": 0}
    agent._saga_announce = False
    assert agent._rollback_notice() == ""


async def test_rollback_notice_silent_when_no_rollback():
    agent = await _make_agent()
    agent._saga_announce = True
    agent._rollback_summary = None
    assert agent._rollback_notice() == ""


async def test_speak_completion_announces_rollback_on_cancel():
    agent = await _make_agent()
    agent._saga_announce = True
    agent._current_step, agent._total_steps = 2, 4
    agent._rollback_summary = {"reverted": 1, "manual": 0, "incomplete": 0}
    result = AgentResult(goal="g", domain="plan", model_used="m", steps=[], success=False)

    tts = MagicMock()
    tts.speak = AsyncMock()
    with patch("tts.polly_stream.get_client", return_value=tts):
        await agent._speak_plan_completion(result, cancelled=True)

    msg = tts.speak.call_args[0][0]   # recorded synchronously at call time
    assert "cancelled at step 2 of 4" in msg
    assert "Reverted 1 file change." in msg


async def test_speak_completion_cancel_silent_when_no_rollback():
    agent = await _make_agent()
    agent._saga_announce = True
    agent._current_step, agent._total_steps = 1, 2
    agent._rollback_summary = None
    result = AgentResult(goal="g", domain="plan", model_used="m", steps=[], success=False)

    tts = MagicMock()
    tts.speak = AsyncMock()
    with patch("tts.polly_stream.get_client", return_value=tts):
        await agent._speak_plan_completion(result, cancelled=True)

    assert tts.speak.call_args[0][0] == "Task cancelled at step 1 of 2."


# ===========================================================================
# Enhancement 2 — git-blob snapshot backend
# ===========================================================================

def test_git_backend_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DA_SAGA_GIT_BACKEND", raising=False)
    assert SagaManager._saga_git_backend_enabled() is False
    monkeypatch.setenv("DA_SAGA_GIT_BACKEND", "1")
    assert SagaManager._saga_git_backend_enabled() is True


@requires_git
def test_snapshot_uses_git_blob_in_repo(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    target = repo / "exists.py"
    target.write_text("ORIGINAL", encoding="utf-8")

    monkeypatch.setenv("DA_SAGA_GIT_BACKEND", "1")
    info = DevAgent._snapshot_for_write(str(target))

    assert info["existed"] is True
    assert info["backup"] is None            # no file-copy backup written
    assert info.get("git_blob")              # captured as a git object instead
    assert info.get("git_repo")
    # The blob is real and inspectable.
    out = subprocess.run([_GIT, "-C", info["git_repo"], "cat-file", "blob", info["git_blob"]],
                         capture_output=True)
    assert out.stdout == b"ORIGINAL"


@requires_git
async def test_git_backend_round_trip_restore(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    target = repo / "doc.py"
    target.write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.setenv("DA_SAGA_GIT_BACKEND", "1")
    snap = json.dumps(DevAgent._snapshot_for_write(str(target)))
    target.write_text("OVERWRITTEN BY PLAN", encoding="utf-8")

    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", str(target), None, "ok", True, 1.0,
        compensation_action="RESTORE_FILE", compensation_args=snap,
    )
    await db.sagas.insert_saga_compensation(run_id, step_id, "RESTORE_FILE", snap)

    incomplete = await agent._run_compensations(run_id, triggered_by="user_cancel")

    assert incomplete == 0
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    await db.close()


@requires_git
def test_git_backend_handles_large_file(tmp_path, monkeypatch):
    """The file-copy backend skips backup above 256 KB; git has no such cap."""
    repo = _git_repo(tmp_path)
    target = repo / "big.py"
    big = "x" * (DevAgent._SAGA_SNAPSHOT_MAX_BYTES + 1024)
    target.write_text(big, encoding="utf-8")

    monkeypatch.setenv("DA_SAGA_GIT_BACKEND", "1")
    info = DevAgent._snapshot_for_write(str(target))
    assert info.get("git_blob")              # captured despite exceeding the cap

    # And it restores.
    target.write_text("SMALL", encoding="utf-8")
    assert DevAgent._restore_file(json.dumps(info)) is True
    assert target.read_text(encoding="utf-8") == big


@requires_git
def test_git_backend_falls_back_outside_repo(tmp_path, monkeypatch):
    # tmp_path is NOT a git repo → snapshot must use the file-copy backend.
    target = tmp_path / "plain.py"
    target.write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.setenv("DA_SAGA_GIT_BACKEND", "1")

    info = DevAgent._snapshot_for_write(str(target))
    assert info.get("git_blob") is None
    assert info["backup"] and Path(info["backup"]).exists()


def test_git_backend_off_is_byte_identical_filecopy(tmp_path, monkeypatch):
    repo_like = tmp_path
    target = repo_like / "f.py"
    target.write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.delenv("DA_SAGA_GIT_BACKEND", raising=False)

    info = DevAgent._snapshot_for_write(str(target))
    assert "git_blob" not in info
    assert info["existed"] is True
    assert info["backup"] and Path(info["backup"]).read_text(encoding="utf-8") == "ORIGINAL"
