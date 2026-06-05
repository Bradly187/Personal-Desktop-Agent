"""desktop.target_cache — background cache of clickable UI targets.

A single daemon thread owns a UIAutomation COM instance and re-enumerates the
foreground window's clickable elements, publishing a lock-protected snapshot.
Both "magnetic cursor" features read from this snapshot so they never block on a
per-event UIA tree walk:

  * Magnetic click  (command_executor._magnetic_snap) — snap a tilt-tap to the
    nearest target.
  * Cursor gravity   (FusionEngine, 60 Hz tick)        — nudge the cursor toward
    a nearby target while moving.

The walk is *change-gated*, not a blind poll: each wake it reads the foreground
window handle (cheap, no COM) and only re-enumerates when that handle changed or
a slow heartbeat elapsed. A consecutive-failure backoff lengthens the interval
when an app misbehaves, so a single bad window can't thrash the COM walk (the
"E_POINTER 3x/s" failure mode that destabilised an earlier version). The walk
itself is scoped to the foreground window via ElementFromHandle rather than
walking up from the focused element while the tree mutates.

COM is apartment-bound, so the cache keeps all UIAutomation calls on its own
thread (CoInitialized there, CoUninitialized on exit) and hands out only plain
Python data. Degrades to an empty snapshot when comtypes / UIAutomation is
unavailable — every consumer treats "no targets" as "no snapping", i.e. the
pre-existing behaviour.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    """A lightweight, thread-safe snapshot of one clickable element."""
    name:   str
    role:   str
    bounds: tuple[int, int, int, int]   # (left, top, right, bottom)

    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return ((l + r) // 2, (t + b) // 2)

    def area(self) -> int:
        l, t, r, b = self.bounds
        return max(0, r - l) * max(0, b - t)

    def distance_to(self, x: int, y: int) -> float:
        l, t, r, b = self.bounds
        dx = max(l - x, 0, x - r)
        dy = max(t - y, 0, y - b)
        return (dx * dx + dy * dy) ** 0.5


class ClickableTargetCache:
    """Polls clickable UI targets on a daemon thread; serves cheap nearest()."""

    def __init__(
        self,
        poll_hz: float = 4.0,
        heartbeat_s: float = 1.5,
        max_targets: int = 200,
        max_backoff_s: float = 2.0,
    ) -> None:
        # Wake cadence — cheap (just reads the foreground hwnd). The actual COM
        # walk only happens on a foreground change or once per heartbeat.
        self._poll_interval = 1.0 / max(1.0, poll_hz)
        self._heartbeat_s = heartbeat_s
        self._max_targets = max_targets
        self._max_backoff_s = max_backoff_s
        self._targets: list[Target] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._available: bool | None = None   # None until first refresh attempt

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="target-cache", daemon=True
        )
        self._thread.start()
        log.info(
            "ClickableTargetCache: started (poll %.1f Hz, heartbeat %.1fs)",
            1.0 / self._poll_interval, self._heartbeat_s,
        )

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running and self._available is not False

    # ── worker thread ───────────────────────────────────────────────────────

    @staticmethod
    def _foreground_hwnd() -> int:
        """Foreground window handle (0 if unavailable). Cheap — no COM."""
        try:
            return int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            return 0

    def _walk_due(
        self, hwnd: int, last_hwnd: int, now: float, last_walk: float
    ) -> bool:
        """Walk only on a foreground change or once per heartbeat.

        A blind poll every tick was what thrashed the COM tree before; gating on
        the (cheap) foreground handle plus a slow heartbeat keeps the walk rare.
        """
        return hwnd != last_hwnd or (now - last_walk) >= self._heartbeat_s

    def _loop(self) -> None:
        # UIAutomation prefers MTA; CoInitialize this thread explicitly so the
        # COM object lives and is called on the same apartment. Balanced with a
        # CoUninitialize in the finally so the apartment is torn down cleanly.
        com_inited = False
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            com_inited = True
        except Exception as exc:   # pragma: no cover - platform dependent
            log.debug("CoInitializeEx (MTA) unavailable: %s", exc)

        try:
            try:
                from desktop.ui_automation import UIAutomationProvider
                provider = UIAutomationProvider()
            except Exception as exc:
                log.warning("ClickableTargetCache: provider import failed — %s", exc)
                self._available = False
                self._running = False
                return

            if not provider.is_available():
                log.warning("ClickableTargetCache: UIAutomation unavailable — "
                            "magnetic features will no-op")
                self._available = False
                self._running = False
                return

            self._available = True
            last_hwnd = -1            # force a walk on the first iteration
            last_walk = 0.0
            backoff = 0.0             # extra sleep added after consecutive failures

            while self._running:
                now = time.monotonic()
                hwnd = self._foreground_hwnd()
                if self._walk_due(hwnd, last_hwnd, now, last_walk):
                    last_hwnd = hwnd
                    last_walk = now
                    try:
                        elems = provider.collect_snap_targets_for_window(
                            hwnd, self._max_targets
                        )
                        snapshot = [
                            Target(name=e.name, role=e.role, bounds=e.bounds)
                            for e in elems
                        ]
                        with self._lock:
                            self._targets = snapshot
                        backoff = 0.0   # success — clear any backoff
                    except Exception as exc:
                        # Grow the backoff so a persistently bad window can't
                        # thrash the walk (E_POINTER storm); cap it.
                        backoff = min(self._max_backoff_s,
                                      max(self._poll_interval, backoff * 2))
                        log.debug(
                            "ClickableTargetCache refresh failed (backoff %.2fs): %s",
                            backoff, exc,
                        )
                time.sleep(self._poll_interval + backoff)
        finally:
            self._running = False
            if com_inited:
                try:
                    import comtypes
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    # ── read API (called from any thread) ───────────────────────────────────

    def snapshot(self) -> list[Target]:
        with self._lock:
            return list(self._targets)

    def count(self) -> int:
        with self._lock:
            return len(self._targets)

    def nearest(self, x: int, y: int, radius: float) -> Target | None:
        """Nearest snap target within `radius` px of (x, y), or None.

        Distance is to the element's rectangle (0 if inside). Ties (e.g. nested
        controls both containing the point) break toward the smaller, more
        specific element. Pure Python over the cached snapshot — safe to call at
        60 Hz from the FusionEngine tick.
        """
        best: Target | None = None
        best_d = float(radius) + 1.0
        best_area = float("inf")
        with self._lock:
            targets = self._targets
            for tg in targets:
                d = tg.distance_to(x, y)
                if d > radius:
                    continue
                a = tg.area()
                if d < best_d or (d == best_d and a < best_area):
                    best_d, best_area, best = d, a, tg
        return best


# ── module-level singleton ──────────────────────────────────────────────────

_cache: ClickableTargetCache | None = None


def get_target_cache() -> ClickableTargetCache:
    """Return the process-wide cache (created lazily, not started)."""
    global _cache
    if _cache is None:
        _cache = ClickableTargetCache()
    return _cache
