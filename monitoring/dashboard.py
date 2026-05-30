"""monitoring.dashboard.py — Live TUI metrics dashboard for the Personal Desktop Agent.

Displays a real-time terminal UI showing:
  - Gate distribution (sparkline-style bar chart)
  - End-to-end latency (EMA + histogram buckets)
  - Per-source success ratios
  - Pain day gauge
  - Cloud escalation rate
  - Last 10 commands feed

Usage:
    # Standalone (polls GET /metrics from a running agent):
    python dashboard.py [--url http://localhost:8080/metrics] [--interval 1.0]

    # In-process (pass the Metrics singleton directly):
    from monitoring.dashboard import Dashboard
    from monitoring.metrics import get_metrics
    dash = Dashboard(metrics=get_metrics())
    await dash.start()          # launches background refresh loop
    dash.stop()                 # tear down before process exit

Requires: curses (stdlib on Linux/macOS; Windows ships it via windows-curses)

Graceful degradation:
  - If curses is unavailable, falls back to periodic plain-text log output.
  - If the /metrics endpoint is unreachable, shows "DISCONNECTED" and retries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from monitoring.metrics import Metrics

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_REFRESH_HZ = 1          # screen redraws per second
_SPARK_WIDTH = 20        # width of sparkline history
_LATENCY_BAR_MAX = 50    # reference scale for latency bar (ms)
_RECENT_CMD_ROWS = 10    # commands shown in the feed

# Bar characters (ASCII-safe)
_BAR_FULL = "█"
_BAR_MED  = "▓"
_BAR_LOW  = "░"
_BAR_EMPTY = " "

# ---------------------------------------------------------------------------
# Colour indices (curses pair numbers)
# ---------------------------------------------------------------------------
_COL_HEADER  = 1   # cyan on black
_COL_OK      = 2   # green on black
_COL_WARN    = 3   # yellow on black
_COL_ERR     = 4   # red on black
_COL_DIM     = 5   # white on black (dim)
_COL_PAIN    = 6   # magenta on black
_COL_CLOUD   = 7   # blue on black


# ---------------------------------------------------------------------------
# SnapshotFetcher — gets metrics from either a live Metrics object or HTTP
# ---------------------------------------------------------------------------

class SnapshotFetcher:
    """Fetch a metrics snapshot from either a live object or an HTTP endpoint."""

    def __init__(
        self,
        metrics: Optional["Metrics"] = None,
        url: str = "http://localhost:8080/metrics",
    ) -> None:
        self._metrics = metrics
        self._url = url
        self._last: Optional[dict] = None
        self._error: Optional[str] = None

    def fetch(self) -> Optional[dict]:
        """Return the latest snapshot dict (may return cached on error)."""
        if self._metrics is not None:
            try:
                self._last = self._metrics.get_snapshot()
                self._error = None
            except Exception as exc:
                self._error = str(exc)
        else:
            try:
                with urllib.request.urlopen(self._url, timeout=2) as resp:
                    self._last = json.loads(resp.read())
                    self._error = None
            except urllib.error.URLError as exc:
                self._error = f"DISCONNECTED: {exc.reason}"
            except Exception as exc:
                self._error = str(exc)
        return self._last

    @property
    def error(self) -> Optional[str]:
        return self._error


# ---------------------------------------------------------------------------
# Text formatters
# ---------------------------------------------------------------------------

def _bar(fraction: float, width: int = 20) -> str:
    """Render a [0.0, 1.0] fraction as a filled bar."""
    fraction = max(0.0, min(1.0, fraction or 0.0))
    filled = round(fraction * width)
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def _pct(v) -> str:
    if v is None:
        return "  N/A"
    return f"{v * 100:5.1f}%"


def _ms(v) -> str:
    if v is None:
        return "   N/A"
    return f"{v:6.1f}ms"


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _sparkline(history: list[float], width: int = _SPARK_WIDTH) -> str:
    """Render a list of [0,1] values as a unicode sparkline."""
    _BLOCKS = " ▁▂▃▄▅▆▇█"
    if not history:
        return " " * width
    window = history[-width:]
    result = []
    for v in window:
        idx = int((v or 0.0) * (len(_BLOCKS) - 1))
        result.append(_BLOCKS[max(0, min(idx, len(_BLOCKS) - 1))])
    # Pad left if shorter than width
    return " " * (width - len(result)) + "".join(result)


# ---------------------------------------------------------------------------
# CursesRenderer
# ---------------------------------------------------------------------------

class CursesRenderer:
    """Renders metrics onto a curses window."""

    def __init__(self, stdscr) -> None:
        self._scr = stdscr
        self._latency_history: list[float] = []   # normalised [0,1] EMAs
        self._cloud_history:   list[float] = []   # cloud_rate_1m history
        self._init_colours()

    def _init_colours(self) -> None:
        import curses
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(_COL_HEADER, curses.COLOR_CYAN,    -1)
        curses.init_pair(_COL_OK,     curses.COLOR_GREEN,   -1)
        curses.init_pair(_COL_WARN,   curses.COLOR_YELLOW,  -1)
        curses.init_pair(_COL_ERR,    curses.COLOR_RED,     -1)
        curses.init_pair(_COL_DIM,    curses.COLOR_WHITE,   -1)
        curses.init_pair(_COL_PAIN,   curses.COLOR_MAGENTA, -1)
        curses.init_pair(_COL_CLOUD,  curses.COLOR_BLUE,    -1)

    def _attr(self, pair: int):
        import curses
        try:
            return curses.color_pair(pair)
        except Exception:
            return 0

    def _addstr(self, row: int, col: int, text: str, attr=0) -> None:
        try:
            self._scr.addstr(row, col, text, attr)
        except Exception:
            pass   # ignore writes outside window bounds

    def render(self, snap: dict, error: Optional[str]) -> None:
        import curses
        scr = self._scr
        scr.erase()
        rows, cols = scr.getmaxyx()
        row = 0

        # ── Title bar ──────────────────────────────────────────────────────
        uptime = snap.get("uptime_s", 0) if snap else 0
        mins, secs = divmod(int(uptime), 60)
        hrs, mins = divmod(mins, 60)
        title = f" Desktop Agent  up {hrs:02d}:{mins:02d}:{secs:02d}  {_fmt_ts(time.time())} "
        self._addstr(row, 0, title.ljust(cols), self._attr(_COL_HEADER) | curses.A_BOLD)
        row += 1

        if error:
            self._addstr(row, 2, error, self._attr(_COL_ERR))
            row += 1

        if not snap:
            scr.refresh()
            return

        counters = snap.get("counters", {})
        gauges   = snap.get("gauges",   {})
        hists    = snap.get("histograms", {})
        sources  = snap.get("source_counts", {})
        domains  = snap.get("domain_counts", {})
        recent   = snap.get("recent_commands", [])

        total = counters.get("commands_total", 0) or 1   # avoid div/0

        # ── Command counters ───────────────────────────────────────────────
        row += 1
        self._addstr(row, 2, "COMMANDS", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        row += 1
        success  = counters.get("commands_success", 0)
        failed   = counters.get("commands_failed", 0)
        clarify  = counters.get("commands_clarify", 0)
        cloud_n  = counters.get("cloud_escalations", 0)
        s_rate   = gauges.get("success_rate_1m")
        c_rate   = gauges.get("cloud_rate_1m")
        self._addstr(row, 4,
            f"Total: {total:5d}   Success: {success:5d} ({_pct(s_rate)})   "
            f"Failed: {failed:4d}   Clarify: {clarify:4d}   Cloud: {cloud_n:4d} ({_pct(c_rate)})",
        )
        row += 1

        # ── Gate distribution ──────────────────────────────────────────────
        row += 1
        self._addstr(row, 2, "GATE DISTRIBUTION", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        row += 1
        gate_names = [
            ("gate0_blocks", "Gate 0 Privacy"),
            ("gate1_discards", "Gate 1 Confidence"),
            ("gate2_cloud", "Gate 2 Complexity"),
            ("gate3_cloud", "Gate 3 VRAM"),
            ("gate4_cloud", "Gate 4 Latency"),
        ]
        for key, label in gate_names:
            n = counters.get(key, 0)
            frac = n / total
            bar = _bar(frac, 12)
            self._addstr(row, 4, f"{label:<20} {bar} {n:5d} ({frac*100:4.1f}%)")
            row += 1

        # ── Latency ────────────────────────────────────────────────────────
        row += 1
        self._addstr(row, 2, "LATENCY", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        row += 1
        lat_ema = gauges.get("latency_ema_ms")
        lat_hist = hists.get("latency_ms", {})
        p50 = lat_hist.get("p50")
        p95 = lat_hist.get("p95")
        lat_count = lat_hist.get("count", 0)

        # Update sparkline history (normalise against 1000ms scale)
        if lat_ema is not None:
            self._latency_history.append(min(1.0, lat_ema / 1000.0))
            if len(self._latency_history) > _SPARK_WIDTH:
                self._latency_history = self._latency_history[-_SPARK_WIDTH:]

        spark = _sparkline(self._latency_history)
        ema_attr = (
            self._attr(_COL_OK)   if (lat_ema or 9999) < 300 else
            self._attr(_COL_WARN) if (lat_ema or 9999) < 700 else
            self._attr(_COL_ERR)
        )
        self._addstr(row, 4, f"EMA: ")
        self._addstr(row, 9, _ms(lat_ema), ema_attr)
        self._addstr(row, 19, f"  p50: {_ms(p50)}  p95: {_ms(p95)}  n={lat_count}")
        row += 1
        self._addstr(row, 4, f"Trend: {spark}")
        row += 1

        # ── Per-source success rates ───────────────────────────────────────
        row += 1
        self._addstr(row, 2, "SOURCE BREAKDOWN", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        row += 1
        all_sources = sorted(sources.items(), key=lambda x: -x[1])
        for src, n in all_sources[:6]:
            frac = n / total
            bar = _bar(frac, 10)
            self._addstr(row, 4, f"{src:<12} {bar} {n:5d} ({frac*100:4.1f}%)")
            row += 1

        # ── Pain day gauge ─────────────────────────────────────────────────
        row += 1
        pain = gauges.get("pain_day_score", 0.0) or 0.0
        pain_attr = (
            self._attr(_COL_PAIN) | curses.A_BOLD if pain > 0.6 else
            self._attr(_COL_WARN)                  if pain > 0.3 else
            self._attr(_COL_OK)
        )
        pain_bar = _bar(pain, 20)
        self._addstr(row, 2, "PAIN DAY  ", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        self._addstr(row, 12, f"{pain_bar} {pain:.2f}", pain_attr)
        row += 1

        # ── Voice quality ──────────────────────────────────────────────────
        logprob_ema = gauges.get("whisper_logprob_ema")
        gesture_ema = gauges.get("gesture_conf_ema")
        vram = gauges.get("vram_free_gb")
        row += 1
        self._addstr(row, 2, "SENSORS", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        row += 1
        self._addstr(row, 4,
            f"Whisper logprob EMA: {logprob_ema:.3f}" if logprob_ema is not None
            else "Whisper logprob EMA: N/A"
        )
        row += 1
        self._addstr(row, 4,
            f"Gesture conf EMA:    {gesture_ema:.3f}" if gesture_ema is not None
            else "Gesture conf EMA:    N/A"
        )
        row += 1
        vram_attr = self._attr(_COL_WARN) if (vram or 99) < 4 else self._attr(_COL_OK)
        self._addstr(row, 4, "VRAM free: ")
        self._addstr(row, 15, f"{vram:.2f} GB" if vram is not None else "N/A", vram_attr)
        row += 1

        # ── Recent commands feed ───────────────────────────────────────────
        row += 1
        self._addstr(row, 2, "RECENT COMMANDS", self._attr(_COL_HEADER) | curses.A_UNDERLINE)
        row += 1
        feed = list(reversed(recent))[:_RECENT_CMD_ROWS]
        for cmd in feed:
            if row >= rows - 2:
                break
            ts_str = _fmt_ts(cmd.get("ts", 0))
            action  = (cmd.get("action") or "?")[:12]
            route   = (cmd.get("route") or "?")[:5]
            domain  = (cmd.get("domain") or "?")[:8]
            lat     = cmd.get("latency_ms", 0)
            ok      = cmd.get("success", True)
            gate    = cmd.get("gate") or ""
            ok_attr = self._attr(_COL_OK) if ok else self._attr(_COL_ERR)
            self._addstr(row, 4,
                f"{ts_str}  {action:<12} {route:<5} {domain:<8} {lat:6.0f}ms",
            )
            status_ch = "✓" if ok else "✗"
            self._addstr(row, 4 + 44, status_ch, ok_attr)
            if gate:
                self._addstr(row, 4 + 46, gate[:20], self._attr(_COL_DIM))
            row += 1

        # ── Footer ─────────────────────────────────────────────────────────
        footer = " q/Ctrl-C to quit "
        self._addstr(rows - 1, 0, footer.ljust(cols), self._attr(_COL_DIM))

        scr.refresh()


# ---------------------------------------------------------------------------
# PlainTextRenderer — fallback when curses is unavailable
# ---------------------------------------------------------------------------

class PlainTextRenderer:
    """Fallback renderer that logs metrics to stdout/logging."""

    def render(self, snap: dict, error: Optional[str]) -> None:
        if error:
            log.warning("Dashboard: %s", error)
            return
        if not snap:
            return
        c = snap.get("counters", {})
        g = snap.get("gauges", {})
        h = snap.get("histograms", {}).get("latency_ms", {})
        log.info(
            "Dashboard | total=%d ok=%d fail=%d cloud=%d | "
            "lat_ema=%s p50=%s p95=%s | pain=%.2f | vram=%s",
            c.get("commands_total", 0),
            c.get("commands_success", 0),
            c.get("commands_failed", 0),
            c.get("cloud_escalations", 0),
            f"{g.get('latency_ema_ms', 0):.0f}ms",
            f"{h.get('p50', 0):.0f}ms",
            f"{h.get('p95', 0):.0f}ms",
            g.get("pain_day_score", 0.0) or 0.0,
            f"{g.get('vram_free_gb', 0):.1f}GB" if g.get("vram_free_gb") else "N/A",
        )


# ---------------------------------------------------------------------------
# Dashboard — async controller
# ---------------------------------------------------------------------------

class Dashboard:
    """Async live TUI controller.

    In-process usage:
        dash = Dashboard(metrics=get_metrics())
        await dash.start()   # spawns background refresh task
        ...
        dash.stop()
    """

    def __init__(
        self,
        metrics: Optional["Metrics"] = None,
        url: str = "http://localhost:8080/metrics",
        interval: float = 1.0,
    ) -> None:
        self._fetcher = SnapshotFetcher(metrics=metrics, url=url)
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._use_curses: Optional[bool] = None  # determined at start()

    async def start(self) -> None:
        """Start the background refresh loop (in-process mode)."""
        self._running = True
        self._task = asyncio.create_task(
            self._log_loop(), name="dashboard_log_loop"
        )
        log.info("Dashboard: started (plain-text log mode)")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _log_loop(self) -> None:
        """Background task: fetch + log at `interval` Hz."""
        renderer = PlainTextRenderer()
        while self._running:
            try:
                snap = self._fetcher.fetch()
                renderer.render(snap, self._fetcher.error)
            except Exception as exc:
                log.warning("Dashboard: render error: %s", exc)
            await asyncio.sleep(self._interval)


# ---------------------------------------------------------------------------
# Interactive curses runner (for standalone use)
# ---------------------------------------------------------------------------

def _run_curses(fetcher: SnapshotFetcher, interval: float) -> None:
    """Run the full interactive curses dashboard (blocks until q/Ctrl-C)."""
    import curses

    def _main(stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)   # non-blocking getch
        stdscr.timeout(int(interval * 1000))
        renderer = CursesRenderer(stdscr)

        while True:
            snap = fetcher.fetch()
            renderer.render(snap, fetcher.error)

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 3):   # 3 = Ctrl-C
                break

    curses.wrapper(_main)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI: python dashboard.py [--url URL] [--interval SECS]"""
    import argparse

    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="Personal Desktop Agent — live dashboard")
    parser.add_argument("--url", default="http://localhost:8080/metrics",
                        help="Metrics endpoint URL (default: http://localhost:8080/metrics)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Refresh interval in seconds (default: 1.0)")
    parser.add_argument("--plain", action="store_true",
                        help="Force plain-text output (no curses TUI)")
    args = parser.parse_args()

    fetcher = SnapshotFetcher(url=args.url)

    if args.plain:
        renderer = PlainTextRenderer()
        logging.basicConfig(level=logging.INFO)
        print(f"Dashboard polling {args.url} every {args.interval}s  (Ctrl-C to stop)")
        try:
            while True:
                snap = fetcher.fetch()
                renderer.render(snap, fetcher.error)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        return

    # Try curses
    try:
        import curses
        print(f"Starting live dashboard (q or Ctrl-C to quit) …")
        _run_curses(fetcher, args.interval)
    except ImportError:
        print("curses not available — falling back to plain text (pip install windows-curses)")
        renderer = PlainTextRenderer()
        logging.basicConfig(level=logging.INFO)
        try:
            while True:
                snap = fetcher.fetch()
                renderer.render(snap, fetcher.error)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    except Exception as exc:
        print(f"Dashboard error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
