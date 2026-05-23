"""Personal Desktop Agent — main entry point.

Starts the full PC-side pipeline:
  iPad WebSocket bridge → FusionEngine → HybridCoordinator → CommandExecutor

Usage:
    python main.py [--port 8765] [--no-mdns] [--debug]
    python main.py --measure-vram        # task 4.2 — print VRAM table and exit
    python main.py --safe-mode           # set SAFE_MODE=1 for MCP server

Ctrl-C triggers graceful shutdown (task 4.3):
  flushes agent.db writes, stops all components.

Startup status table (task 4.4) is printed before the bridge starts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("desktop_agent")


# ---------------------------------------------------------------------------
# Task 4.2 — VRAM measurement
# ---------------------------------------------------------------------------

def _measure_vram() -> None:
    """Load all models and print a VRAM usage table, then exit.

    Updates task 1.2: run this with all models to get measured Blackwell values.
    """
    print("\n=== VRAM Measurement (RTX 5090) ===\n")

    def _nvml_snapshot(label: str) -> None:
        try:
            import pynvml as nvml
            nvml.nvmlInit()
            handle = nvml.nvmlDeviceGetHandleByIndex(0)
            info = nvml.nvmlDeviceGetMemoryInfo(handle)
            used_gb = info.used / (1024 ** 3)
            free_gb = info.free / (1024 ** 3)
            total_gb = info.total / (1024 ** 3)
            nvml.nvmlShutdown()
            print(f"  {label:<40}  used={used_gb:.1f} GB  free={free_gb:.1f} GB  total={total_gb:.1f} GB")
        except Exception as exc:
            print(f"  {label:<40}  [NVML error: {exc}]")

    _nvml_snapshot("Baseline (no models loaded)")

    # --- Whisper large-v3 ---
    print("\n  Loading Whisper large-v3 ...")
    try:
        from faster_whisper import WhisperModel
        _ = WhisperModel("large-v3", device="cuda", compute_type="float16")
        _nvml_snapshot("After Whisper large-v3")
    except ImportError:
        print("  [SKIP] faster-whisper not installed — run: pip install faster-whisper")
    except Exception as exc:
        print(f"  [FAIL] Whisper: {exc}")

    # --- YOLOv8-pose ---
    print("\n  Loading YOLOv8-pose ...")
    try:
        from ultralytics import YOLO
        _ = YOLO("yolov8n-pose.pt")  # nano variant — downloads if absent
        _nvml_snapshot("After YOLOv8-pose")
    except ImportError:
        print("  [SKIP] ultralytics not installed — run: pip install ultralytics")
    except Exception as exc:
        print(f"  [FAIL] YOLOv8: {exc}")

    # --- Ollama — use largest locally-available model ---
    print("\n  Querying Ollama for available models ...")
    try:
        import urllib.request, json as _json

        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            tags = _json.loads(r.read())
        models = tags.get("models", [])
        if not models:
            raise RuntimeError("No models pulled — run: ollama pull llama3.1:70b")

        # Sort by size (largest first); skip cloud/remote entries with size=0
        models_local = [m for m in models if m.get("size", 0) > 0]
        if not models_local:
            raise RuntimeError("No local models found")
        models_local.sort(key=lambda m: m["size"], reverse=True)
        chosen = models_local[0]["name"]
        size_gb = models_local[0]["size"] / (1024 ** 3)
        print(f"  Largest available model: {chosen} ({size_gb:.1f} GB)")

        print(f"  Triggering {chosen} load (single token) ...")
        body = _json.dumps({
            "model": chosen,
            "prompt": "hi",
            "stream": False,
            "options": {"num_predict": 1},
        }).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            _json.loads(resp.read())
        _nvml_snapshot(f"After Ollama {chosen}")
        print(f"\n  NOTE: llama3.1:70b not pulled. For full task-1.2 measurement,")
        print(f"  run: ollama pull llama3.1:70b  then re-run --measure-vram")
    except Exception as exc:
        print(f"  [FAIL/SKIP] Ollama: {exc}")

    print("\n  All done. Update the VRAM budget tables with the values above.")
    print("  Files to update:")
    print("    .kiro/specs/ipad-sensor-focus/diagrams/05-data-flow.md")
    print("    .kiro/specs/ipad-sensor-focus/local-inference-comparison.md\n")


# ---------------------------------------------------------------------------
# Task 4.4 — Startup status table
# ---------------------------------------------------------------------------

def _print_startup_table(port: int, safe_mode: bool, host: str = "0.0.0.0") -> None:
    """Print a table of which PC-side services are available."""
    rows: list[tuple[str, str, str]] = []

    def check(name: str, fn) -> None:
        try:
            status, note = fn()
        except Exception as exc:
            status, note = "ERROR", str(exc)[:60]
        rows.append((name, status, note))

    def _check_pynvml():
        import pynvml as nvml
        nvml.nvmlInit()
        h = nvml.nvmlDeviceGetHandleByIndex(0)
        info = nvml.nvmlDeviceGetMemoryInfo(h)
        name_b = nvml.nvmlDeviceGetName(h)
        gpu_name = name_b.decode() if isinstance(name_b, bytes) else name_b
        nvml.nvmlShutdown()
        free_gb = info.free / (1024 ** 3)
        return "OK", f"{gpu_name}  {free_gb:.1f} GB free"

    def _check_ollama():
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            data = __import__("json").loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        return "OK", f"{len(models)} model(s) pulled"

    def _check_tesseract():
        import pytesseract
        v = pytesseract.get_tesseract_version()
        return "OK", f"v{v}"

    def _check_pix2tex():
        from pix2tex.cli import LatexOCR  # noqa: F401
        return "OK", "pix2tex available (model loads on first use)"

    def _check_mdns():
        from zeroconf import Zeroconf  # noqa: F401
        return "OK", "mDNS discovery enabled"

    def _check_whisper():
        from faster_whisper import WhisperModel  # noqa: F401
        return "OK", "faster-whisper available"

    def _check_safe_mode():
        return ("ACTIVE", "keyboard_type and mouse_drag blocked") if safe_mode else ("off", "")

    def _check_mediapipe():
        import mediapipe  # noqa: F401
        import cv2  # noqa: F401
        return "OK", "gesture recognition ready"

    def _check_numpy():
        import numpy as np
        return "OK", f"v{np.__version__}  (LiDAR depth maps)"

    def _check_sentence_transformers():
        from sentence_transformers import SentenceTransformer  # noqa: F401
        return "OK", "MiniLM available (loads on first few-shot query)"

    def _check_acoustic_profiler():
        from acoustic_profiler import AcousticProfiler  # noqa: F401
        return "OK", "acoustic profiler available"

    def _check_uiautomation():
        from ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        avail = p.is_available()
        return ("OK", "UIA COM available") if avail else ("WARN", "UIA COM unavailable (comtypes?)")

    def _check_gaze_calibration():
        from gaze_calibrator import GazeCalibrator, _JSON_PATH
        if not _JSON_PATH.exists():
            return "WARN", "not calibrated — say 'hey agent calibrate monitor'"
        cal = GazeCalibrator()
        if cal.load():
            status = cal.get_status()
            residual = status["residual_px"]
            calibrated_at = status["calibrated_at"]
            if calibrated_at > 0:
                import time as _t
                age_days = (_t.time() - calibrated_at) / 86400
                return "OK", f"residual={residual:.1f}px  age={age_days:.0f}d"
            return "OK", f"residual={residual:.1f}px  age=unknown"
        return "WARN", "calibration file unreadable"

    check("GPU / VRAM (pynvml)",            _check_pynvml)
    check("Ollama LLM server",              _check_ollama)
    check("Whisper (faster-whisper)",       _check_whisper)
    check("Acoustic profiler",              _check_acoustic_profiler)
    check("UIAutomation (Win32)",           _check_uiautomation)
    check("Gaze monitor calibration",      _check_gaze_calibration)
    check("MiniLM (sentence-transformers)", _check_sentence_transformers)
    check("Screen OCR (tesseract)",         _check_tesseract)
    check("Handwriting OCR (pix2tex)",      _check_pix2tex)
    check("Gesture (mediapipe+opencv)",     _check_mediapipe)
    check("LiDAR depth (numpy)",            _check_numpy)
    check("mDNS discovery (zeroconf)",      _check_mdns)
    check("SAFE_MODE",                      _check_safe_mode)

    w_name = max(len(r[0]) for r in rows) + 2
    w_status = max(len(r[1]) for r in rows) + 2

    print()
    print(f"  {'Service':<{w_name}}  {'Status':<{w_status}}  Notes")
    print(f"  {'-' * w_name}  {'-' * w_status}  {'-' * 40}")
    for name, status, note in rows:
        ok = status in ("OK", "off")
        marker = " " if ok else "!"
        print(f"  [{marker}] {name:<{w_name - 4}}  {status:<{w_status}}  {note}")

    print()
    print(f"  Bridge: ws://{host}:{port}/ws")
    print()


# ---------------------------------------------------------------------------
# Task 4.3 — Graceful shutdown
# ---------------------------------------------------------------------------

class _ShutdownController:
    """Coordinates graceful shutdown on SIGINT / Ctrl-C."""

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._components: list = []  # objects with async stop() or stop()

    def register(self, *components) -> None:
        self._components.extend(components)

    def arm(self) -> None:
        """Install signal handler — must be called from the running event loop."""
        import sys as _sys
        loop = asyncio.get_running_loop()
        if _sys.platform == "win32":
            # Windows doesn't support loop.add_signal_handler; use signal module directly
            signal.signal(signal.SIGINT, lambda *_: self._stop_event.set())
            signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
        else:
            loop.add_signal_handler(signal.SIGINT, self._handle_sigint)
            loop.add_signal_handler(signal.SIGTERM, self._handle_sigint)

    def _handle_sigint(self) -> None:
        log.info("Shutdown signal received — stopping gracefully ...")
        self._stop_event.set()

    async def wait_for_shutdown(self) -> None:
        # Poll every 200ms so the Windows signal handler (set via signal.signal)
        # gets a chance to run — asyncio's blocking wait() never yields back to
        # the main thread on Windows, causing Ctrl-C to be swallowed.
        while not self._stop_event.is_set():
            await asyncio.sleep(0.2)

    async def shutdown(self, trainer=None, agent_db=None, session_id: int = -1, twin_state=None) -> None:
        log.info("Saving calibration and flushing logs ...")

        # Stop twin state (flushes pending observe() tasks, persists preference model)
        if twin_state is not None:
            await twin_state.stop()

        # Stop trainer (flushes final gesture calibration)
        if trainer is not None:
            await trainer.stop()

        # Close session record in DB
        if agent_db is not None and session_id >= 0:
            await agent_db.close_session(session_id)

        # Stop registered components (FusionEngine, GestureProcessor, etc.)
        for comp in reversed(self._components):
            try:
                method = getattr(comp, "close", None) or getattr(comp, "stop", None)
                if method:
                    result = method()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                log.warning("Shutdown error for %s: %s", comp, exc)

        if agent_db is not None:
            await agent_db.close()

        log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# Watchdog — periodic health checks for soak testing
# ---------------------------------------------------------------------------

async def _watchdog(fusion, whisper, session_id: int) -> None:
    """Log periodic health metrics every 60 s to make soak runs interpretable.

    Checks:
      - FusionEngine tick rate (warns if < 50 Hz — indicates event loop starvation)
      - WhisperStream VAD thread alive
      - Ollama reachability (every 10 min)
      - GPU VRAM headroom (every 10 min)
      - Route task queue depth (warns if > 10 in-flight simultaneously)
    """
    import urllib.request as _ureq

    _WATCHDOG_PERIOD_S = 60.0
    _OLLAMA_CHECK_EVERY = 10  # cycles → every 10 min

    _last_tick_count = getattr(fusion, "_tick_count", None)
    _cycle = 0

    while True:
        await asyncio.sleep(_WATCHDOG_PERIOD_S)
        _cycle += 1

        # --- Tick rate ---
        current_ticks = getattr(fusion, "_tick_count", 0)
        if _last_tick_count is not None:
            hz = (current_ticks - _last_tick_count) / _WATCHDOG_PERIOD_S
            if hz < 50.0:
                log.warning("WATCHDOG: FusionEngine tick rate LOW: %.1f Hz (expected 60)", hz)
            else:
                log.info("WATCHDOG: FusionEngine %.1f Hz  route_tasks_inflight=%d",
                         hz, len(getattr(fusion, "_route_tasks", set())))
        _last_tick_count = current_ticks

        # --- Route task queue depth ---
        inflight = len(getattr(fusion, "_route_tasks", set()))
        if inflight > 10:
            log.warning("WATCHDOG: %d route tasks in-flight — possible coordinator stall", inflight)

        # --- WhisperStream thread ---
        if whisper is not None:
            vad_alive = getattr(whisper, "_vad_thread", None)
            if vad_alive is not None and not vad_alive.is_alive():
                log.error("WATCHDOG: WhisperStream VAD thread is DEAD (session %d)", session_id)

        # --- Ollama + VRAM (every 10 min) ---
        if _cycle % _OLLAMA_CHECK_EVERY == 0:
            try:
                with _ureq.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
                    r.read()
                log.info("WATCHDOG: Ollama reachable")
            except Exception as exc:
                log.warning("WATCHDOG: Ollama unreachable: %s", exc)

            try:
                import pynvml as nvml
                nvml.nvmlInit()
                h = nvml.nvmlDeviceGetHandleByIndex(0)
                info = nvml.nvmlDeviceGetMemoryInfo(h)
                nvml.nvmlShutdown()
                free_gb = info.free / (1024 ** 3)
                if free_gb < 2.0:
                    log.warning("WATCHDOG: GPU VRAM critically low: %.1f GB free", free_gb)
                else:
                    log.info("WATCHDOG: GPU VRAM %.1f GB free", free_gb)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pipeline assembly
# ---------------------------------------------------------------------------

async def _run_pipeline(args: argparse.Namespace) -> None:
    from command_executor import CommandExecutor
    from db import AgentDB
    from local_inference import OllamaInference
    from hybrid_coordinator import HybridCoordinator, CoordinatorConfig
    from behavioral_twin_state import BehavioralTwinState
    from fusion_engine import FusionEngine, FusionConfig
    from ipad_bridge import IPadBridge
    from continuous_trainer import ContinuousTrainer
    from lidar_receiver import LiDARReceiver
    from gesture_processor import GestureProcessor
    from model_router import ModelRouter
    from dev_agent import DevAgent
    from whisper_stream import WhisperStream
    from audit_log import AuditLog
    from content_filter import ContentFilter
    from mcp_trust_classifier import MCPTrustClassifier
    from gaze_calibrator import GazeCalibrator

    if args.safe_mode:
        os.environ["SAFE_MODE"] = "1"

    # --- Resolve screen size for FusionEngine ---
    try:
        import pyautogui
        sw, sh = pyautogui.size()
    except Exception:
        sw, sh = 1920, 1080

    # --- Open agent.db and start session ---
    agent_db = AgentDB()
    await agent_db.open(Path("agent.db"))

    # --- Open audit log (separate append-only DB) ---
    audit = AuditLog()
    await audit.open(Path("audit.db"))

    # --- Initialize content filter and trust classifier ---
    content_filter = ContentFilter(audit_log=audit)
    trust_classifier = MCPTrustClassifier(audit_log=audit)

    git_hash: Optional[str] = None
    try:
        git_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        pass

    mode = "safe" if args.safe_mode else "normal"
    session_id = await agent_db.insert_session(mode=mode, git_hash=git_hash)
    log.info("Session %d started (mode=%s git=%s)", session_id, mode, git_hash or "unknown")
    await audit.log_session_start(session_id)

    # --- Instantiate BehavioralTwinState ---
    twin_state = BehavioralTwinState(agent_db=agent_db)
    await twin_state.start()

    # --- Build components ---
    cfg = CoordinatorConfig()
    local = OllamaInference()

    # GestureProcessor created first so trainer can hold a reference for
    # calibrated threshold push-back.
    lidar = LiDARReceiver()
    gesture = GestureProcessor()
    gesture.set_lidar(lidar)

    trainer = ContinuousTrainer(
        agent_db=agent_db, config=cfg, twin_state=twin_state,
        gesture_processor=gesture,          # receives calibrated velocity thresholds
    )

    router = ModelRouter()
    coordinator = HybridCoordinator(
        local=local, config=cfg, trainer=trainer,
        agent_db=agent_db, session_id=session_id,
        content_filter=content_filter, audit_log=audit,
        twin_state=twin_state,
    )
    dev_agent = DevAgent(
        router=router, coordinator=coordinator, trainer=trainer,
        agent_db=agent_db,
    )
    coordinator.set_dev_agent(dev_agent)
    fusion = FusionEngine(screen_width=sw, screen_height=sh)
    fusion.set_coordinator(coordinator)

    from acoustic_profiler import AcousticProfiler
    profiler = AcousticProfiler(agent_db=agent_db, session_id=session_id)
    await profiler.load()

    whisper = WhisperStream()
    whisper.set_fusion_engine(fusion)
    whisper.set_agent_db(agent_db, session_id=session_id)
    whisper.set_acoustic_profiler(profiler)
    coordinator.set_whisper_stream(whisper)
    coordinator.set_fusion_engine(fusion)   # pain-day threshold propagation
    coordinator.set_profiler(profiler)

    # Wire VoiceCalibrator
    from voice_calibrator import VoiceCalibrator
    try:
        from polly_stream import get_client as _get_tts
        _speak_fn = _get_tts().speak_sync
    except Exception:
        _speak_fn = None
    calibrator = VoiceCalibrator(agent_db=agent_db, whisper_stream=whisper, profiler=profiler)
    if _speak_fn:
        calibrator.set_tts(_speak_fn)
    coordinator.set_calibrator(calibrator)

    # Wire profiler into twin state for voice clarity pain signal
    if twin_state:
        twin_state.set_acoustic_profiler(profiler)

    # --- Gaze calibrator (load persisted calibration if available) ---
    gaze_calibrator = GazeCalibrator(screen_w=sw, screen_h=sh)
    gaze_calibrator.load()
    if gaze_calibrator.is_calibrated:
        log.info("GazeCalibrator: loaded persisted calibration  residual=%.1f px",
                 gaze_calibrator.get_status()["residual_px"])

    bridge = IPadBridge(port=args.port, host=args.host)
    bridge.set_fusion_engine(fusion)
    bridge.set_lidar(lidar)
    bridge.set_gesture_processor(gesture)
    bridge.set_whisper_stream(whisper)
    bridge.set_coordinator(coordinator)  # needed for pain_day_override message
    bridge.set_agent_db(agent_db, session_id)  # needed for ipad_log DB persistence
    bridge.set_gaze_calibrator(gaze_calibrator)
    fusion.set_gaze_calibrator(gaze_calibrator)

    # Wire acoustic drift → bridge recalibration request (thread-safe)
    _loop = asyncio.get_event_loop()
    def _on_drift(drift):
        asyncio.run_coroutine_threadsafe(
            bridge.send_recalibration_request(
                reason=drift.reason,
                degradation_pct=drift.degradation_pct,
            ),
            _loop,
        )
    profiler.add_drift_callback(_on_drift)

    # Optional sensor viewer window
    viewer = None
    if args.viewer:
        from sensor_viewer import SensorViewer
        viewer = SensorViewer()
        bridge.set_viewer(viewer)
        viewer.start()

    shutdown = _ShutdownController()
    shutdown.register(fusion, gesture, whisper)
    if viewer:
        shutdown.register(viewer)
    shutdown.arm()

    # --- Start trainer and WhisperStream ---
    await trainer.start()
    await whisper.start()

    # --- Sync hotwords into WhisperStream once trainer is ready ---
    hotwords = await trainer.get_hotwords()
    if hotwords:
        whisper.update_hotwords(hotwords)

    # --- Print startup table (task 4.4) ---
    if not args.quiet:
        _print_startup_table(args.port, args.safe_mode, host=args.host)

    # --- Run bridge + fusion + watchdog concurrently ---
    bridge_task = asyncio.create_task(bridge.run(no_mdns=args.no_mdns))
    fusion_task = asyncio.create_task(fusion.run())
    watchdog_task = asyncio.create_task(_watchdog(fusion, whisper, session_id))

    # Wait for Ctrl-C
    await shutdown.wait_for_shutdown()

    # Cancel running tasks
    for t in (bridge_task, fusion_task, watchdog_task):
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    await shutdown.shutdown(trainer=trainer, agent_db=agent_db, session_id=session_id, twin_state=twin_state)
    await audit.log_session_stop(reason="normal")
    await audit.close()


# ---------------------------------------------------------------------------
# Viewer-only mode (bridge + viewer, no inference)
# ---------------------------------------------------------------------------

async def _run_viewer_only(args: argparse.Namespace) -> None:
    """Run just the bridge and sensor viewer — no LLM, no gesture, no whisper."""
    from ipad_bridge import IPadBridge
    from sensor_viewer import SensorViewer

    log.info("Starting in viewer-only mode (no inference pipeline)")

    viewer = SensorViewer(always_on_top=True)
    viewer.start()

    bridge = IPadBridge(port=args.port, host=args.host)
    bridge.set_viewer(viewer)

    try:
        await bridge.run(no_mdns=args.no_mdns)
    except KeyboardInterrupt:
        pass
    finally:
        viewer.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Personal Desktop Agent — iPad-to-Windows accessibility bridge"
    )
    p.add_argument("--port", type=int, default=8765,
                   help="WebSocket port (default: 8765)")
    p.add_argument("--host", type=str, default="0.0.0.0",
                   help="Bind address (default: 0.0.0.0; use 10.99.0.1 for WireGuard-only)")
    p.add_argument("--no-mdns", action="store_true",
                   help="Disable mDNS/Bonjour service advertisement")
    p.add_argument("--debug", action="store_true",
                   help="Enable DEBUG logging")
    p.add_argument("--safe-mode", action="store_true",
                   help="Block keyboard_type and mouse_drag (sets SAFE_MODE=1)")
    p.add_argument("--quiet", action="store_true",
                   help="Skip the startup status table")
    p.add_argument("--measure-vram", action="store_true",
                   help="Load all models, print VRAM snapshot, and exit (task 1.2)")
    p.add_argument("--viewer", action="store_true",
                   help="Open a desktop window showing iPad camera + LiDAR feeds")
    p.add_argument("--viewer-only", action="store_true",
                   help="Run only the bridge + viewer (no inference pipeline)")
    return p.parse_args()


def _raise_windows_timer_resolution() -> None:
    """Boost Windows timer resolution from the 15.6 ms default to 1 ms.

    Without this, asyncio.sleep(1/60) rounds up to ~31 ms on Windows because the
    OS scheduler quantum is 15.6 ms. The FusionEngine 60 Hz loop then actually
    runs at ~32 Hz. timeBeginPeriod(1) is process-scoped on Windows 10 2004+
    and is released automatically on process exit.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
        log.info("Windows timer resolution raised to 1 ms (60 Hz loops now achievable)")
    except OSError as exc:
        log.warning("timeBeginPeriod(1) failed: %s — FusionEngine will run at ~32 Hz", exc)


def main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    _raise_windows_timer_resolution()

    # Task 4.2 — early exit path
    if args.measure_vram:
        _measure_vram()
        return

    # Viewer-only mode: bridge + viewer, no inference pipeline
    if args.viewer_only:
        asyncio.run(_run_viewer_only(args))
        return

    log.info("Personal Desktop Agent starting ...")
    try:
        asyncio.run(_run_pipeline(args))
    except KeyboardInterrupt:
        pass  # SIGINT already handled by _ShutdownController


if __name__ == "__main__":
    main()
