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

    def _check_kiro():
        import urllib.request as _ur
        # Try a quick HTTP ping to the WebSocket port to see if extension is running
        try:
            _ur.urlopen("http://127.0.0.1:8767/", timeout=1)
        except Exception as exc:
            msg = str(exc)
            if "Connection refused" in msg or "actively refused" in msg:
                return "WARN", "extension not running — install kiro-extension/ and reload Kiro"
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
        if not _os.environ.get("ANTHROPIC_API_KEY"):
            return "WARN", "ANTHROPIC_API_KEY not set"
        return "OK", f"{cloud_dev_model} (anthropic_cloud)"

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
    check("Kiro/VS Code bridge",            _check_kiro)
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

    # Backend selection (--backend flag)
    _backend = args.backend.lower() if hasattr(args, "backend") else "ollama"
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
            base_url=getattr(args, "vllm_server_url", "http://localhost:8000"),
            model=getattr(args, "vllm_server_model", "meta-llama/Meta-Llama-3.1-8B-Instruct"),
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

    # ── Cluster offload (laptop service node) ──────────────────────────────
    # Loads cluster_config.json if present; otherwise a disabled config (no-op).
    # When lightweight_host == "laptop", the command domain is routed to the
    # laptop's Ollama while the health monitor reports it up.
    from core.cluster_config import ClusterConfig
    from core.cluster_health import ClusterHealthMonitor
    cluster_cfg = ClusterConfig.load(getattr(args, "cluster_config", None))
    cluster_health = None
    if cluster_cfg.enabled:
        cluster_health = ClusterHealthMonitor(cluster_cfg)
        await cluster_health.start()
    router.set_cluster(cluster_cfg, cluster_health)

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
                log.warning("CloudDevAgent: ANTHROPIC_API_KEY not set or anthropic SDK missing "
                            "— dev queries will return CLARIFY until configured")
        except Exception as _cda_exc:
            log.warning("CloudDevAgent: failed to initialise: %s", _cda_exc)
            _cloud_dev_agent = None

    # Cluster offload: route DevAgent RAG queries to the laptop indexer service.
    if cluster_cfg.has_remote_indexer:
        dev_agent.set_remote_indexer_url(cluster_cfg.laptop_indexer_url)
        dev_agent.set_cluster_health(cluster_health)

    # ── Kiro/VS Code bridge client (--kiro flag) ───────────────────────────
    if getattr(args, "kiro", False):
        try:
            from inference.kiro_client import KiroClient
            kiro = KiroClient()
            dev_agent.set_kiro(kiro)
            log.info("KiroClient: wired to DevAgent (ws://127.0.0.1:8767)")
        except Exception as _kiro_exc:
            log.warning("KiroClient: failed to initialise: %s", _kiro_exc)

    # ── Metrics singleton — wire to all pipeline components ────────────────
    m = get_metrics()
    fusion_pre = None   # FusionEngine created below; wire metrics after
    coordinator.set_metrics(m)

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
    # Cluster offload: delegate transcription to the laptop Whisper service when
    # configured. set_remote_url() must precede whisper.start() so the local
    # large-v3 model is never loaded (saves ~3.5 GB VRAM on the desktop).
    if cluster_cfg.has_remote_whisper:
        whisper.set_remote_url(cluster_cfg.laptop_whisper_url)
        whisper.set_cluster_health(cluster_health)
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
    dev_agent.set_event_bus(event_bus)

    # Wire CommandExecutor DB access for per-call timeout + idempotency.
    coordinator._executor.set_agent_db(agent_db)

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
    profiler.add_drift_callback(_on_drift)

    # ── RealSense L515 HandPointer ────────────────────────────────────────────
    _realsense_proc = None
    if os.environ.get("DA_REALSENSE", "0") != "0" or getattr(args, "realsense", False):
        try:
            import json as _rs_json
            from sensors.hand_pointer import (
                HandPointer, HandPointerConfig, ThumbClick, ThumbClickConfig,
            )
            _CORNER_ORDER = ("TOP-LEFT", "TOP-RIGHT", "BOTTOM-RIGHT", "BOTTOM-LEFT")
            _hp_cfg = HandPointerConfig()
            _cal_path = Path(__file__).parent / "hand_pointer_calibration.json"
            if _cal_path.exists():
                try:
                    with open(_cal_path) as _f:
                        _cal_data = _rs_json.load(_f)
                    _mon_key = f"{sw}x{sh}@0,0"
                    _cal_entry = _cal_data.get("monitors", {}).get(_mon_key)
                    if _cal_entry:
                        _hp_cfg.in_x0 = float(_cal_entry["in_x0"])
                        _hp_cfg.in_y0 = float(_cal_entry["in_y0"])
                        _hp_cfg.in_x1 = float(_cal_entry["in_x1"])
                        _hp_cfg.in_y1 = float(_cal_entry["in_y1"])
                        _cc = _cal_entry.get("corners")
                        if isinstance(_cc, dict) and len(_cc) == 4:
                            _hp_cfg.corners = [
                                list(map(float, _cc[k])) for k in _CORNER_ORDER
                            ]
                        _hp_cfg.overshoot = 0.06
                        log.info("RealSense HandPointer: calibration loaded for %s", _mon_key)
                    else:
                        log.warning("RealSense HandPointer: no calibration for %s — "
                                    "run validate_realsense.py --calibrate-corners", _mon_key)
                except Exception as _e:
                    log.warning("RealSense HandPointer: calibration load error: %s", _e)
            else:
                log.warning("RealSense HandPointer: no calibration file — "
                            "run validate_realsense.py --calibrate-corners first")

            import ctypes as _rs_ct
            _rs_u32 = _rs_ct.windll.user32

            def _rs_move(x, y):
                _rs_u32.SetCursorPos(int(x), int(y))

            _hp_gravity = None
            if target_cache is not None:
                def _hp_gravity(x, y, r):
                    tg = target_cache.nearest(int(x), int(y), r, max_dim=120)
                    return tg.center() if tg else None

            _thumb_click = (
                ThumbClick(ThumbClickConfig()) if getattr(args, "thumb_click", False) else None
            )
            hand_pointer = HandPointer(
                sw, sh, _hp_cfg,
                move_cb=_rs_move,
                gravity_provider=_hp_gravity,
            )
            bridge.set_hand_pointer(hand_pointer, thumb_click=_thumb_click)
            log.info(
                "RealSense HandPointer active  screen=%dx%d  gravity=%s  click=%s",
                sw, sh,
                "ON" if _hp_gravity else "OFF",
                "thumb-pinch" if _thumb_click else "dwell",
            )

            # Auto-start the capture sidecar (Python 3.10 venv) unless suppressed.
            _sidecar_py = Path(__file__).parent / ".venv-realsense" / "Scripts" / "python.exe"
            _sidecar_script = Path(__file__).parent / "sensors" / "realsense_publisher.py"
            if (_sidecar_py.exists() and _sidecar_script.exists()
                    and not getattr(args, "no_realsense_sidecar", False)):
                import subprocess as _subp
                _realsense_proc = _subp.Popen(
                    [str(_sidecar_py), str(_sidecar_script),
                     "--host", "127.0.0.1", "--port", str(args.port)],
                    stdout=_subp.DEVNULL,
                    stderr=open(Path(__file__).parent / "logs" / "realsense_sidecar.err", "w"),
                )
                log.info("RealSense sidecar started (pid=%d)", _realsense_proc.pid)
            else:
                if not _sidecar_py.exists():
                    log.info("RealSense sidecar: .venv-realsense not found — "
                             "run start_realsense.bat manually")
                else:
                    log.info("RealSense sidecar: auto-start suppressed (--no-realsense-sidecar)")
        except Exception as _rs_exc:
            log.warning("RealSense HandPointer setup failed: %s", _rs_exc)

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
            _metrics_site = _aio_web.TCPSite(_metrics_runner, "0.0.0.0", args.metrics_port)
            await _metrics_site.start()
            log.info("Metrics endpoint: http://0.0.0.0:%d/metrics", args.metrics_port)
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
    await whisper.start()

    # --- ResourceGovernor — pain-aware hardware resource control ---
    from core.resource_governor import ResourceGovernor
    governor = ResourceGovernor(memory=memory)
    governor.set_fusion_engine(fusion)
    governor.set_whisper_stream(whisper)
    governor.set_model_router(router)   # eviction targets the live model lineup
    governor.set_scheduler(scheduler)   # gap #3: flare pauses new dev/background admission
    if indexer is not None:
        governor.set_indexer(indexer)
    await governor.start()
    twin_state.set_resource_governor(governor)   # SVT fast-path: <100ms flare response
    shutdown.register(governor)
    if target_cache is not None:
        shutdown.register(target_cache)

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
    supervisor.supervise(SupervisedSpec(
        name="resource_governor",
        is_alive=governor.is_healthy,
        restart=governor.restart,
        enabled=lambda: governor._running,
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
        if cluster_cfg.enabled:
            _h = cluster_health.status() if cluster_health is not None else {}
            def _mark(svc):
                return "UP" if _h.get(svc) else "down"
            print("  Cluster node (laptop):")
            print(f"    Ollama (lightweight) : {cluster_cfg.laptop_ollama_url or '-'}  [{_mark('laptop_ollama')}]"
                  f"  offload={'on' if cluster_cfg.offload_lightweight else 'off'}")
            print(f"    Whisper              : {cluster_cfg.laptop_whisper_url or '-'}  [{_mark('whisper')}]")
            print(f"    Indexer              : {cluster_cfg.laptop_indexer_url or '-'}  [{_mark('indexer')}]")

    # Durable goal backlog (gap D): drain any goals queued/requeued from a previous
    # session now that the full pipeline is wired. Fire-and-forget — each goal runs
    # through plan_and_run with its own approval gate + crash-recoverable ledger.
    from core.async_utils import fire_and_log
    fire_and_log(dev_agent.drain_goal_queue(), log, label="startup_goal_drain")

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

    if _realsense_proc is not None:
        _realsense_proc.terminate()
        log.info("RealSense sidecar terminated (pid=%d)", _realsense_proc.pid)

    # Stop cluster health monitor
    if cluster_health is not None:
        await cluster_health.stop()

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
    p.add_argument("--dashboard", action="store_true",
                   help="Show live TUI metrics dashboard in the terminal")
    p.add_argument("--index-codebase", action="store_true",
                   help="Index Python/Swift source + docs PDFs into ChromaDB RAG at startup")
    p.add_argument("--metrics-port", type=int, default=0,
                   help="Expose /metrics JSON endpoint on this port (0 = disabled)")
    p.add_argument("--cluster-config", type=str, default=None,
                   help="Path to cluster_config.json (default: project root). "
                        "Enables laptop-node offload of the lightweight command domain.")
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
    p.add_argument("--vllm-server-url", type=str, default="http://localhost:8000",
                   help="Base URL of the WSL2 `vllm serve` OpenAI-compatible server "
                        "(requires --backend vllm-server). Start it with "
                        "scripts/start_vllm_server.bat.")
    p.add_argument("--vllm-server-model", type=str,
                   default="meta-llama/Meta-Llama-3.1-8B-Instruct",
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
    # ── Cloud DevAgent — Anthropic API path for dev-domain queries ───────────
    p.add_argument("--cloud-dev-agent", action="store_true",
                   help="Route dev-domain queries (code/math/vision/plan/general) to "
                        "Claude via the Anthropic API as a fallback when no local "
                        "specialist is awake — avoids a ~50s GPU wake. "
                        "Needs ANTHROPIC_API_KEY. ~$0.01/query on Sonnet 4.6.")
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
    # ── Kiro/VS Code bridge (roadmap item #2) ────────────────────────────────
    p.add_argument("--kiro", action="store_true",
                   help="Connect to Kiro/VS Code bridge extension on ws://127.0.0.1:8767")
    # ── File watcher (roadmap item #5) ───────────────────────────────────────
    p.add_argument("--watch", action="store_true",
                   help="Enable continuous file watcher for incremental RAG re-indexing "
                        "(requires --index-codebase and pip install watchdog)")
    # ── RealSense L515 hand-pointer ───────────────────────────────────────────
    p.add_argument("--realsense", action="store_true",
                   help="Enable RealSense L515 absolute hand-pointer cursor "
                        "(DA_REALSENSE=1 env var also works); auto-starts the "
                        ".venv-realsense sidecar if found")
    p.add_argument("--no-realsense-sidecar", action="store_true",
                   help="With --realsense: wire HandPointer but don't auto-start "
                        "the sidecar (run start_realsense.bat manually)")
    p.add_argument("--thumb-click", action="store_true",
                   help="With --realsense: use thumb-pinch to click instead of dwell")
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
