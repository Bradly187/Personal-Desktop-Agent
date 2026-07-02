"""scripts/chat_demo.py — run the chat UI with a SCRIPTED coordinator (no LLM).

Drives the real ChatServer + EventBus + frame pump + frontend with a canned
event sequence, so the chat window and live DAG can be exercised end-to-end
without Ollama / models loaded. For manual/visual verification only — not wired
into the agent. Start with:  python scripts/chat_demo.py   (serves :8770)
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer, _APPROVAL_DIR
from core.events import EventBus


class _FakeDB:
    """Minimal AgentDB surface EventBus needs (durable log is a no-op here)."""
    async def insert_event(self, *a, **k):
        return 1

    async def upsert_event_consumer(self, *a, **k):
        return None


class _PassthroughScheduler:
    def submit(self, coro, priority, label="", trace_id=""):
        return asyncio.ensure_future(coro)


class _DemoCoordinator:
    """Publishes a scripted live-event sequence keyed by cmd.trace_id."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._active_root = str(Path(__file__).parent.parent)

    # Active-directory switching (specs/chat-context-attachments R1) — scripted.
    def list_writable_roots(self) -> dict:
        import tempfile
        return {"active_root": self._active_root,
                "writable_roots": [str(Path(__file__).parent.parent), tempfile.gettempdir()]}

    def set_active_directory(self, path: str, *, confirm: bool = False) -> dict:
        import os
        if not os.path.isdir(path):
            return {"status": "invalid", "path": path}
        in_scope = path in self.list_writable_roots()["writable_roots"]
        if not in_scope and not confirm:
            return {"status": "confirm_required", "path": path}
        self._active_root = path
        return {"status": "activated", "path": path, "active_root": path,
                "writable_roots": [path]}

    async def _pub(self, topic, payload, tid):
        await self._bus.publish(topic, payload, source="demo", trace_id=tid)

    async def route(self, cmd) -> dict:
        tid, text = cmd.trace_id, cmd.text.lower()

        # 1) Conversational → stream tokens (with markdown, R2).
        if text.startswith("explain") or "how" in text:
            answer = ("The **fusion engine** runs a 60 Hz loop with a 6-level "
                      "sensor priority:\n\n"
                      "1. touch\n2. voice-click\n3. tilt\n4. gesture\n"
                      "5. on-device keyword\n6. Whisper voice\n\n"
                      "```python\nasync def tick():\n    await fuse(sensors)\n```\n"
                      "See `core/fusion_engine.py` for details. ")
            for word in answer.split(" "):
                await self._pub("chat.token", {"text": word + " "}, tid)
                await asyncio.sleep(0.05)
            return {"status": "ok", "action": "dev_agent", "response": answer,
                    "domain": "general", "model": "gemma4:12b", "steps": 0}

        # 2) Dev plan → DAG with an approval gate on the destructive step.
        if any(k in text for k in ("create", "fizzbuzz", "script", "build", "run")):
            steps = [
                {"n": 1, "action": "READ_FILE", "args": "template.py", "deps": []},
                {"n": 2, "action": "WRITE_FILE", "args": "fizzbuzz.py", "deps": [1]},
                {"n": 3, "action": "RUN_TERMINAL", "args": "python fizzbuzz.py", "deps": [2]},
            ]
            await self._pub("plan.generated", {"goal": cmd.text, "steps": steps}, tid)

            # step 1 (read) runs immediately
            await self._run_node(1, "READ_FILE", tid)

            # approval card before the destructive write — carries the plan steps
            # and the pending diff (specs/chat-workbench-parity R5/R6).
            await self._pub("dag.approval_requested",
                            {"message": "I'll run WRITE_FILE, RUN_TERMINAL. Approve all?",
                             "destructive": True,
                             "goal": cmd.text,
                             "steps": [{"n": s["n"], "action": s["action"],
                                        "args": s["args"]} for s in steps],
                             "file_path": "fizzbuzz.py",
                             "diff": ("--- a/fizzbuzz.py\n+++ b/fizzbuzz.py\n"
                                      "@@ -0,0 +1,3 @@\n"
                                      "+for i in range(1, 101):\n"
                                      "+    s = 'Fizz'*(i%3==0) + 'Buzz'*(i%5==0)\n"
                                      "+    print(s or i)\n")}, tid)
            approved = await self._await_approval()
            if not approved:
                return {"status": "ok", "action": "dev_agent",
                        "response": "Cancelled — approval denied.", "steps": 1}

            await self._run_node(2, "WRITE_FILE", tid)
            await self._run_node(3, "RUN_TERMINAL", tid)
            # Post-run artifacts (R8): walkthrough card + rewindable marker.
            await self._pub("dag.walkthrough",
                            {"markdown": "## Walkthrough\n\n- Wrote `fizzbuzz.py`"
                                         " (3 lines)\n- Ran it: output `1 2 Fizz"
                                         " 4 Buzz …`\n"}, tid)
            await self._pub("dag.run_finalized",
                            {"run_id": 1, "status": "completed",
                             "rewindable": True}, tid)
            return {"status": "ok", "action": "dev_agent",
                    "response": "Created `fizzbuzz.py` and ran it. Output: 1 2 Fizz 4 Buzz …",
                    "domain": "plan", "model": "qwen3-coder:30b", "steps": 3}

        # 3) Simple command → 4-gate flow.
        await self._pub("gate.decided", {"gate": "all_pass", "latency_ms": 38,
                                         "domain": "command"}, tid)
        await asyncio.sleep(0.2)
        await self._pub("command.executed", {"action": "OPEN", "route": "local",
                                             "gate": "all_pass", "success": True,
                                             "latency_ms": 41}, tid)
        return {"status": "ok", "action": "OPEN", "response": "Opening Notepad.",
                "domain": "command"}

    async def _run_node(self, n, action, tid):
        await self._pub("dag.step_started", {"n": n, "action": action}, tid)
        await asyncio.sleep(0.7)
        await self._pub("dag.step_completed", {"n": n, "action": action, "success": True,
                                              "latency_ms": 120, "result_snippet": "ok",
                                              "args_snippet": "fizzbuzz.py"}, tid)

    # Chat run controls (specs/chat-workbench-parity R4/R8.3) — scripted.
    def request_dev_cancel(self) -> bool:
        return True

    async def revert_last_dev_run(self, trace_id: str = "") -> bool:
        await self._pub("dag.approval_requested",
                        {"message": "Undo the run: create fizzbuzz?",
                         "destructive": True,
                         "command": "restore: fizzbuzz.py"}, trace_id)
        return await self._await_approval()

    async def _await_approval(self, timeout_s: float = 12.0) -> bool:
        resp = _APPROVAL_DIR / "response"
        resp.unlink(missing_ok=True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if resp.exists():
                txt = resp.read_text(encoding="utf-8-sig").strip().lower()
                resp.unlink(missing_ok=True)
                return txt in ("yes", "y", "approve")
            await asyncio.sleep(0.1)
        return False


async def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args, _ = ap.parse_known_args()
    db = _FakeDB()
    bus = EventBus(db)
    cs = ChatServer(host="127.0.0.1", port=args.port)
    cs.set_event_bus(bus)
    cs.set_scheduler(_PassthroughScheduler())
    cs.set_coordinator(_DemoCoordinator(bus))
    await cs.start()
    print("chat demo on", cs.url())
    try:
        await asyncio.Event().wait()
    finally:
        await cs.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
