"""Sprint P — security-residuals regression tests.

Covers the 2026-06-14 audit findings:
  #3  _path_in_scope resolves symlinks/junctions (no scope escape)
  #4  content filter redacts Google OAuth / API-key / bearer tokens
  #11 double-quoted $()/backtick command substitution is caught
  #12 backup restore skips a destination outside the known roots
  #13 crash marker treats a live concurrent instance as not-a-crash

Run:
    python -m pytest tests/test_sprint_p_security.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.goal_session import _path_in_scope, _bash_is_allowlisted
from adaptive.content_filter import ContentFilter
from core import crash_marker
from scripts.backup_agent_state import (
    restore_backup, BackupConfig, _MANIFEST_NAME, _MANIFEST_VERSION,
)


# ---------------------------------------------------------------------------
# #3 — symlink/junction scope escape
# ---------------------------------------------------------------------------

def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory junction (Windows) / symlink (POSIX). False if denied."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                               capture_output=True, text=True)
            return r.returncode == 0
        os.symlink(target, link, target_is_directory=True)
        return True
    except Exception:
        return False


def test_path_in_scope_blocks_junction_escape(tmp_path):
    scope = tmp_path / "scope"; scope.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    link = scope / "escape"
    if not _make_dir_link(link, outside):
        pytest.skip("cannot create a directory junction/symlink here")
    sneaky = link / "evil.txt"   # lexically under scope, really under outside
    # abspath would call this in-scope; realpath resolves the junction → denied.
    assert _path_in_scope(str(sneaky), [str(scope)]) is False


def test_path_in_scope_allows_genuine_child(tmp_path):
    scope = tmp_path / "scope"; scope.mkdir()
    assert _path_in_scope(str(scope / "sub" / "ok.txt"), [str(scope)]) is True
    assert _path_in_scope(str(scope), [str(scope)]) is True
    assert _path_in_scope("", [str(scope)]) is False


# ---------------------------------------------------------------------------
# #11 — double-quoted command substitution
# ---------------------------------------------------------------------------

def test_double_quoted_substitution_is_caught():
    # bash expands $()/backticks inside double quotes → must NOT be allowlisted.
    assert _bash_is_allowlisted('echo "$(rm -rf x)"') is False
    assert _bash_is_allowlisted('echo "`whoami`"') is False


def test_single_quoted_substitution_is_literal():
    # single quotes suppress expansion → safe to allowlist.
    assert _bash_is_allowlisted("echo '$(rm -rf x)'") is True


def test_literal_redirect_in_quotes_still_allowed():
    # regression for the original behavior: '>' inside quotes is data.
    assert _bash_is_allowlisted('echo "a > b"') is True
    assert _bash_is_allowlisted("grep '->' file.py") is True


# ---------------------------------------------------------------------------
# #4 — Google / bearer token scrubbing
# ---------------------------------------------------------------------------

def test_content_filter_redacts_google_tokens():
    cf = ContentFilter()
    api_key = "AIza" + "B" * 35
    refresh = "1//" + "0Abc-_dEf" * 3            # >= 20 chars after 1//
    access = "ya29." + "X" * 25
    bearer = "Authorization: Bearer " + "z" * 24
    text = f"key={api_key} refresh={refresh} access={access}\n{bearer}"
    redacted, findings = cf.scrub_sync(text)
    names = {f.pattern_name for f in findings}
    assert "google_api_key" in names
    assert "google_oauth_refresh" in names
    assert "google_oauth_access" in names
    assert "bearer_token" in names
    # None of the raw secrets survive in the output.
    for secret in (api_key, refresh, access):
        assert secret not in redacted


def test_content_filter_no_false_positive_on_plain_text():
    cf = ContentFilter()
    redacted, findings = cf.scrub_sync("Meet me at 1//2 past noon, bearer of good news.")
    # "1//2" is too short for a refresh token; "bearer of" has no token after it.
    assert not any(f.pattern_name.startswith("google") for f in findings)


# ---------------------------------------------------------------------------
# #12 — restore rejects out-of-root destinations
# ---------------------------------------------------------------------------

def _make_archive(path: Path, entries: list[dict], payloads: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in payloads.items():
            zf.writestr(name, data)
        zf.writestr(_MANIFEST_NAME, json.dumps(
            {"version": _MANIFEST_VERSION, "entries": entries}))


def test_restore_skips_dest_outside_roots(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    home = tmp_path / "home"; home.mkdir()
    bak = tmp_path / "bak"; bak.mkdir()
    cfg = BackupConfig(project_root=proj, claude_home=home, backup_root=bak)

    good = proj / "sub" / "ok.bin"               # under project_root → allowed
    evil = tmp_path / "outside" / "evil.bin"     # under no root → must be skipped
    archive = bak / "a.zip"
    _make_archive(
        archive,
        entries=[
            {"archive": "g", "dest": str(good), "kind": "file"},
            {"archive": "e", "dest": str(evil), "kind": "file"},
        ],
        payloads={"g": b"good", "e": b"EVIL"},
    )

    restored = restore_backup(archive, cfg)
    assert good in restored and good.read_bytes() == b"good"
    assert evil not in restored
    assert not evil.exists()                     # never written outside the roots


# ---------------------------------------------------------------------------
# #13 — concurrent-instance crash marker
# ---------------------------------------------------------------------------

@pytest.fixture
def marker(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "agent.running"
    monkeypatch.setattr(crash_marker, "_MARKER", path)
    return path


def test_live_concurrent_owner_is_not_a_crash(marker, monkeypatch):
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 999999, "started": "x"}))
    monkeypatch.setattr(crash_marker, "_pid_alive", lambda pid: True)
    assert crash_marker.check_and_mark() is False        # peer alive → not a crash
    assert json.loads(marker.read_text())["pid"] == 999999  # marker untouched


def test_clear_preserves_other_instance_marker(marker):
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 999999, "started": "x"}))
    crash_marker.clear()                                  # not ours → don't delete
    assert marker.exists()
    assert json.loads(marker.read_text())["pid"] == 999999


def test_dead_owner_is_a_crash(marker, monkeypatch):
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 999999, "started": "x"}))
    monkeypatch.setattr(crash_marker, "_pid_alive", lambda pid: False)
    assert crash_marker.check_and_mark() is True          # dead owner → crash
    assert json.loads(marker.read_text())["pid"] == os.getpid()  # taken over
