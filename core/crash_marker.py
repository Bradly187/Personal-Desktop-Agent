"""Crash marker — detect unclean process exits across restarts.

A marker file (``logs/agent.running``) is written when the pipeline starts and
removed only after a fully graceful shutdown. If the marker already exists at
startup, the previous process died without cleaning up (crash, hard kill,
power loss) — the caller can tell the user via TTS so they know recovered
state may apply (goal_queue requeue and interrupted-plan reconciliation
already handle the data side).

This is deliberately process-level and dumb: no pid liveness probing, no
locking. The watchdog (scripts/agent_watchdog.ps1) guarantees only one agent
instance runs, so a present marker at startup always means an unclean exit.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_MARKER = Path(__file__).resolve().parent.parent / "logs" / "agent.running"


def check_and_mark() -> bool:
    """Return True if the previous run exited uncleanly, then write the marker.

    Never raises — a marker that can't be written degrades to "crash detection
    unavailable", not a startup failure.
    """
    crashed = _MARKER.exists()
    if crashed:
        prev = ""
        try:
            info = json.loads(_MARKER.read_text(encoding="utf-8"))
            prev = f" (pid {info.get('pid')}, started {info.get('started')})"
        except Exception:
            pass
        log.warning("Unclean shutdown detected — previous run%s left its crash marker", prev)
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(
            json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not write crash marker: %s", exc)
    return crashed


def clear() -> None:
    """Remove the marker after a graceful shutdown. Never raises."""
    try:
        _MARKER.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear crash marker: %s", exc)
