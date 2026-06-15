"""pain_journal_server — local voice pain/symptom + medication journal (stdio MCP skill).

Built for the single RA user this agent serves: on a flare day, hands hurt and a
phone app is the wrong tool — a spoken "log a flare, hands, six out of ten" is.
All data lives in a private local SQLite db at ~/.claude/health/pain.db, OUTSIDE
the repo, and NEVER leaves the machine: every tool here is read or LOCAL-write,
so there are no `send_tools` and no cloud egress (a local write is not a "send",
matching the files/notes skills — voice journaling must be zero-friction).

This is also a data flywheel: the symptom rows are exactly the labelled signal the
PainDayEngine and the weather-pressure flare-correlation already want, so each
entry makes routing smarter. (A future flare_summary could JOIN the weather
skill's 12-hour pressure trend; kept self-contained for now.)

Helpers are pure functions that take an explicit db path + injectable `now`, so
they unit-test without touching the real journal (see tests/test_pain_journal_skill.py).

Run standalone:  python -m skills.servers.pain_journal_server
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pain_journal")

_MAX_SEVERITY = 10
_DAY_S = 86_400.0


# --------------------------------------------------------------------------- #
# Storage (pure helpers — db path passed in, so tests use a tmp db)
# --------------------------------------------------------------------------- #

def _db_path() -> Path:
    """Journal location: private, outside the repo. ``DA_PAIN_DB`` overrides
    (used by tests and to relocate the journal), mirroring ``DA_NOTES_ROOT``."""
    override = os.environ.get("DA_PAIN_DB")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "health" / "pain.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parent dir + schema if needed). Caller closes."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS symptoms ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "area TEXT NOT NULL, severity INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meds ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "name TEXT NOT NULL, dose TEXT NOT NULL DEFAULT '')"
    )
    return conn


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def _clamp_severity(severity: int) -> int:
    try:
        s = int(severity)
    except (TypeError, ValueError):
        s = 0
    return max(0, min(_MAX_SEVERITY, s))


def _log_symptom(
    db_path: Path, area: str, severity: int, note: str = "",
    now: Optional[float] = None,
) -> str:
    """Record one symptom observation. Honest no-op if no body area is given."""
    area = (area or "").strip()
    if not area:
        return "I didn't catch which area — try 'log a flare, hands, six out of ten'."
    sev = _clamp_severity(severity)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO symptoms (ts, area, severity, note) VALUES (?, ?, ?, ?)",
            (_now(now), area.lower(), sev, (note or "").strip()),
        )
        conn.commit()
    finally:
        conn.close()
    tail = f" — {note.strip()}" if note and note.strip() else ""
    return f"Logged {area.lower()} at {sev} out of {_MAX_SEVERITY}{tail}."


def _log_med(
    db_path: Path, name: str, dose: str = "", now: Optional[float] = None,
) -> str:
    """Record that a medication/dose was taken."""
    name = (name or "").strip()
    if not name:
        return "I didn't catch the medication name."
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO meds (ts, name, dose) VALUES (?, ?, ?)",
            (_now(now), name.lower(), (dose or "").strip()),
        )
        conn.commit()
    finally:
        conn.close()
    dose_str = f" ({dose.strip()})" if dose and dose.strip() else ""
    return f"Logged {name.lower()}{dose_str} taken."


def _recent_symptoms(db_path: Path, days: int = 7, now: Optional[float] = None) -> str:
    """List symptom entries from the last `days`, most recent first."""
    if not db_path.exists():
        return "No pain logged yet."
    cutoff = _now(now) - max(1, days) * _DAY_S
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, area, severity, note FROM symptoms WHERE ts >= ? "
            "ORDER BY ts DESC", (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"No pain logged in the last {days} days."
    lines = [f"Last {days} days — {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}:"]
    for ts, area, sev, note in rows:
        when = time.strftime("%a %d %b %H:%M", time.localtime(ts))
        tail = f" — {note}" if note else ""
        lines.append(f"- {when}: {area} {sev}/{_MAX_SEVERITY}{tail}")
    return "\n".join(lines)


def _flare_summary(db_path: Path, days: int = 7, now: Optional[float] = None) -> str:
    """Aggregate the last `days`: entry count, average + peak severity, worst area."""
    if not db_path.exists():
        return "No pain logged yet."
    cutoff = _now(now) - max(1, days) * _DAY_S
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, area, severity FROM symptoms WHERE ts >= ?", (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"No pain logged in the last {days} days."
    severities = [r[2] for r in rows]
    avg = sum(severities) / len(severities)
    peak = max(severities)
    distinct_days = len({int(r[0] // _DAY_S) for r in rows})
    # Worst area = highest mean severity (ties broken by frequency).
    by_area: dict[str, list[int]] = {}
    for _ts, area, sev in rows:
        by_area.setdefault(area, []).append(sev)
    worst = max(by_area.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), len(kv[1])))
    return (
        f"Last {days} days: {len(rows)} entries across {distinct_days} day(s). "
        f"Average severity {avg:.1f}/{_MAX_SEVERITY}, peak {peak}. "
        f"Worst area: {worst[0]} (avg {sum(worst[1]) / len(worst[1]):.1f})."
    )


def _appointment_brief(db_path: Path, days: int = 30, now: Optional[float] = None) -> str:
    """A plain summary to bring to a rheumatology appointment."""
    if not db_path.exists():
        return "No pain logged yet."
    cutoff = _now(now) - max(1, days) * _DAY_S
    conn = _connect(db_path)
    try:
        symptoms = conn.execute(
            "SELECT area, severity FROM symptoms WHERE ts >= ?", (cutoff,),
        ).fetchall()
        meds = conn.execute(
            "SELECT name, COUNT(*) FROM meds WHERE ts >= ? GROUP BY name "
            "ORDER BY COUNT(*) DESC", (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    if not symptoms and not meds:
        return f"Nothing logged in the last {days} days."

    lines = [f"Summary for the last {days} days:"]
    if symptoms:
        by_area: dict[str, list[int]] = {}
        for area, sev in symptoms:
            by_area.setdefault(area, []).append(sev)
        lines.append(f"Symptoms ({len(symptoms)} entries):")
        for area, sevs in sorted(by_area.items(), key=lambda kv: -len(kv[1])):
            lines.append(
                f"- {area}: {len(sevs)}x, severity {min(sevs)}-{max(sevs)} "
                f"(avg {sum(sevs) / len(sevs):.1f})"
            )
    if meds:
        lines.append("Medications logged:")
        for name, count in meds:
            lines.append(f"- {name}: {count}x")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# MCP tools (thin wrappers over the helpers)
# --------------------------------------------------------------------------- #

@mcp.tool()
def log_symptom(area: str, severity: int = 0, note: str = "") -> str:
    """Record a symptom: body area, 0-10 severity, optional note (local write)."""
    return _log_symptom(_db_path(), area, severity, note)


@mcp.tool()
def log_med(name: str, dose: str = "") -> str:
    """Record that a medication/dose was taken (local write)."""
    return _log_med(_db_path(), name, dose)


@mcp.tool()
def recent_symptoms(days: int = 7) -> str:
    """List symptom entries from the last N days, newest first (read-only)."""
    return _recent_symptoms(_db_path(), days)


@mcp.tool()
def flare_summary(days: int = 7) -> str:
    """Aggregate severity/frequency/worst-area over the last N days (read-only)."""
    return _flare_summary(_db_path(), days)


@mcp.tool()
def appointment_brief(days: int = 30) -> str:
    """A plain symptom + medication summary to bring to an appointment (read-only)."""
    return _appointment_brief(_db_path(), days)


if __name__ == "__main__":
    mcp.run(transport="stdio")
