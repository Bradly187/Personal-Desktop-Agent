"""Tests for scripts/backup_agent_state.py — backup/restore round-trip.

Covers:
  - WAL-safe SQLite snapshot (live writer connection open during backup)
  - source collection (missing paths skipped; credentials excluded by default)
  - archive contents + manifest
  - rotation (keep last N)
  - restore round-trip with safety renames (incl. stale -wal/-shm aside)
  - "latest" archive selection
  - DPAPI credential encryption (Windows + pywin32 only)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backup_agent_state import (
    _MANIFEST_NAME,
    BackupConfig,
    collect_sources,
    create_backup,
    find_latest_backup,
    restore_backup,
    rotate_backups,
    snapshot_sqlite,
)

try:
    import win32crypt  # noqa: F401

    _HAS_DPAPI = True
except ImportError:  # pragma: no cover
    _HAS_DPAPI = False


def _make_db(path: Path, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO kv VALUES (?, ?)", rows)
    conn.close()


def _read_rows(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT k, v FROM kv ORDER BY k").fetchall())


@pytest.fixture
def env(tmp_path: Path) -> BackupConfig:
    """A miniature project root + ~/.claude with every backup source present."""
    project = tmp_path / "project"
    home = tmp_path / "claude_home"
    project.mkdir()
    home.mkdir()

    _make_db(project / "agent.db", [("threshold", "0.42")])
    _make_db(project / "audit.db", [("event", "session_start")])

    (project / "chroma_db").mkdir()
    (project / "chroma_db" / "chroma.sqlite3").write_text("codebase-index")
    (project / "chroma_db" / "seg").mkdir()
    (project / "chroma_db" / "seg" / "data.bin").write_bytes(b"\x00\x01")

    (project / "approval_config.json").write_text('{"voice_id": "Danielle"}')
    (project / "cluster_config.json").write_text('{"laptop": {}}')
    (project / "skills" / "manifests").mkdir(parents=True)
    (project / "skills" / "manifests" / "echo.json").write_text('{"enabled": false}')

    (home / "ipad_bridge").mkdir()
    (home / "ipad_bridge" / "paired_token").write_text("tok-abc123")
    (home / "ipad_bridge" / "config.json").write_text('{"writable_roots": []}')
    (home / "personal_kb").mkdir()
    (home / "personal_kb" / "config.json").write_text('{"roots": []}')
    (home / "personal_kb" / "state.json").write_text('{"mtimes": {}}')
    (home / "personal_kb" / "chroma").mkdir()
    (home / "personal_kb" / "chroma" / "chroma.sqlite3").write_text("kb-index")

    (home / "skills" / "credentials" / "google_pim").mkdir(parents=True)
    (home / "skills" / "credentials" / "google_pim" / "token.json").write_text(
        '{"refresh_token": "SECRET"}'
    )

    return BackupConfig(
        project_root=project,
        claude_home=home,
        backup_root=tmp_path / "backups",
        keep=3,
    )


# ===========================================================================
# SQLite snapshot
# ===========================================================================

class TestSqliteSnapshot:
    def test_round_trip(self, tmp_path: Path):
        src = tmp_path / "a.db"
        _make_db(src, [("x", "1"), ("y", "2")])
        snap = tmp_path / "out" / "a.db"
        snapshot_sqlite(src, snap)
        assert _read_rows(snap) == {"x": "1", "y": "2"}

    def test_captures_wal_content_with_live_writer(self, tmp_path: Path):
        """Rows committed to the WAL (not yet checkpointed, writer still open)
        must appear in the snapshot — the failure mode of a raw file copy."""
        src = tmp_path / "live.db"
        _make_db(src, [("base", "row")])
        writer = sqlite3.connect(src)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO kv VALUES ('wal_only', 'committed')")
            writer.commit()  # lands in -wal, no checkpoint
            snap = tmp_path / "snap.db"
            snapshot_sqlite(src, snap)
        finally:
            writer.close()
        assert _read_rows(snap) == {"base": "row", "wal_only": "committed"}


# ===========================================================================
# Source collection
# ===========================================================================

class TestCollectSources:
    def test_all_present(self, env: BackupConfig):
        archives = {s.archive for s in collect_sources(env)}
        assert "db/agent.db" in archives
        assert "db/audit.db" in archives
        assert "chroma/chroma_db" in archives
        assert "chroma/personal_kb" in archives
        assert "config/ipad_bridge/paired_token" in archives
        assert "config/personal_kb/state.json" in archives

    def test_credentials_excluded_by_default(self, env: BackupConfig):
        assert not any(
            s.archive.startswith("credentials/") for s in collect_sources(env)
        )

    def test_credentials_included_when_opted_in(self, env: BackupConfig):
        env.include_credentials = True
        creds = [s for s in collect_sources(env) if s.kind == "dpapi"]
        assert len(creds) == 1
        assert creds[0].archive == "credentials/google_pim/token.json.dpapi"

    def test_missing_sources_skipped(self, env: BackupConfig, caplog):
        (env.project_root / "cluster_config.json").unlink()
        with caplog.at_level("WARNING"):
            sources = collect_sources(env)
        assert not any(s.archive == "config/cluster_config.json" for s in sources)
        assert any("cluster_config.json" in r.message for r in caplog.records)


# ===========================================================================
# Backup
# ===========================================================================

class TestCreateBackup:
    def test_archive_contents_and_manifest(self, env: BackupConfig):
        archive = create_backup(env)
        assert archive.exists()
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            assert "db/agent.db" in names
            assert "chroma/chroma_db/seg/data.bin" in names
            assert "chroma/personal_kb/chroma.sqlite3" in names
            assert "config/skills_manifests/echo.json" in names
            manifest = json.loads(zf.read(_MANIFEST_NAME))
        assert manifest["includes_credentials"] is False
        dests = {e["dest"] for e in manifest["entries"]}
        assert str(env.project_root / "agent.db") in dests
        # no plaintext token anywhere in the archive
        assert not any("credentials" in n for n in names)

    def test_db_snapshot_inside_zip_is_valid(self, env: BackupConfig, tmp_path: Path):
        archive = create_backup(env)
        with zipfile.ZipFile(archive) as zf:
            out = tmp_path / "extracted.db"
            out.write_bytes(zf.read("db/agent.db"))
        assert _read_rows(out) == {"threshold": "0.42"}

    def test_empty_environment_raises(self, tmp_path: Path):
        cfg = BackupConfig(
            project_root=tmp_path / "nope",
            claude_home=tmp_path / "nada",
            backup_root=tmp_path / "backups",
        )
        with pytest.raises(RuntimeError, match="Nothing to back up"):
            create_backup(cfg)

    def test_rotation_keeps_last_n(self, env: BackupConfig):
        for _ in range(5):
            create_backup(env)  # keep=3
        remaining = sorted(env.backup_root.glob("desktop-agent-backup-*.zip"))
        assert len(remaining) == 3

    def test_rotate_returns_deleted(self, env: BackupConfig):
        paths = [create_backup(env) for _ in range(3)]
        doomed = rotate_backups(env.backup_root, keep=1)
        assert doomed == sorted(paths)[:2]
        assert sorted(env.backup_root.glob("*.zip")) == [sorted(paths)[2]]


# ===========================================================================
# Restore
# ===========================================================================

class TestRestore:
    def test_round_trip_after_damage(self, env: BackupConfig):
        archive = create_backup(env)
        # Damage everything restorable
        _make_db(env.project_root / "agent.db.tmp", [("threshold", "9.99")])
        (env.project_root / "agent.db").unlink()
        (env.project_root / "agent.db.tmp").rename(env.project_root / "agent.db")
        (env.project_root / "chroma_db" / "seg" / "data.bin").unlink()
        (env.claude_home / "ipad_bridge" / "paired_token").write_text("WRONG")

        restored = restore_backup(archive, env)

        assert _read_rows(env.project_root / "agent.db") == {"threshold": "0.42"}
        assert (env.project_root / "chroma_db" / "seg" / "data.bin").read_bytes() == b"\x00\x01"
        assert (env.claude_home / "ipad_bridge" / "paired_token").read_text() == "tok-abc123"
        assert env.project_root / "agent.db" in restored

    def test_safety_rename_preserves_existing(self, env: BackupConfig):
        archive = create_backup(env)
        (env.claude_home / "ipad_bridge" / "paired_token").write_text("current")
        restore_backup(archive, env)
        asides = list((env.claude_home / "ipad_bridge").glob("paired_token.pre-restore-*"))
        assert len(asides) == 1
        assert asides[0].read_text() == "current"

    def test_stale_wal_shm_moved_aside(self, env: BackupConfig):
        archive = create_backup(env)
        wal = env.project_root / "agent.db-wal"
        shm = env.project_root / "agent.db-shm"
        wal.write_bytes(b"stale-wal")
        shm.write_bytes(b"stale-shm")
        restore_backup(archive, env)
        assert not wal.exists()
        assert not shm.exists()
        assert list(env.project_root.glob("agent.db-wal.pre-restore-*"))
        assert list(env.project_root.glob("agent.db-shm.pre-restore-*"))

    def test_existing_dir_moved_aside(self, env: BackupConfig):
        archive = create_backup(env)
        marker = env.project_root / "chroma_db" / "post-backup-file.txt"
        marker.write_text("added after backup")
        restore_backup(archive, env)
        assert not marker.exists()  # dir was replaced wholesale
        asides = list(env.project_root.glob("chroma_db.pre-restore-*"))
        assert len(asides) == 1
        assert (asides[0] / "post-backup-file.txt").exists()

    def test_restore_warns_about_credentials(self, env: BackupConfig, caplog):
        archive = create_backup(env)
        with caplog.at_level("WARNING"):
            restore_backup(archive, env)
        assert any("re-run" in r.message for r in caplog.records)

    def test_find_latest(self, env: BackupConfig):
        paths = [create_backup(env) for _ in range(3)]
        assert find_latest_backup(env.backup_root) == sorted(paths)[-1]

    def test_find_latest_empty_raises(self, tmp_path: Path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(FileNotFoundError):
            find_latest_backup(tmp_path / "empty")

    def test_tampered_manifest_rejected(self, env: BackupConfig):
        archive = create_backup(env)
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(_MANIFEST_NAME))
            payload = {n: zf.read(n) for n in zf.namelist() if n != _MANIFEST_NAME}
        manifest["entries"][0]["archive"] = "../../evil"
        evil = archive.with_name("evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            for name, data in payload.items():
                zf.writestr(name, data)
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest))
        with pytest.raises(ValueError, match="Unsafe archive path"):
            restore_backup(evil, env)


# ===========================================================================
# DPAPI credentials (Windows-only)
# ===========================================================================

@pytest.mark.skipif(not _HAS_DPAPI, reason="pywin32/DPAPI unavailable")
class TestDpapiCredentials:
    def test_token_encrypted_in_archive(self, env: BackupConfig):
        env.include_credentials = True
        archive = create_backup(env)
        with zipfile.ZipFile(archive) as zf:
            blob = zf.read("credentials/google_pim/token.json.dpapi")
        assert b"SECRET" not in blob  # never plaintext at rest

    def test_credential_restore_round_trip(self, env: BackupConfig):
        env.include_credentials = True
        archive = create_backup(env)
        token = env.claude_home / "skills" / "credentials" / "google_pim" / "token.json"
        token.write_text("clobbered")
        restore_backup(archive, env)
        assert json.loads(token.read_text()) == {"refresh_token": "SECRET"}


# ===========================================================================
# Atomic archive creation — a failed backup must leave nothing behind
# ===========================================================================

class TestAtomicArchive:
    def test_failed_backup_leaves_no_archive(self, env: BackupConfig, monkeypatch):
        import scripts.backup_agent_state as bs

        def boom(src, dst):
            raise RuntimeError("disk full")

        monkeypatch.setattr(bs, "snapshot_sqlite", boom)
        with pytest.raises(RuntimeError, match="disk full"):
            create_backup(env)
        # Neither a partial .zip (would poison "latest"/rotation) nor an
        # abandoned .tmp may survive a failed run.
        assert list(env.backup_root.glob("*.zip")) == []
        assert list(env.backup_root.glob("*.tmp")) == []

    def test_failed_backup_does_not_poison_latest(self, env: BackupConfig, monkeypatch):
        import scripts.backup_agent_state as bs

        good = create_backup(env)

        def boom(src, dst):
            raise RuntimeError("disk full")

        monkeypatch.setattr(bs, "snapshot_sqlite", boom)
        with pytest.raises(RuntimeError):
            create_backup(env)
        assert find_latest_backup(env.backup_root) == good
