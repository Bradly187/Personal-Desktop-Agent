"""v8→9 migration: sensor_telemetry gains trace_id (sensor→command correlation).

Follows the changing-the-db-schema skill: verify the UPGRADE path (a real v8 DB
with data moves forward losslessly), the fresh-create path, the deferred index,
and that insert_sensor_telemetry round-trips the new column.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION

# sensor_telemetry exactly as it existed at user_version 8 (no trace_id).
_V8_SENSOR_TELEMETRY = """
CREATE TABLE sensor_telemetry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER,
    ts               REAL    NOT NULL,
    tilt_rx          REAL,
    tilt_ry          REAL,
    gaze_dx          REAL,
    gaze_dy          REAL,
    gaze_conf        REAL,
    head_pitch       REAL,
    head_yaw         REAL,
    cursor_x         INTEGER,
    cursor_y         INTEGER,
    pain_day_active  INTEGER NOT NULL DEFAULT 0,
    active_source    TEXT,
    gesture_conf     REAL,
    rms_ambient      REAL
);
"""


def _make_v8_db(path: str) -> None:
    c = sqlite3.connect(path)
    try:
        c.executescript(_V8_SENSOR_TELEMETRY)
        c.execute("INSERT INTO sensor_telemetry (session_id, ts, tilt_rx) "
                  "VALUES (1, 1000.0, 0.5)")
        c.execute("PRAGMA user_version = 8")
        c.commit()
    finally:
        c.close()


async def test_v8_db_migrates_forward_losslessly(tmp_path):
    p = tmp_path / "v8_agent.db"
    _make_v8_db(str(p))

    db = AgentDB()
    await db.open(p)
    await db.close()

    c = sqlite3.connect(str(p))
    cols = [r[1] for r in c.execute("PRAGMA table_info(sensor_telemetry)")]
    idxs = [r[1] for r in c.execute("PRAGMA index_list(sensor_telemetry)")]
    row = c.execute(
        "SELECT session_id, ts, tilt_rx, trace_id FROM sensor_telemetry").fetchone()
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()

    assert "trace_id" in cols
    assert ver == _AGENT_DB_SCHEMA_VERSION == 9
    assert row == (1, 1000.0, 0.5, None)        # pre-migration data intact
    assert "idx_st_trace" in idxs               # deferred index built post-migrate


async def test_insert_round_trips_trace_id(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "fresh.db")
    await db.telemetry.insert_sensor_telemetry(1, 2000.0, tilt_rx=0.1, trace_id="tr-abc")
    await db.telemetry.insert_sensor_telemetry(1, 2001.0, tilt_rx=0.2)    # no trace in window
    await db.close()

    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    rows = c.execute(
        "SELECT ts, trace_id FROM sensor_telemetry ORDER BY ts").fetchall()
    c.close()
    assert rows == [(2000.0, "tr-abc"), (2001.0, None)]
