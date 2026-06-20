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
import warnings
from pathlib import Path
from typing import Optional

# torch.cuda imports pynvml (deprecated package) and emits a FutureWarning on every
# startup. nvidia-ml-py is already declared in requirements.txt and provides the same
# API; the warning is upstream noise we cannot fix in torch itself.
warnings.filterwarnings(
    "ignore",
    message="The pynvml package is deprecated",
    category=FutureWarning,
    module="torch",
)

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
    print("    specs/ipad-sensor-focus/diagrams/05-data-flow.md")
    print("    specs/ipad-sensor-focus/local-inference-comparison.md\n")


# ---------------------------------------------------------------------------
# Task 4.4 — Startup status table
# ---------------------------------------------------------------------------

def _print_startup_table(
    port: int,
    safe_mode: bool,
    host: str = "0.0.0.0",
    backend: str = "ollama",
    vllm_server_url: str = "http://localhost:8000",
    cloud_dev_agent: bool = False,
    cloud_dev_model: str = "claude-opus-4-8",
) -> None:
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
        from calibration.acoustic_profiler import AcousticProfiler  # noqa: F401
        return "OK", "acoustic profiler available"

    def _check_uiautomation():
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        avail = p.is_available()
        return ("OK", "UIA COM available") if avail else ("WARN", "UIA COM unavailable (comtypes?)")

    def _check_chromadb():
        import chromadb  # noqa: F401
        from chromadb.utils.embedding_functions import (  # noqa: F401
            SentenceTransformerEmbeddingFunction,
        )
        return "OK", "ChromaDB + MiniLM available (codebase RAG)"

    def _check_duckdb():
        import duckdb  # noqa: F401
        return "OK", f"v{duckdb.__version__}  (session analytics)"

    def _check_vscode_bridge():
        import urllib.request as _ur
        # Try a quick HTTP ping to the WebSocket port to see if extension is running
        try:
            _ur.urlopen("http://127.0.0.1:8767/", timeout=1)
        except Exception as exc:
            msg = str(exc)
            if "Connection refused" in msg or "actively refused" in msg:
                return "WARN", "extension not running — install desktop-agent-bridge/ and reload VS Code"
            # Any HTTP response (even 400/404) means the server is up
            return "OK", "bridge extension running on ws://127.0.0.1:8767"
        return "OK", "bridge extension running on ws://127.0.0.1:8767"

    def _check_llamacpp():
        import urllib.request as _ur
        try:
            with _ur.urlopen("http://localhost:8080/health", timeout=2) as r:
                r.read()
            return "OK", "llama-server reachable on :8080"
        except Exception:
            return "WARN", "llama-server not running (needed for --backend llamacpp)"

    def _check_vllm():
        try:
            import vllm  # noqa: F401
            return "OK", f"vllm v{vllm.__version__} installed (activate with --backend vllm)"
        except ImportError:
            return "WARN", "not installed — run vllm_setup.bat to fix CUDA wheels"

    def _check_cloud_dev_agent():
        if not cloud_dev_agent:
            return "off", "enable with --cloud-dev-agent"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "WARN", "anthropic SDK not installed"
        import os as _os
        if not _os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            return "WARN", "AWS_BEARER_TOKEN_BEDROCK not set"
        return "OK", f"{cloud_dev_model} (bedrock)"

    def _check_vllm_server():
        import urllib.request as _ur
        url = vllm_server_url.rstrip("/")
        try:
            with _ur.urlopen(f"{url}/v1/models", timeout=2) as r:
                r.read()
            return "OK", f"reachable at {url}"
        except Exception as exc:
            msg = str(exc).lower()
            if "refused" in msg or "timed out" in msg:
                return "WARN", f"unreachable at {url} — run scripts/start_vllm_server.bat"
            return "WARN", f"{url}: {str(exc)[:50]}"

    check("Metrics (in-process)",           lambda: ("OK", "metrics.py singleton ready"))
    check("ChromaDB RAG (codebase index)",  _check_chromadb)
    check("DuckDB (session analytics)",     _check_duckdb)
    check("VS Code Bridge",                 _check_vscode_bridge)
    check("llama.cpp server (:8080)",       _check_llamacpp)
    check("vLLM",                           _check_vllm)
    check("Cloud DevAgent (Anthropic)",     _check_cloud_dev_agent)
    if backend == "vllm-server":
        check("vLLM server (OpenAI HTTP)",   _check_vllm_server)
    check("GPU / VRAM (pynvml)",            _check_pynvml)
    check("Ollama LLM server",              _check_ollama)
    check("Whisper (faster-whisper)",       _check_whisper)
    check("Acoustic profiler",              _check_acoustic_profiler)
    check("UIAutomation (Win32)",           _check_uiautomation)
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

        # --- Ollama + VRAM (every 10 min) — run in thread to avoid blocking event loop ---
        if _cycle % _OLLAMA_CHECK_EVERY == 0:
            def _check_ollama_and_vram() -> tuple[str, str]:
                ollama_msg = ""
                vram_msg = ""
                try:
                    with _ureq.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
                        r.read()
                    ollama_msg = "ok"
                except Exception as exc:
                    ollama_msg = f"unreachable: {exc}"
                try:
                    import pynvml as nvml
                    nvml.nvmlInit()
                    h = nvml.nvmlDeviceGetHandleByIndex(0)
                    info = nvml.nvmlDeviceGetMemoryInfo(h)
                    nvml.nvmlShutdown()
                    vram_msg = f"{info.free / (1024**3):.1f} GB free"
                except Exception:
                    pass
                return ollama_msg, vram_msg

            ollama_status, vram_status = await asyncio.to_thread(_check_ollama_and_vram)
            if ollama_status == "ok":
                log.info("WATCHDOG: Ollama reachable")
            else:
                log.warning("WATCHDOG: Ollama %s", ollama_status)
            if vram_status:
                free_gb = float(vram_status.split()[0])
                if free_gb < 2.0:
                    log.warning("WATCHDOG: GPU VRAM critically low: %s", vram_status)
                else:
                    log.info("WATCHDOG: GPU VRAM %s", vram_status)


# ---------------------------------------------------------------------------
# Pipeline assembly
# ---------------------------------------------------------------------------

async def _run_pipeline(args: argparse.Namespace) -> None:
    from core.command_executor import CommandExecutor
    from storage.db import AgentDB
    from inference.local_inference import (
        OllamaInference, LlamaCppInference, VLLMInference, VLLMServerInference, VLLMEmbedder,
    )
    from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig
    from adaptive.behavioral_twin_state import BehavioralTwinState
    from core.fusion_engine import FusionEngine, FusionConfig
    from core.ipad_bridge import IPadBridge
    from adaptive.continuous_trainer import ContinuousTrainer
    from sensors.lidar_receiver import LiDARReceiver
    from sensors.gesture_processor import GestureProcessor
    from inference.model_router import ModelRouter, VLLMSpecialistPool
    from inference.dev_agent import DevAgent
    from sensors.whisper_stream import WhisperStream
    from storage.audit_log import AuditLog
    from adaptive.content_filter import ContentFilter
    from adaptive.mcp_trust_classifier import MCPTrustClassifier
    from monitoring.metrics import get_metrics
    from storage.session_analyzer import SessionAnalyzer

    if args.safe_mode:
        os.environ["SAFE_MODE"] = "1"

    # --- Crash marker: detect an unclean exit of the previous process ---
    # Written now, removed only after graceful shutdown completes. If it's
    # already present, the last run crashed/was killed — a brief TTS notice is
    # spoken once the pipeline is up (the data side is already handled by
    # mark_interrupted_runs + goal_queue requeue below).
    from core import crash_marker
    _unclean_exit = crash_marker.check_and_mark()

    # --- Resolve screen size for FusionEngine ---
    try:
        import pyautogui
        sw, sh = pyautogui.size()
    except Exception:
        sw, sh = 1920, 1080

    # --- Open agent.db and start session ---
    agent_db = AgentDB()
    await agent_db.open(Path("agent.db"))

    # --- Startup DB prune: keep high-write tables from growing unboundedly ---
    # sensor_telemetry: ~86,400 rows/day → retain 7 days
    # gesture_velocity_samples: ~7,200 rows/day → retain 90 days
    # ipad_logs: ~50 rows/day → retain 60 days
    # Non-fatal: prune failures are logged and skipped, never block startup.
    await agent_db.prune_sensor_telemetry(days=7)
    await agent_db.prune_gesture_velocity_samples(days=90)
    await agent_db.prune_ipad_logs(days=60)
    # Orchestration tables (added in schema v3)
    await agent_db.prune_event_log(days=7)
    await agent_db.prune_tool_calls(days=30)
    await agent_db.prune_rate_limit_events(days=7)
    # command_traces: tracing is on by default → grows per command; retain 30 days
    await agent_db.prune_command_traces(days=30)

    # --- Crash recovery: reconcile plans left mid-run by a previous process ---
    # Any agent_run still 'running' means the process died during a plan. Mark
    # them 'interrupted'; DevAgent.resume_pending_plan() can offer a gated resume.
    _interrupted = await agent_db.mark_interrupted_runs()
    if _interrupted:
        log.warning(
            "Recovered %d interrupted plan run(s) from a previous session — "
            "say 'resume task' to continue the most recent one.", _interrupted,
        )

    # Durable goal backlog (gap D): a goal left 'running' means the process died
    # mid-goal. Requeue it (idempotency_key prevents duplicates; poison goals that
    # exhausted max_attempts are marked failed). The drainer is kicked after the
    # pipeline is wired (see below) so queued goals from a previous session run.
    _requeued = await agent_db.requeue_stale_running()
    if _requeued:
        log.warning("Re-queued %d goal(s) from the durable backlog after a crash.", _requeued)

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

    # --- MemoryManager — schema-validated façade over AgentDB + SemanticMemory ---
    from storage.memory_manager import MemoryManager
    memory = MemoryManager(agent_db=agent_db, twin_state=twin_state)

    # --- Build components ---
    cfg = CoordinatorConfig()
    # Gap 1: restore learned Gate 1 threshold that was adapted in a prior session
    if agent_db.available:
        try:
            async with agent_db._conn.execute(
                "SELECT new_value FROM settings_versions "
                "WHERE component='coordinator' AND key='whisper_logprob_min' "
                "ORDER BY ts DESC LIMIT 1"
            ) as _cur:
                _row = await _cur.fetchone()
                if _row and _row[0]:
                    cfg.whisper_logprob_min = float(_row[0])
                    log.info("Gate 1: restored learned threshold %.2f from DB", cfg.whisper_logprob_min)
        except Exception as _exc:
            log.debug("Gate 1: could not restore threshold (using default): %s", _exc)

    # Backend selection (--backend flag)
    _backend = args.backend.lower() if hasattr(args, "backend") else "ollama"

    # Ensure a local Ollama server is up before building any Ollama-backed engine.
    # The command model (default backend) and the ModelRouter specialists both run
    # on Ollama, so start the server here if it isn't already listening — covers
    # every launch path (start_agent.bat, watchdog, scheduled task, direct). No-op
    # when Ollama is already up; degrades to cloud fallback if it can't be started.
    _uses_ollama = (_backend == "ollama") or (not getattr(args, "no_local_specialists", False))
    if _uses_ollama:
        from inference.local_inference import ensure_ollama_running
        await asyncio.to_thread(ensure_ollama_running)

    if _backend == "llamacpp":
        local = LlamaCppInference(
            model=getattr(args, "llamacpp_model", "local-model"),
            host=getattr(args, "llamacpp_host", "http://localhost:8080"),
        )
        log.info("Using llama.cpp backend (llama-server on %s)", local.host)
    elif _backend == "vllm":
        _vllm_speculative = getattr(args, "speculative", False)
        local = VLLMInference(
            model=getattr(args, "vllm_model", "meta-llama/Meta-Llama-3.1-8B-Instruct"),
            speculative_model="llama3.1:8b" if _vllm_speculative else None,
        )
        log.info(
            "Using vLLM backend (LLM class) — model loads on first inference request%s",
            " [speculative decoding enabled]" if _vllm_speculative else "",
        )
    elif _backend == "vllm-server":
        local = VLLMServerInference(
            base_url=getattr(args, "vllm_server_url", "http://127.0.0.1:8000"),
            model=getattr(args, "vllm_server_model",
                          "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4"),
        )
        log.info(
            "Using vLLM-server backend (OpenAI-compatible HTTP at %s) — "
            "server managed externally in WSL2 (scripts/start_vllm_server.sh)",
            local.base_url,
        )
    else:
        local = OllamaInference()

    # ── vLLM specialist pool (--vllm-pool) ────────────────────────────────
    # Architecture 2: INT4 AWQ specialists in vLLM, TTL-slept between requests.
    # Requires --backend vllm (command model) + WSL2 with vllm installed.
    # Ollama remains the automatic fallback if the pool raises.
    _vllm_pool: Optional[VLLMSpecialistPool] = None
    _no_local_specialists = getattr(args, "no_local_specialists", False)
    if _no_local_specialists and getattr(args, "vllm_pool", False):
        log.warning("--no-local-specialists overrides --vllm-pool; specialist pool disabled "
                    "(dev queries route to the cloud)")
    if getattr(args, "vllm_pool", False) and not _no_local_specialists:
        if _backend != "vllm":
            log.warning("--vllm-pool requires --backend vllm; specialist pool disabled")
        else:
            # Pass the command engine so the pool can sleep it before waking a
            # 30B-class specialist (they can't co-reside with Whisper on 32 GB).
            _vllm_pool = VLLMSpecialistPool(command_engine=local)
            # Mutual exclusion the other way: when the command engine wakes, sleep
            # any awake specialist first.
            if hasattr(local, "set_pre_wake_hook"):
                local.set_pre_wake_hook(_vllm_pool.sleep_all_specialists)
            await _vllm_pool.start()
            log.info("VLLMSpecialistPool: started (INT4 AWQ specialists) — "
                     "command<->specialist mutual-exclusion wired")

    # ── vLLM embedder (--vllm-embed) ──────────────────────────────────────
    # Replaces sentence-transformers in SemanticMemory / CodebaseIndexer.
    _vllm_embedder: Optional[VLLMEmbedder] = None
    if getattr(args, "vllm_embed", False):
        if _backend != "vllm":
            log.warning("--vllm-embed requires --backend vllm; using sentence-transformers")
        else:
            _vllm_embedder = VLLMEmbedder(
                model=getattr(args, "embed_model", "nomic-ai/nomic-embed-text-v1.5")
            )
            log.info("VLLMEmbedder: will activate on first encode() call")

    # GestureProcessor created first so trainer can hold a reference for
    # calibrated threshold push-back.
    lidar = LiDARReceiver()
    gesture = GestureProcessor()
    gesture.set_lidar(lidar)

    # D7: FlickEngine — wired if not in safe mode
    if not args.safe_mode:
        try:
            from desktop.flick_engine import FlickEngine
            from desktop.snap_zones import get_snap_zones, move_window_drag
            _flick_engine = FlickEngine(
                screen_w=sw, screen_h=sh,
                snap_zones_fn=get_snap_zones,
                move_window_fn=move_window_drag,
            )
            gesture.set_flick_engine(_flick_engine)
            log.info("FlickEngine: initialised  screen=%dx%d", sw, sh)
        except Exception as _fe_exc:
            log.warning("FlickEngine: could not initialise (%s) — flick-to-snap disabled", _fe_exc)

    trainer = ContinuousTrainer(
        agent_db=agent_db, config=cfg, twin_state=twin_state,
        gesture_processor=gesture,          # receives calibrated velocity thresholds
    )

    router = ModelRouter()
    if _vllm_pool is not None:
        router.set_vllm_pool(_vllm_pool)

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
    # E4: drain any escalations the DB couldn't accept on a prior run (DB-down at
    # halt time) back into the review queue now that the DB is healthy.
    try:
        await dev_agent.reconcile_pending_escalations()
    except Exception as _esc_exc:
        log.debug("escalation reconcile failed: %s", _esc_exc)

    # ── Skill model (N+1): MCP-client SkillRegistry ────────────────────────
    # Connecting an MCP server via a manifest (skills/manifests/*.json) adds
    # capability without editing core verbs; skills run as DevAgent
    # SKILL_QUERY/SKILL_CALL tool-calls. A skill that fails to start is skipped.
    skill_registry = None
    try:
        from skills.registry import SkillRegistry
        skill_registry = SkillRegistry(
            content_filter=content_filter, trust_classifier=trust_classifier,
            audit_log=audit, agent_db=agent_db,
        )
        await skill_registry.start()
        dev_agent.set_skill_registry(skill_registry)
        coordinator.set_skill_registry(skill_registry)   # voice 'help' listing
        if skill_registry.has_skills():
            log.info("SkillRegistry: skills active")
    except Exception as _skill_exc:
        log.warning("SkillRegistry: failed to start (%s) — skills disabled", _skill_exc)
        skill_registry = None

    # ── Personal knowledge base: semantic search over the user's own files ──
    # Pure-local (no auth, no cloud); indexes ~/Documents + ~/Notes (or the
    # roots in ~/.claude/personal_kb/config.json) in a background task so
    # startup is never blocked. Queried via "what did I write in my notes
    # about …" or the SEARCH_PERSONAL plan verb; re-index via "index my notes".
    personal_kb = None
    _kb_index_task = None
    try:
        from storage.personal_kb import PersonalKB
        personal_kb = PersonalKB()
        if await personal_kb.start():
            dev_agent.set_personal_kb(personal_kb)
            coordinator.set_personal_kb(personal_kb)
            _kb_index_task = asyncio.create_task(personal_kb.index(),
                                                 name="personal_kb_index")
        else:
            personal_kb = None
    except Exception as _kb_exc:
        log.warning("PersonalKB: failed to start (%s) — personal search disabled", _kb_exc)
        personal_kb = None

    # Wire MemoryManager into all storage-writing components
    coordinator.set_memory(memory)
    dev_agent.set_memory(memory)
    trainer.set_memory(memory)

    # ── Cloud DevAgent (--cloud-dev-agent) ─────────────────────────────────
    _cloud_dev_agent = None
    if getattr(args, "cloud_dev_agent", False):
        try:
            from inference.cloud_dev_agent import CloudDevAgent
            _cloud_dev_agent = CloudDevAgent()

            def _local_specialist_awake() -> bool:
                return _vllm_pool is not None and bool(_vllm_pool.get_status().get("awake"))

            coordinator.set_cloud_dev_agent(
                _cloud_dev_agent,
                always_cloud=_no_local_specialists,
                local_available_fn=_local_specialist_awake,
            )
            _cda_status = _cloud_dev_agent.get_status()
            log.info("CloudDevAgent: wired (available=%s model=%s always_cloud=%s)",
                     _cda_status["available"], _cda_status["model"], _no_local_specialists)
            if not _cda_status["available"]:
                log.warning("CloudDevAgent: AWS_BEARER_TOKEN_BEDROCK not set or anthropic SDK missing "
                            "— dev queries will return CLARIFY until configured")
        except Exception as _cda_exc:
            log.warning("CloudDevAgent: failed to initialise: %s", _cda_exc)
            _cloud_dev_agent = None

    # ── VS Code bridge client (--vscode flag) ─────────────────────────────
    if getattr(args, "vscode", False):
        try:
            from inference.bridge_client import BridgeClient
            bridge = BridgeClient()
            dev_agent.set_bridge(bridge)
            log.info("BridgeClient: wired to DevAgent (ws://127.0.0.1:8767)")
        except Exception as _bridge_exc:
            log.warning("BridgeClient: failed to initialise: %s", _bridge_exc)

    # ── Metrics singleton — wire to all pipeline components ────────────────
    m = get_metrics()
    fusion_pre = None   # FusionEngine created below; wire metrics after
    coordinator.set_metrics(m)
    if hasattr(local, "set_metrics"):
        local.set_metrics(m)     # ollama_hang_detected counter (FINDING 4)

    fusion = FusionEngine(screen_width=sw, screen_height=sh)
    fusion.set_coordinator(coordinator)
    fusion.set_metrics(m)           # wire metrics to FusionEngine (record_command_routed)
    fusion.set_session_id(session_id)

    # Magnetic cursor:
    #   * Phase 1 (tilt-tap snap) / Phase 2b (dwell snap) — per-click UIA lookup
    #     in command_executor; also reads the cache's cheap snapshot when running.
    #   * Phase 3 (cursor gravity) — biases the tilt cursor toward a nearby
    #     clickable at 60 Hz so the user can settle on buttons without precision.
    #
    # The background ClickableTargetCache was reworked (2026-06-05) to be
    # change-gated (walk only on foreground change / heartbeat) with a failure
    # backoff and a foreground-scoped UIA walk — fixing the E_POINTER thrash. The
    # fullscreen overlay that caused the DWM soft-hang was removed entirely;
    # gravity now runs headless. Kill-switch: DA_CURSOR_GRAVITY=0.
    target_cache = None
    if os.environ.get("DA_CURSOR_GRAVITY", "1") != "0":
        from desktop.target_cache import get_target_cache
        target_cache = get_target_cache()
        target_cache.start()
        fusion.set_target_cache(target_cache)
        log.info("Cursor gravity enabled (DA_CURSOR_GRAVITY)")
    else:
        log.info("Cursor gravity disabled via DA_CURSOR_GRAVITY=0")

    # Priority-aware scheduler — gates DEV_AGENT/BACKGROUND tasks so they
    # cannot starve accessibility commands during a flare.
    from core.scheduler import AccessibilityScheduler
    scheduler = AccessibilityScheduler()
    await scheduler.start()
    scheduler.set_metrics(m)        # queue-depth / dev-inflight visibility in /metrics
    fusion.set_scheduler(scheduler)
    dev_agent.set_scheduler(scheduler)

    from calibration.acoustic_profiler import AcousticProfiler
    profiler = AcousticProfiler(agent_db=agent_db, session_id=session_id)
    await profiler.load()

    whisper = WhisperStream()
    whisper.set_fusion_engine(fusion)
    whisper.set_agent_db(agent_db, session_id=session_id)
    whisper.set_acoustic_profiler(profiler)
    whisper.set_metrics(m)          # wire metrics to WhisperStream (latency + hallucinations)
    coordinator.set_whisper_stream(whisper)
    coordinator.set_fusion_engine(fusion)   # pain-day threshold propagation
    coordinator.set_profiler(profiler)

    # Wire VoiceCalibrator
    from calibration.voice_calibrator import VoiceCalibrator
    try:
        from tts.polly_stream import get_client as _get_tts
        _speak_fn = _get_tts().speak_sync
    except Exception:
        _speak_fn = None
    calibrator = VoiceCalibrator(agent_db=agent_db, whisper_stream=whisper, profiler=profiler)
    if _speak_fn:
        calibrator.set_tts(_speak_fn)
    coordinator.set_calibrator(calibrator)

    # Wire VoicePromptComposer — "hey agent claude compose" → dictate to Claude Code
    from inference.voice_prompt_composer import VoicePromptComposer
    composer = VoicePromptComposer()
    if _speak_fn:
        composer.set_speak_fn(_speak_fn)
    composer.set_suppress_fn(whisper.suppress)
    whisper.set_composer(composer)

    # Wire profiler into fusion engine for rms_ambient telemetry
    fusion.set_acoustic_profiler(profiler)

    # Wire profiler into twin state for voice clarity pain signal
    if twin_state:
        twin_state.set_acoustic_profiler(profiler)
        # Immediate pain-day velocity-floor flips (no 60s ContinuousTrainer lag)
        twin_state.set_gesture_processor(gesture)

    # ── EventBus + RateLimiter (orchestration gap remediation — schema v3) ────
    from core.events import EventBus
    from core.rate_limiter import RateLimiter
    event_bus = EventBus(agent_db)
    rate_limiter = RateLimiter(agent_db)
    coordinator.set_event_bus(event_bus)
    coordinator.set_rate_limiter(rate_limiter)   # throttles cloud (Anthropic) egress
    dev_agent.set_event_bus(event_bus)
    # Surface silent backend events: Ollama hang (inference.stalled) + breaker open
    # (breaker.opened). No-op on backends without a set_event_bus method.
    if hasattr(local, "set_event_bus"):
        local.set_event_bus(event_bus)

    # ── PC desktop chat UI (opt-in via --chat): chat window + live DAG preview ──
    # Standalone localhost aiohttp server sharing the live pipeline. Kept separate
    # from the iPad bridge so the iPad WebSocket protocol stays an iPad concern.
    chat_server = None
    if getattr(args, "chat", False):
        from core.chat_server import ChatServer
        chat_server = ChatServer(
            host=args.chat_host, port=args.chat_port,
            allow_destructive=not args.chat_readonly,
        )
        chat_server.set_coordinator(coordinator)
        chat_server.set_scheduler(scheduler)
        chat_server.set_event_bus(event_bus)
        chat_server.set_agent_db(agent_db, session_id)

    # Wire CommandExecutor DB access for per-call timeout + idempotency.
    coordinator._executor.set_agent_db(agent_db)
    # Wire audit log so command execution failures appear in the tamper-evident trail.
    coordinator._executor.set_audit_log(audit)

    # Read cache configs from DB and push to the cache-using components.
    try:
        vg_ttl, vg_max = await agent_db.get_cache_config("vision_grounder")
        if hasattr(coordinator, "_vision_grounder") and coordinator._vision_grounder:
            coordinator._vision_grounder.set_cache_config(vg_ttl, vg_max)
    except Exception as _cfg_exc:
        log.debug("Could not read vision_grounder cache config: %s", _cfg_exc)
    try:
        ua_ttl, ua_max = await agent_db.get_cache_config("ui_automation")
        from desktop.ui_automation import UIAutomationProvider as _UAP
        # UIAutomationProvider is a lazy singleton; update it when first accessed.
        from core import command_executor as _cex
        if _cex._ui_provider is not None:
            _cex._ui_provider.set_cache_config(ua_ttl, ua_max)
    except Exception as _cfg_exc:
        log.debug("Could not read ui_automation cache config: %s", _cfg_exc)

    bridge = IPadBridge(port=args.port, host=args.host)
    bridge.set_fusion_engine(fusion)
    bridge.set_lidar(lidar)
    bridge.set_gesture_processor(gesture)
    bridge.set_whisper_stream(whisper)
    bridge.set_coordinator(coordinator)  # needed for pain_day_override message
    bridge.set_agent_db(agent_db, session_id)  # needed for ipad_log DB persistence
    coordinator.set_bridge(bridge)  # trace_id correlation: coordinator → bridge on command executed
    coordinator.set_target_cache(target_cache)  # A2UI click-target palette (DA_A2UI_CLICK_TARGETS)
    fusion.set_agent_db(agent_db)   # D2: throttled sensor-stream persistence
    await fusion.load_rom_calibration(agent_db)   # D4: ROM → tilt dead zone
    await profiler.load_rom_bounds(agent_db)       # D4: ROM → initial VAD bounds

    # D6: wire profiler → WhisperStream so VAD changes push immediately
    profiler.set_whisper_ref(whisper)

    # Wire acoustic drift → provisional VAD relaxation + bridge recalibration request
    _loop = asyncio.get_event_loop()
    def _on_drift(drift):
        # D6: apply provisional relaxation immediately (before recal completes)
        profiler.apply_provisional_vad_relaxation(factor=0.7)
        asyncio.run_coroutine_threadsafe(
            bridge.send_recalibration_request(
                reason=drift.reason,
                degradation_pct=drift.degradation_pct,
            ),
            _loop,
        )
        # R-1 foundation: publish voice.drift so observer agents (Fatigue Monitor)
        # and event rules can react. Best-effort; never blocks the audio thread.
        asyncio.run_coroutine_threadsafe(
            event_bus.publish(
                "voice.drift",
                {"drift_pct": drift.degradation_pct, "reason": drift.reason,
                 "voice_samples": getattr(drift, "voice_samples", 0)},
                source="acoustic_profiler",
            ),
            _loop,
        )
    profiler.add_drift_callback(_on_drift)

    # A2UI: push an Approve/Deny surface to the iPad when the approval gate opens
    # (parallel input to the voice gate; a tap writes the same response file).
    # The callback fires from the WhisperStream audio thread → hop to the bridge
    # loop with run_coroutine_threadsafe, mirroring _on_drift above.
    def _on_approval_gate_open(description: str) -> None:
        from core import a2ui
        surface = a2ui.approval_surface(description)
        asyncio.run_coroutine_threadsafe(bridge.send_a2ui_surface(surface), _loop)
    whisper.on_approval_gate_open = _on_approval_gate_open

    # ── Optional codebase RAG index ────────────────────────────────────────
    indexer = None
    if args.index_codebase:
        try:
            from inference.codebase_indexer import CodebaseIndexer
            _project_root = str(Path(__file__).parent)
            indexer = CodebaseIndexer(
                project_root=_project_root,
                embedder=_vllm_embedder,   # None → falls back to sentence-transformers
            )
            if await indexer.start():
                _idx_stats = await indexer.index()
                log.info("CodebaseIndexer: %s", _idx_stats)
                dev_agent.set_indexer(indexer)
                # Start file watcher for continuous incremental indexing
                if getattr(args, "watch", False):
                    if indexer.start_watching():
                        log.info("CodebaseIndexer: file watcher active")
                    else:
                        log.info("CodebaseIndexer: file watcher unavailable (pip install watchdog)")
            else:
                log.warning("CodebaseIndexer: ChromaDB unavailable — RAG disabled")
                indexer = None
        except Exception as _idx_exc:
            log.warning("CodebaseIndexer: failed to start: %s", _idx_exc)
            indexer = None

    # ── Start VRAM poller + optional /metrics HTTP endpoint ────────────────
    await m.start_vram_poller(interval_s=60.0)

    if args.metrics_port:
        try:
            from aiohttp import web as _aio_web
            from monitoring.trace import get_tracer as _get_tracer
            # H1: bind loopback only. /trace returns recent command text and
            # /metrics is for local operator inspection — nothing remote needs them.
            _metrics_host = "127.0.0.1"
            _metrics_app = _aio_web.Application()
            _metrics_app.router.add_get("/metrics", m.aiohttp_handler)

            async def _trace_recent(_req):
                return _aio_web.json_response({"traces": _get_tracer().get_recent(50)})

            async def _trace_one(req):
                tr = _get_tracer().get_trace(req.match_info["tid"])
                if tr is None:
                    return _aio_web.json_response({"error": "not found"}, status=404)
                return _aio_web.json_response(tr)

            _metrics_app.router.add_get("/trace", _trace_recent)
            _metrics_app.router.add_get("/trace/{tid}", _trace_one)
            _metrics_runner = _aio_web.AppRunner(_metrics_app)
            await _metrics_runner.setup()
            _metrics_site = _aio_web.TCPSite(_metrics_runner, _metrics_host, args.metrics_port)
            await _metrics_site.start()
            log.info("Metrics endpoint: http://%s:%d/metrics", _metrics_host, args.metrics_port)
        except Exception as _me_exc:
            log.warning("Metrics HTTP endpoint failed: %s", _me_exc)

    # ── Optional sensor viewer window ──────────────────────────────────────
    viewer = None
    if args.viewer:
        from sensors.sensor_viewer import SensorViewer
        viewer = SensorViewer()
        bridge.set_viewer(viewer)
        if hasattr(gesture, "set_viewer"):
            gesture.set_viewer(viewer)
        if args.viewer and not args.safe_mode and hasattr(gesture, "_flick_engine"):
            fe = getattr(gesture, "_flick_engine", None)
            if fe is not None:
                viewer.set_flick_engine(fe)
        viewer.start()

    # ── Optional live dashboard ────────────────────────────────────────────
    dashboard_obj = None
    if args.dashboard:
        try:
            from monitoring.dashboard import Dashboard
            dashboard_obj = Dashboard(metrics=m, interval=1.0)
            await dashboard_obj.start()
        except Exception as _dash_exc:
            log.warning("Dashboard failed to start: %s", _dash_exc)

    shutdown = _ShutdownController()
    shutdown.register(fusion, gesture, whisper)
    shutdown.register(scheduler)
    if viewer:
        shutdown.register(viewer)
    shutdown.arm()

    # --- Start trainer and WhisperStream ---
    await trainer.start()
    await trainer.load_velocity_calibration()  # Gap 3: reload persisted gesture floors
    await whisper.start()

    # --- ResourceGovernor — pain-aware hardware resource control ---
    from core.resource_governor import ResourceGovernor
    governor = ResourceGovernor(memory=memory)
    governor.set_fusion_engine(fusion)
    governor.set_whisper_stream(whisper)
    governor.set_model_router(router)   # eviction targets the live model lineup
    governor.set_scheduler(scheduler)   # gap #3: flare pauses new dev/background admission
    governor.set_event_bus(event_bus)   # vram.evicted / vram.restored on flare transitions
    if indexer is not None:
        governor.set_indexer(indexer)
    # Live-refresh the iPad Agent dashboard on each pain-day (flare) transition so
    # its "Pain day" row reflects current state without waiting for a reconnect.
    governor.set_flare_change_callback(lambda active: bridge.push_status_dashboard())
    await governor.start()
    twin_state.set_resource_governor(governor)   # flare fast-path: <100ms flare response
    shutdown.register(governor)
    if target_cache is not None:
        shutdown.register(target_cache)

    # --- Proactivity (N+2): time- + event-triggered automation ---
    # ProactiveScheduler promotes due scheduled goals; EventRuleEngine fires rules
    # off the EventBus. Both feed the existing goal_queue/drainer; notifications go
    # out via Notifier (Danielle TTS + iPad push).
    from core.notifier import Notifier
    from core.proactive_scheduler import ProactiveScheduler
    from core.event_rule_engine import EventRuleEngine
    notifier = Notifier(bridge=bridge)
    # Flush store-and-forward notifications when an iPad (re)connects (E12).
    if bridge is not None:
        bridge.register_connect_handler(notifier.flush_pending)
    proactive = ProactiveScheduler(agent_db, dev_agent=dev_agent, scheduler=scheduler,
                                   notifier=notifier)
    event_rules = EventRuleEngine(agent_db, event_bus, notifier=notifier, dev_agent=dev_agent)
    await proactive.start()
    await event_rules.start()
    shutdown.register(proactive)
    shutdown.register(event_rules)

    # Email watcher (N+2): polls the Gmail skill and publishes email.arrived onto
    # the bus so event rules can fire. The loop always runs (skill presence is
    # re-checked per tick) so a google_pim hot-started by voice "connect Google"
    # is picked up without a restart; the notifier carries the one-time
    # token-expired alert.
    from core.email_watcher import EmailWatcher
    email_watcher = EmailWatcher(
        skill_registry, event_bus, notifier=notifier,
        state_path=Path.home() / ".claude" / "email_watcher_seen.json")
    await email_watcher.start()
    shutdown.register(email_watcher)

    # --- Supervisor — liveness watchdog for the critical background loops ---
    # (gap #2) Restarts the scheduler worker / governor poll loop if either dies
    # unexpectedly. Registered LAST so reversed-order shutdown stops it FIRST —
    # it must not try to restart a subsystem that shutdown is tearing down.
    from core.supervisor import Supervisor, SupervisedSpec
    supervisor = Supervisor()
    supervisor.supervise(SupervisedSpec(
        name="scheduler",
        is_alive=scheduler.is_healthy,
        restart=scheduler.restart,
        enabled=lambda: scheduler._running,
    ))
    # Periodic loops also expose a heartbeat + stale_after_s so the Supervisor
    # catches an ALIVE-BUT-WEDGED loop (stuck on a hung await), not just a crashed
    # one. stale_after_s is sized well above each loop's period + slowest legit
    # iteration so a healthy-but-slow tick is never killed. Event-driven loops
    # (scheduler worker, event_rule_engine) block idle by design — no heartbeat.
    supervisor.supervise(SupervisedSpec(
        name="resource_governor",
        is_alive=governor.is_healthy,
        restart=governor.restart,
        enabled=lambda: governor._running,
        last_heartbeat=governor.last_heartbeat,
        stale_after_s=60.0,    # poll=5s
    ))
    supervisor.supervise(SupervisedSpec(
        name="proactive_scheduler",
        is_alive=proactive.is_healthy,
        restart=proactive.restart,
        enabled=lambda: proactive._running,
        last_heartbeat=proactive.last_heartbeat,
        stale_after_s=180.0,   # poll=30s
    ))
    supervisor.supervise(SupervisedSpec(
        name="event_rule_engine",
        is_alive=event_rules.is_healthy,
        restart=event_rules.restart,
        enabled=lambda: event_rules._running,
    ))
    supervisor.supervise(SupervisedSpec(
        name="email_watcher",
        is_alive=email_watcher.is_healthy,
        restart=email_watcher.restart,
        enabled=lambda: email_watcher._running,
        last_heartbeat=email_watcher.last_heartbeat,
        stale_after_s=360.0,   # poll=120s; a wedged skill stdio call stalls here
    ))

    # Escalation (gap E): when a subsystem can't be restarted, TELL the user
    # (spoken warning — they may be unattended) and degrade rather than silently
    # die. A FAILED scheduler → bypass it: FusionEngine._emit falls back to direct
    # create_task dispatch when set_scheduler(None), so accessibility keeps working.
    async def _on_subsystem_failed(name: str) -> None:
        log.critical("Supervisor escalation: subsystem %r is FAILED (unrecoverable)", name)
        if name == "scheduler":
            try:
                fusion.set_scheduler(None)   # degrade to direct dispatch
                log.warning("Degraded mode: FusionEngine now dispatches directly "
                            "(scheduler bypassed); accessibility unaffected, "
                            "dev/background gating lost until restart")
            except Exception as exc:
                log.error("Degraded-mode handoff failed: %s", exc)
        try:
            from tts.polly_stream import get_client as _get_tts
            msg = (f"Warning. The {name.replace('_', ' ')} stopped responding and "
                   "could not be restarted. The system is running in a reduced mode.")
            await asyncio.to_thread(_get_tts().speak_sync, msg)
        except Exception as exc:
            log.debug("Supervisor escalation TTS unavailable: %s", exc)

    supervisor.set_on_failed(_on_subsystem_failed)
    supervisor.set_metrics(m)
    await supervisor.start()
    shutdown.register(supervisor)

    # --- Sync hotwords into WhisperStream once trainer is ready ---
    hotwords = await trainer.get_hotwords()
    if hotwords:
        whisper.update_hotwords(hotwords)

    # --- Print startup table (task 4.4) ---
    if not args.quiet:
        _print_startup_table(
            args.port, args.safe_mode, host=args.host,
            backend=_backend,
            vllm_server_url=getattr(args, "vllm_server_url", "http://localhost:8000"),
            cloud_dev_agent=getattr(args, "cloud_dev_agent", False),
            cloud_dev_model=(_cloud_dev_agent.model if _cloud_dev_agent else "claude-opus-4-8"),
        )

    # Durable goal backlog (gap D): drain any goals queued/requeued from a previous
    # session now that the full pipeline is wired. Fire-and-forget — each goal runs
    # through plan_and_run with its own approval gate + crash-recoverable ledger.
    from core.async_utils import fire_and_log
    fire_and_log(dev_agent.drain_goal_queue(), log, label="startup_goal_drain")

    # Warm the command model so the FIRST real command doesn't pay the cold-load
    # penalty (~7.5 s observed for llama3.1:8b loading into VRAM vs ~190 ms warm).
    # Fire-and-forget: never blocks startup or the 60 Hz loop. No-op on non-Ollama
    # backends (warmup() defaults to a no-op; only OllamaInference pre-loads).
    # Opt out with DA_COMMAND_WARMUP=0.
    if os.environ.get("DA_COMMAND_WARMUP", "1") != "0":
        fire_and_log(local.warmup(), log, label="command_model_warmup")

    # Crash notice: the previous process exited uncleanly (marker survived).
    # Tell the user briefly — they may have been away when it happened and
    # should know that recovered state (requeued goals, interrupted plans)
    # may apply. Fire-and-forget: TTS being down must not block startup.
    if _unclean_exit:
        async def _speak_crash_notice() -> None:
            from tts.polly_stream import get_client as _get_tts
            await asyncio.to_thread(
                _get_tts().speak_sync,
                "I restarted after a crash. Any in-progress work was recovered "
                "where possible — say 'resume task' if something was interrupted.",
            )
        fire_and_log(_speak_crash_notice(), log, label="crash_notice_tts")

    # --- Run bridge + fusion + watchdog concurrently ---
    bridge_task = asyncio.create_task(bridge.run(no_mdns=args.no_mdns))
    fusion_task = asyncio.create_task(fusion.run())
    watchdog_task = asyncio.create_task(_watchdog(fusion, whisper, session_id))

    # Start the chat UI server (if enabled) and open the desktop window/browser
    # once it is actually listening.
    if chat_server is not None:
        await chat_server.start()
        _open_chat_shell(chat_server.url(), getattr(args, "chat_window", False))

    # Wait for Ctrl-C
    await shutdown.wait_for_shutdown()

    # Cancel running tasks
    for t in (bridge_task, fusion_task, watchdog_task):
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    # Stop skill MCP-client sessions (tear down their stdio subprocesses)
    if skill_registry is not None:
        await skill_registry.stop()

    # Stop the personal KB. Order matters: stop() FIRST — it sets the worker's
    # cooperative stop event, so a still-running background index exits at the
    # next file instead of pinning the executor join for the whole Documents
    # walk. Then bounded-wait the task (cancel alone can't interrupt to_thread).
    if personal_kb is not None:
        await personal_kb.stop()
    if _kb_index_task is not None and not _kb_index_task.done():
        try:
            await asyncio.wait_for(_kb_index_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    # Stop the chat UI server
    if chat_server is not None:
        await chat_server.stop()

    # Stop vLLM specialist pool watchdog
    if _vllm_pool is not None:
        await _vllm_pool.stop()

    # Stop dashboard + indexer before shutdown flushes DB
    if dashboard_obj is not None:
        dashboard_obj.stop()
    m.stop_vram_poller()

    # Run session analytics and persist summary to DB
    if session_id >= 0:
        try:
            analyzer = SessionAnalyzer(agent_db_path=str(Path("agent.db")))
            summary = await analyzer.run_and_persist(session_id, agent_db)
            analyzer.close()
            report = analyzer.format_report(summary)
            log.info("Session summary:\n%s", report)
        except Exception as _sa_exc:
            log.warning("SessionAnalyzer failed: %s", _sa_exc)

    if indexer is not None:
        await indexer.stop()

    await shutdown.shutdown(trainer=trainer, agent_db=agent_db, session_id=session_id, twin_state=twin_state)
    await audit.log_session_stop(reason="normal")
    await audit.close()

    # Graceful shutdown completed — remove the crash marker so the next
    # startup doesn't announce a crash. Last step on purpose: anything that
    # dies before this point IS an unclean exit.
    crash_marker.clear()


# ---------------------------------------------------------------------------
# Viewer-only mode (bridge + viewer, no inference)
# ---------------------------------------------------------------------------

async def _run_viewer_only(args: argparse.Namespace) -> None:
    """Run just the bridge and sensor viewer — no LLM, no gesture, no whisper."""
    from core.ipad_bridge import IPadBridge
    from sensors.sensor_viewer import SensorViewer

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
# Chat UI desktop shell
# ---------------------------------------------------------------------------

def _open_chat_shell(url: str, native_window: bool) -> None:
    """Open the chat UI: a native pywebview window if requested and available,
    else the default browser. Never fatal — the URL is always reachable
    manually (and logged), so a missing GUI dependency can't break startup."""
    def _browser() -> None:
        try:
            import webbrowser
            webbrowser.open(url)
            log.info("Chat UI: opened in default browser — %s", url)
        except Exception as exc:
            log.warning("Chat UI: could not open browser (%s). Open manually: %s", exc, url)

    if native_window:
        try:
            import threading
            import webview  # pywebview — optional dependency (Edge WebView2 on Windows)

            def _win() -> None:
                try:
                    webview.create_window("Desktop Agent", url, width=1200, height=820)
                    webview.start()
                except Exception as exc:
                    log.warning("Chat UI: native window failed (%s) — browser fallback", exc)
                    _browser()

            threading.Thread(target=_win, daemon=True, name="chat-webview").start()
            log.info("Chat UI: launching native window — %s", url)
            return
        except ImportError:
            log.info("Chat UI: pywebview not installed — using browser "
                     "(install with: pip install pywebview)")
    _browser()


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
                   help="Bind address (default: 0.0.0.0, now pairing-token-gated; "
                        "pin to 10.99.0.1 with --no-mdns for WireGuard-only)")
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
    p.add_argument("--dashboard", action="store_true",
                   help="Show live TUI metrics dashboard in the terminal")
    p.add_argument("--index-codebase", action="store_true",
                   help="Index Python/Swift source + docs PDFs into ChromaDB RAG at startup")
    p.add_argument("--metrics-port", type=int, default=0,
                   help="Expose /metrics JSON endpoint on this port (0 = disabled)")
    p.add_argument("--chat", action="store_true",
                   help="Enable the PC desktop chat UI (chat window + live DAG preview)")
    p.add_argument("--chat-port", type=int, default=8770,
                   help="Port for the chat UI server (default: 8770)")
    p.add_argument("--chat-host", type=str, default="127.0.0.1",
                   help="Bind host for the chat UI server (default: 127.0.0.1, localhost-only)")
    p.add_argument("--chat-window", action="store_true",
                   help="Open the chat UI in a native window (pywebview) instead of the browser")
    p.add_argument("--chat-readonly", action="store_true",
                   help="Disable in-chat approval of destructive dev steps (voice/iPad approval only)")
    # ── Backend selection (roadmap item #1, #6) ──────────────────────────────
    p.add_argument("--backend", "--inference-backend", type=str, default="ollama",
                   dest="backend",
                   choices=["ollama", "vllm", "llamacpp", "vllm-server"],
                   help="LLM inference backend: ollama (default), vllm, llamacpp, vllm-server "
                        "(--inference-backend is an accepted alias)")
    p.add_argument("--vllm-model", type=str,
                   default="meta-llama/Meta-Llama-3.1-8B-Instruct",
                   help="HuggingFace model ID for vLLM backend")
    # ── vLLM-server backend (Option C: WSL2 `vllm serve` over HTTP) ──────────
    p.add_argument("--vllm-server-url", type=str, default="http://127.0.0.1:8000",
                   help="Base URL of the WSL2 `vllm serve` OpenAI-compatible server "
                        "(requires --backend vllm-server). Start it with "
                        "scripts/start_vllm_server.bat. Use 127.0.0.1, not "
                        "localhost: the ::1 resolution adds ~2s per request "
                        "(WSL NAT forwarding is IPv4-only).")
    p.add_argument("--vllm-server-model", type=str,
                   default="hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
                   help="Model name the vllm-server was started with (must match the "
                        "--model passed to `vllm serve`).")
    p.add_argument("--speculative", action="store_true",
                   help="Enable speculative decoding (roadmap #9): passes "
                        "--speculative-model llama3.1:8b to vLLM at startup. "
                        "Requires --backend vllm. Acceptance rate ~60-80%% on code.")
    p.add_argument("--llamacpp-model", type=str, default="local-model",
                   help="Model name label for llama.cpp backend (informational only)")
    p.add_argument("--llamacpp-host", type=str, default="http://localhost:8080",
                   help="llama-server base URL for llama.cpp backend")
    # ── vLLM Architecture 2: INT4 specialist pool + embedding ────────────────
    p.add_argument("--vllm-pool", action="store_true",
                   help="Enable vLLM INT4 specialist pool (requires --backend vllm). "
                        "Routes code/math/vision/plan/general through in-process vLLM "
                        "LLM instances with TTL sleep; 3-8s wake vs Ollama's 60s cold load. "
                        "Needs AWQ checkpoints on HuggingFace (see model_router.py header).")
    # ── Cloud DevAgent — Amazon Bedrock path for dev-domain queries ──────────
    p.add_argument("--cloud-dev-agent", action="store_true",
                   help="Route dev-domain queries (code/math/vision/plan/general) to "
                        "Claude via Amazon Bedrock as a fallback when no local "
                        "specialist is awake — avoids a ~50s GPU wake. "
                        "Needs AWS_BEARER_TOKEN_BEDROCK. ~$0.0175/query on Opus 4.8.")
    p.add_argument("--no-local-specialists", action="store_true",
                   help="Skip the VLLMSpecialistPool entirely and route ALL dev-domain "
                        "queries to the cloud (implies the cloud is the primary dev "
                        "path; requires --cloud-dev-agent). Keeps the GPU free for the "
                        "command path — no 30B specialist is ever woken.")
    p.add_argument("--vllm-embed", action="store_true",
                   help="Use VLLMEmbedder (nomic-embed-text-v1.5) for RAG instead of "
                        "sentence-transformers. Requires --backend vllm.")
    p.add_argument("--embed-model", type=str,
                   default="nomic-ai/nomic-embed-text-v1.5",
                   help="HuggingFace embedding model for --vllm-embed")
    # ── VS Code bridge (roadmap item #2) ───────────────────────────────────
    p.add_argument("--vscode", action="store_true",
                   help="Connect to VS Code bridge extension on ws://127.0.0.1:8767")
    # ── File watcher (roadmap item #5) ───────────────────────────────────────
    p.add_argument("--watch", action="store_true",
                   help="Enable continuous file watcher for incremental RAG re-indexing "
                        "(requires --index-codebase and pip install watchdog)")
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


def _configure_logging(debug: bool) -> None:
    """Configure root logging: pretty console + rotating file, with trace_id.

    Two improvements over the old one-line ``basicConfig`` (2026-06-19 observability
    follow-up):

    * **Rotation** — the agent runs as a watchdog-restarted daemon, so an
      unbounded ``logs/agent.log`` was a real disk-growth bug. A
      ``RotatingFileHandler`` caps it at 10 MB × 5 backups.
    * **trace_id correlation** — every record carries the in-flight ``trace_id``
      (via a log-record factory reading the trace ContextVar), so a console/file
      line can be pasted straight into ``python -m monitoring.replay <trace_id>``.
      Records logged outside a command context show ``--------``.

    Idempotent: safe to call once from ``main()``. Best-effort on the file handler
    (a read-only ``logs/`` must never stop the agent from starting).
    """
    # Inject trace_id onto every LogRecord so the format string can reference it
    # regardless of which logger emitted the record (a Filter would only cover
    # records logged directly to the logger it's attached to).
    from monitoring.trace import get_tracer

    _prev_factory = logging.getLogRecordFactory()

    def _factory(*fargs, **fkwargs):
        record = _prev_factory(*fargs, **fkwargs)
        try:
            tid = get_tracer().get_current()
        except Exception:
            tid = None
        record.trace_id = tid or "--------"
        return record

    logging.setLogRecordFactory(_factory)

    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(trace_id)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_h = RotatingFileHandler(
            log_dir / "agent.log", maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8",
        )
        # Full date in the file (it persists across days, unlike the console).
        file_h.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(trace_id)s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(file_h)
    except Exception as exc:  # disk/permission issue — console logging still works
        log.warning("Rotating file log unavailable (%s) — console only", exc)


def main() -> None:
    args = _parse_args()

    _configure_logging(args.debug)

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
