"""desktop.target_cache — background cache of clickable UI targets.

A single daemon thread owns a UIAutomation COM instance and re-enumerates the
focused window's clickable elements a few times a second, publishing a
lock-protected snapshot. All three "magnetic cursor" features read from this
snapshot so they never block on a per-event UIA tree walk:

  * Magnetic click  (command_executor._magnetic_snap) — snap a tilt-tap to the
    nearest target.
  * Highlight overlay (desktop.magnetic_overlay)       — draw the target that a
    tap would hit.
  * Cursor gravity   (FusionEngine, 60 Hz tick)        — nudge the cursor toward
    a nearby target while moving.

COM is apartment-bound, so the cache keeps all UIAutomation calls on its own
thread (CoInitialized there) and hands out only plain Python data. Degrades to
an empty snapshot when comtypes / UIAutomation is unavailable — every consumer
treats "no targets" as "no snapping", i.e. the pre-existing behaviour.
"""

from __future__ import annotations

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

    def __init__(self, refresh_hz: float = 3.0, max_targets: int = 300) -> None:
        self._interval = 1.0 / max(0.5, refresh_hz)
        self._max_targets = max_targets
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
        log.info("ClickableTargetCache: started (%.1f Hz)", 1.0 / self._interval)

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running and self._available is not False

    # ── worker thread ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        # UIAutomation prefers MTA; CoInitialize this thread explicitly so the
        # COM object lives and is called on the same apartment.
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception as exc:   # pragma: no cover - platform dependent
            log.debug("CoInitializeEx (MTA) unavailable: %s", exc)

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
            # Keep the thread alive but idle in case COM becomes available later
            # is pointless; just exit.
            self._running = False
            return

        self._available = True
        while self._running:
            t0 = time.monotonic()
            try:
                elems = provider.collect_snap_targets(self._max_targets)
                snapshot = [
                    Target(name=e.name, role=e.role, bounds=e.bounds)
                    for e in elems
                ]
                with self._lock:
                    self._targets = snapshot
            except Exception as exc:
                log.debug("ClickableTargetCache refresh failed: %s", exc)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, self._interval - elapsed))

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
