#!/usr/bin/env python3
"""Backup / restore the agent's persistent state.

Months of learned behaviour live outside git: agent.db (learned thresholds,
few-shot examples, behavioral twin, goal queue), audit.db (append-only audit
log), the ChromaDB RAG stores, and the small config/state files under
~/.claude/. This script snapshots all of it into one dated zip archive.

What gets backed up:
  - agent.db + audit.db        — via the sqlite3 backup API (WAL-safe; never
                                 a raw copy of a live database)
  - chroma_db/                 — codebase RAG (raw copy; chroma's internal
                                 sqlite is only written during indexing, and
                                 the store can always be re-indexed)
  - ~/.claude/personal_kb/     — personal-document RAG + config.json/state.json
  - approval_config.json, cluster_config.json, skills/manifests/
  - ~/.claude/ipad_bridge/     — paired_token + config.json

OAuth refresh tokens (~/.claude/skills/credentials/) are EXCLUDED by default —
the recovery path after a restore is to re-run
``skills/servers/google_pim_auth.py`` once per Google account. Pass
``--include-credentials`` to include them DPAPI-encrypted (decryptable only by
the same Windows user on the same machine; requires pywin32). Plaintext tokens
are never written to an archive.

Usage:
    python scripts/backup_agent_state.py                       # take a backup
    python scripts/backup_agent_state.py --backup-root D:\\bak --keep 14
    python scripts/backup_agent_state.py --restore latest      # newest archive
    python scripts/backup_agent_state.py --restore <path.zip>
    python scripts/backup_agent_state.py --install-schedule --schedule-time 03:00

Restore stops short of being destructive: every file/dir it would overwrite is
first renamed to ``<name>.pre-restore-<timestamp>``. Stop the agent (main.py)
before restoring — restoring agent.db under a live writer is undefined.
Manifest destinations are absolute paths, so restore targets the machine the
backup was taken on.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

logger = logging.getLogger("backup_agent_state")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ARCHIVE_PREFIX = "desktop-agent-backup-"
_MANIFEST_NAME = "MANIFEST.json"
_MANIFEST_VERSION = 1
_DPAPI_SUFFIX = ".dpapi"
_TASK_NAME = "DesktopAgentBackup"
_RECOVERY_NOTE = (
    "OAuth tokens are not in this archive. After restoring, re-run "
    "skills/servers/google_pim_auth.py to re-authorize Google skills."
)


@dataclass
class BackupConfig:
    """Resolved locations for one backup/restore run (injectable for tests)."""

    project_root: Path = _PROJECT_ROOT
    claude_home: Path = field(default_factory=lambda: Path.home() / ".claude")
    backup_root: Path = field(
        default_factory=lambda: Path.home() / "Backups" / "desktop-agent"
    )
    keep: int = 7
    include_credentials: bool = False


@dataclass
class Source:
    """One thing to back up: a source path and where it lands in the zip."""

    path: Path
    archive: str  # forward-slash path (or dir prefix) inside the zip
    kind: str  # "sqlite" | "file" | "dir" | "dpapi"


# ---------------------------------------------------------------------------
# Source collection
# ---------------------------------------------------------------------------

def collect_sources(cfg: BackupConfig) -> list[Source]:
    """Resolve the backup manifest; missing sources are skipped with a warning."""
    candidates = [
        Source(cfg.project_root / "agent.db", "db/agent.db", "sqlite"),
        Source(cfg.project_root / "audit.db", "db/audit.db", "sqlite"),
        Source(cfg.project_root / "chroma_db", "chroma/chroma_db", "dir"),
        Source(cfg.claude_home / "personal_kb" / "chroma", "chroma/personal_kb", "dir"),
        Source(cfg.project_root / "approval_config.json", "config/approval_config.json", "file"),
        Source(cfg.project_root / "cluster_config.json", "config/cluster_config.json", "file"),
        Source(cfg.project_root / "skills" / "manifests", "config/skills_manifests", "dir"),
        Source(cfg.claude_home / "ipad_bridge" / "paired_token", "config/ipad_bridge/paired_token", "file"),
        Source(cfg.claude_home / "ipad_bridge" / "config.json", "config/ipad_bridge/config.json", "file"),
        Source(cfg.claude_home / "personal_kb" / "config.json", "config/personal_kb/config.json", "file"),
        Source(cfg.claude_home / "personal_kb" / "state.json", "config/personal_kb/state.json", "file"),
    ]
    if cfg.include_credentials:
        cred_root = cfg.claude_home / "skills" / "credentials"
        if cred_root.is_dir():
            for f in sorted(p for p in cred_root.rglob("*") if p.is_file()):
                rel = f.relative_to(cred_root).as_posix()
                candidates.append(
                    Source(f, f"credentials/{rel}{_DPAPI_SUFFIX}", "dpapi")
                )
        else:
            logger.warning("--include-credentials set but %s does not exist", cred_root)

    sources: list[Source] = []
    for src in candidates:
        if src.path.exists():
            sources.append(src)
        else:
            logger.warning("Skipping missing source: %s", src.path)
    return sources


# ---------------------------------------------------------------------------
# SQLite + DPAPI primitives
# ---------------------------------------------------------------------------

def snapshot_sqlite(src: Path, dst: Path) -> None:
    """Copy a (possibly live, WAL-mode) SQLite db via the online backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    ) as conn, closing(sqlite3.connect(dst)) as snap:
        conn.backup(snap)
    logger.info("SQLite snapshot: %s -> %s", src, dst)


def _dpapi_protect(data: bytes) -> bytes:
    import win32crypt  # pywin32; deferred so non-credential runs never need it

    return win32crypt.CryptProtectData(data, "desktop-agent-backup", None, None, None, 0)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _unique_archive_path(backup_root: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_root / f"{_ARCHIVE_PREFIX}{stamp}.zip"
    counter = 1
    while path.exists():
        path = backup_root / f"{_ARCHIVE_PREFIX}{stamp}-{counter}.zip"
        counter += 1
    return path


def create_backup(cfg: BackupConfig) -> Path:
    """Take a full backup; returns the path of the created archive."""
    cfg.backup_root.mkdir(parents=True, exist_ok=True)
    sources = collect_sources(cfg)
    if not sources:
        raise RuntimeError("Nothing to back up — no sources found")

    archive = _unique_archive_path(cfg.backup_root)
    entries: list[dict[str, str]] = []

    with TemporaryDirectory(prefix="da-backup-") as tmp:
        tmpdir = Path(tmp)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in sources:
                if src.kind == "sqlite":
                    snap = tmpdir / Path(src.archive).name
                    snapshot_sqlite(src.path, snap)
                    zf.write(snap, src.archive)
                elif src.kind == "file":
                    zf.write(src.path, src.archive)
                elif src.kind == "dir":
                    for f in sorted(p for p in src.path.rglob("*") if p.is_file()):
                        rel = f.relative_to(src.path).as_posix()
                        zf.write(f, f"{src.archive}/{rel}")
                elif src.kind == "dpapi":
                    zf.writestr(src.archive, _dpapi_protect(src.path.read_bytes()))
                else:  # pragma: no cover — kinds are fixed above
                    raise ValueError(f"Unknown source kind: {src.kind}")
                entries.append(
                    {"archive": src.archive, "dest": str(src.path), "kind": src.kind}
                )
                logger.info("Backed up %s (%s)", src.path, src.kind)

            manifest = {
                "version": _MANIFEST_VERSION,
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "includes_credentials": cfg.include_credentials,
                "credential_recovery": _RECOVERY_NOTE,
                "entries": entries,
            }
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))

    logger.info("Backup written: %s (%d sources)", archive, len(entries))
    rotate_backups(cfg.backup_root, cfg.keep)
    return archive


def rotate_backups(backup_root: Path, keep: int) -> list[Path]:
    """Delete all but the newest *keep* archives; returns the deleted paths."""
    if keep <= 0:
        return []
    archives = sorted(backup_root.glob(f"{_ARCHIVE_PREFIX}*.zip"))
    doomed = archives[:-keep]
    for path in doomed:
        path.unlink()
        logger.info("Rotated out old backup: %s", path)
    return doomed


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _safety_rename(target: Path, stamp: str) -> None:
    """Move an existing file/dir aside instead of overwriting it."""
    if target.exists():
        aside = target.with_name(f"{target.name}.pre-restore-{stamp}")
        counter = 1
        while aside.exists():
            aside = target.with_name(f"{target.name}.pre-restore-{stamp}-{counter}")
            counter += 1
        target.rename(aside)
        logger.info("Safety rename: %s -> %s", target, aside.name)


def find_latest_backup(backup_root: Path) -> Path:
    archives = sorted(backup_root.glob(f"{_ARCHIVE_PREFIX}*.zip"))
    if not archives:
        raise FileNotFoundError(f"No backup archives under {backup_root}")
    return archives[-1]


def restore_backup(archive: Path) -> list[Path]:
    """Restore an archive into place; returns the list of restored dest paths.

    Every existing destination (and, for SQLite dbs, any sibling -wal/-shm
    files) is renamed to ``<name>.pre-restore-<timestamp>`` first.
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    restored: list[Path] = []

    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read(_MANIFEST_NAME))
        if manifest.get("version") != _MANIFEST_VERSION:
            raise ValueError(f"Unsupported manifest version in {archive}")
        members = set(zf.namelist())

        for entry in manifest["entries"]:
            dest = Path(entry["dest"])
            kind = entry["kind"]
            arc = entry["archive"]
            if ".." in Path(arc).parts:  # defence against a tampered manifest
                raise ValueError(f"Unsafe archive path in manifest: {arc}")

            _safety_rename(dest, stamp)
            dest.parent.mkdir(parents=True, exist_ok=True)

            if kind in ("file", "sqlite"):
                dest.write_bytes(zf.read(arc))
                if kind == "sqlite":
                    # A restored db must not pair with a stale WAL/SHM.
                    for suffix in ("-wal", "-shm"):
                        _safety_rename(dest.with_name(dest.name + suffix), stamp)
            elif kind == "dir":
                prefix = arc.rstrip("/") + "/"
                for member in sorted(m for m in members if m.startswith(prefix)):
                    rel = Path(member[len(prefix):])
                    if ".." in rel.parts:
                        raise ValueError(f"Unsafe member path: {member}")
                    out = dest / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(zf.read(member))
            elif kind == "dpapi":
                dest.write_bytes(_dpapi_unprotect(zf.read(arc)))
            else:
                raise ValueError(f"Unknown entry kind in manifest: {kind}")

            restored.append(dest)
            logger.info("Restored %s (%s)", dest, kind)

    if not manifest.get("includes_credentials"):
        logger.warning(_RECOVERY_NOTE)
    logger.info("Restore complete: %d entries from %s", len(restored), archive)
    return restored


# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------

def install_schedule(cfg: BackupConfig, time_str: str = "03:00") -> None:
    """Register a daily Windows Task Scheduler entry running this script."""
    script = Path(__file__).resolve()
    run_cmd = (
        f'"{sys.executable}" "{script}"'
        f' --backup-root "{cfg.backup_root}" --keep {cfg.keep}'
    )
    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "DAILY",
        "/ST", time_str,
        "/TN", _TASK_NAME,
        "/TR", run_cmd,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"schtasks failed: {result.stderr.strip()}")
    logger.info("Scheduled task %r registered: daily at %s", _TASK_NAME, time_str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--backup-root", type=Path, default=None,
        help=r"Backup destination (default: %%USERPROFILE%%\Backups\desktop-agent)",
    )
    parser.add_argument("--keep", type=int, default=7, help="Archives to retain (default 7)")
    parser.add_argument(
        "--include-credentials", action="store_true",
        help="Include OAuth tokens, DPAPI-encrypted (default: excluded; re-auth on restore)",
    )
    parser.add_argument(
        "--restore", metavar="ARCHIVE",
        help='Restore a backup zip into place ("latest" picks the newest archive)',
    )
    parser.add_argument(
        "--install-schedule", action="store_true",
        help="Register a daily Task Scheduler entry for this backup",
    )
    parser.add_argument("--schedule-time", default="03:00", help="HH:MM for --install-schedule")
    parser.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = BackupConfig(keep=args.keep, include_credentials=args.include_credentials)
    if args.backup_root is not None:
        cfg.backup_root = args.backup_root

    try:
        if args.install_schedule:
            install_schedule(cfg, args.schedule_time)
            return 0
        if args.restore:
            archive = (
                find_latest_backup(cfg.backup_root)
                if args.restore == "latest"
                else Path(args.restore)
            )
            restore_backup(archive)
            return 0
        create_backup(cfg)
        return 0
    except Exception:
        logger.exception("backup_agent_state failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
