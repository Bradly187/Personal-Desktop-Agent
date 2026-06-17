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
        hwnd_source=None,
    ) -> None:
        # Which window to enumerate each refresh. Default None → the foreground
        # (focused) window — correct for the tilt/touch flow where the focused
        # window IS the target. A free-roaming pointer (RealSense hand cursor)
        # passes a callable returning the window UNDER THE CURSOR, so gravity
        # snaps to whatever window the cursor is hovering (incl. its title-bar
        # min/max/close), not the focused one.
        self._hwnd_source = hwnd_source
        # Wake cadence — cheap (just reads the foreground hwnd). The actual COM
        # walk only happens on a foreground change or once per heartbeat.
        self._poll_interval = 1.0 / max(1.0, poll_hz)
        self._heartbeat_s = heartbeat_s
        self._max_targets = max_targets
        self._max_backoff_s = max_backoff_s
        # Reads are lock-free: the worker only ever *rebinds* self._targets to a
        # fresh list (never mutates one in place) and Target is frozen, so a
        # reader grabbing the reference then iterating can never see a partial
        # snapshot under the GIL. This keeps the 60 Hz FusionEngine tick from
        # ever blocking on a lock held by the worker thread.
        self._targets: list[Target] = []
        self._thread: threading.Thread | None = None
        self._running = False
        self._available: bool | None = None   # None until first refresh attempt
        # Set by stop() to wake the worker immediately instead of waiting out the
        # current poll/backoff sleep — makes teardown prompt rather than lagging
        # up to (poll_interval + max_backoff_s).
        self._stop_event = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
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
        self._stop_event.set()   # wake the worker out of its sleep at once

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

    def _current_hwnd(self) -> int:
        """The window to enumerate this refresh: the injected source (e.g.
        window-under-cursor) if provided, else the foreground window."""
        if self._hwnd_source is not None:
            try:
                return int(self._hwnd_source() or 0)
            except Exception:
                return 0
        return self._foreground_hwnd()

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

            while not self._stop_event.is_set():
                now = time.monotonic()
                hwnd = self._current_hwnd()
                if self._walk_due(hwnd, last_hwnd, now, last_walk):
                    last_hwnd = hwnd
                    last_walk = now
                    try:
                        elems = provider.collect_snap_targets_for_window(
                            hwnd, self._max_targets
                        )
                        # Build outside any lock, then publish with a single
                        # atomic rebind — readers grab the reference lock-free.
                        self._targets = [
                            Target(name=e.name, role=e.role, bounds=e.bounds)
                            for e in elems
                        ]
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
                # Event.wait returns at once when stop() fires, so teardown is
                # prompt instead of waiting out the full poll/backoff interval.
                self._stop_event.wait(self._poll_interval + backoff)
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
        return list(self._targets)   # grab ref + copy; rebind is atomic

    def count(self) -> int:
        return len(self._targets)

    def nearest(self, x: int, y: int, radius: float,
                max_dim: float | None = None) -> Target | None:
        """Nearest snap target within `radius` px of (x, y), or None.

        Distance is to the element's rectangle (0 if inside). Ties (e.g. nested
        controls both containing the point) break toward the smaller, more
        specific element. `max_dim` (optional) skips targets whose width OR
        height exceeds it — used by cursor gravity to stick only to compact
        controls (buttons, caption min/max/close, toolbar icons) and ignore
        large regions / long hyperlinks, so it assists the hard small targets
        without grabbing everything. Pure Python over the cached snapshot — safe
        to call at 60 Hz. Lock-free: grab the snapshot reference once (atomic
        rebind) and iterate it; the worker only ever swaps in a new list.
        """
        best: Target | None = None
        best_d = float(radius) + 1.0
        best_area = float("inf")
        targets = self._targets   # atomic reference grab
        for tg in targets:
            if max_dim is not None:
                l, t, r, b = tg.bounds
                if (r - l) > max_dim or (b - t) > max_dim:
                    continue
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
