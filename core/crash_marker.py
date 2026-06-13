"""Crash marker — detect unclean process exits across restarts.

A marker file (``logs/agent.running``) is written when the pipeline starts and
removed only after a fully graceful shutdown. If the marker already exists at
startup, the previous process died without cleaning up (crash, hard kill,
power loss) — the caller can tell the user via TTS so they know recovered
state may apply (goal_queue requeue and interrupted-plan reconciliation
already handle the data side).

The marker records the owning pid. A present marker at startup is treated as a
crash ONLY if its pid is dead (or is our own) — a marker owned by a different,
still-alive process means a concurrent instance is running (e.g. a manual
start_agent.bat alongside the watchdog), not a crash (#13). In that case we
neither claim a crash nor overwrite/delete the live peer's marker.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_MARKER = Path(__file__).resolve().parent.parent / "logs" / "agent.running"


def _pid_alive(pid) -> bool:
    """True if `pid` names a live process; False on a bad/None pid."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return True
    return True


def check_and_mark() -> bool:
    """Return True if the previous run exited uncleanly, then write the marker.

    Never raises — a marker that can't be written degrades to "crash detection
    unavailable", not a startup failure.
    """
    crashed = False
    if _MARKER.exists():
        prev_pid = None
        prev_started = None
        try:
            info = json.loads(_MARKER.read_text(encoding="utf-8"))
            prev_pid = info.get("pid")
            prev_started = info.get("started")
        except Exception:
            pass   # corrupt marker → treat as a crash (prev_pid stays None)
        if (prev_pid is not None and int(prev_pid) != os.getpid()
                and _pid_alive(prev_pid)):
            # A different, live process owns the marker → concurrent instance,
            # not a crash. Leave its marker untouched and don't claim a crash.
            log.warning(
                "Crash marker owned by live pid %s — concurrent instance, not a crash",
                prev_pid)
            return False
        crashed = True
        log.warning(
            "Unclean shutdown detected — previous run (pid %s, started %s) left its crash marker",
            prev_pid, prev_started)
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
    """Remove the marker after a graceful shutdown. Never raises.

    Only removes a marker we own (matching pid) so a clean shutdown can't delete
    a concurrent instance's marker out from under it (#13)."""
    try:
        if _MARKER.exists():
            try:
                info = json.loads(_MARKER.read_text(encoding="utf-8"))
                owner = info.get("pid")
                if owner is not None and int(owner) != os.getpid():
                    log.debug("Crash marker owned by pid %s, not ours — leaving it", owner)
                    return
            except Exception:
                pass   # corrupt/unreadable → fall through and remove
        _MARKER.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear crash marker: %s", exc)
