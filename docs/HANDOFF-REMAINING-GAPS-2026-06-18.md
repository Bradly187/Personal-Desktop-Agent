# Handoff Document: Remaining Critical Gaps (2026-06-18)

> **⚠️ Partially moot (2026-06-18).** The laptop compute cluster was **excised on 2026-06-19**
> (#118) — any gap below referencing `remote_indexer_service.py` / `remote_whisper_*` no longer
> applies (those files were deleted; the agent is single-machine local-only).

## Overview

Two HIGH-priority findings remain from the comprehensive security/performance/observability audit conducted on 2026-06-18. Both are well-scoped, have clear implementation sketches, and are ready for pickup.

**Completed this session:**
- ✅ FINDING 1 (CRITICAL) — Whisper service authentication (C2)
- ✅ FINDING 2 (HIGH) — Hardcoded secrets in config
- ✅ FINDING 3 (HIGH) — Route-task circuit breaker

**Remaining:**
- ⏳ FINDING 4 (MEDIUM) — Ollama semaphore timeout
- ⏳ FINDING 5 (HIGH) — Silent audit trail gaps

---

## FINDING 4: Ollama Semaphore Timeout (MEDIUM)

### Problem

The one-at-a-time Ollama semaphore (enforcing serial inference) has **no timeout wrapper**. If Ollama hangs (CUDA OOM, vLLM stall):

1. Next inference waits **indefinitely** for semaphore release
2. Ollama health check runs every **10 minutes** — hang invisible for 10+ min
3. Cloud fallback doesn't trigger until individual request timeout (30s in coordinator), but semaphore is never released to other tasks
4. Accessibility commands eventually time out at the coordinator level, but with a 10–30 min delay

**Real-world scenario:** User says "click" while Ollama is hung → no response for 10+ minutes → user thinks agent crashed.

### Current Code

**File:** `inference/local_inference.py` (L468 area)

```python
self._semaphore = asyncio.Semaphore(1)  # <- No timeout wrapper

# Inference call (somewhere in the class):
async with self._semaphore:
    # If Ollama is hung, this awaits forever
    response = await self._client.generate(...)  # Ollama HTTP POST
```

**Ollama health check:** `main.py` L443

```python
_OLLAMA_CHECK_EVERY = 10  # Check every 10 cycles (600 seconds) at 60 Hz
# Only checks liveness, doesn't release hung semaphores
```

### Solution

**Step 1: Add timeout wrapper to Ollama inference**

Wrap the semaphore-guarded inference in `asyncio.wait_for()`:

```python
# inference/local_inference.py — OllamaInference._generate_streaming() or equivalent

async def _generate_streaming(self, prompt: str, **kwargs):
    """Generate with timeout."""
    OLLAMA_TIMEOUT_S = float(os.environ.get("DA_OLLAMA_TIMEOUT_S", "45"))
    
    try:
        async with asyncio.timeout(OLLAMA_TIMEOUT_S):  # Python 3.11+
            async with self._semaphore:
                # Ollama inference
                response = await self._client.generate(...)
                return response
    except asyncio.TimeoutError:
        log.error("Ollama inference timeout after %.0fs — falling back to cloud", OLLAMA_TIMEOUT_S)
        self._metrics.inc("ollama_hang_detected")
        raise OllamaTimeoutError(f"Ollama did not respond within {OLLAMA_TIMEOUT_S}s")
```

**Python 3.10 fallback** (if needed):

```python
async def _generate_streaming(self, prompt: str, **kwargs):
    """Generate with timeout (Python 3.10 compat)."""
    OLLAMA_TIMEOUT_S = float(os.environ.get("DA_OLLAMA_TIMEOUT_S", "45"))
    
    try:
        async with self._semaphore:
            response = await asyncio.wait_for(
                self._client.generate(...),
                timeout=OLLAMA_TIMEOUT_S
            )
            return response
    except asyncio.TimeoutError:
        log.error("Ollama inference timeout after %.0fs — falling back to cloud", OLLAMA_TIMEOUT_S)
        self._metrics.inc("ollama_hang_detected")
        raise
```

**Step 2: Add metric**

In `monitoring/metrics.py`:

```python
self._counters: dict[str, int] = {
    ...
    "ollama_hang_detected": 0,  # <- New counter
}
```

**Step 3: Environment variable**

- `DA_OLLAMA_TIMEOUT_S` (default `45`, tunable for slower hardware)
- Document in CLAUDE.md: "If Ollama frequently times out, reduce `DA_OLLAMA_TIMEOUT_S` or check VRAM/temp on inference laptop"

**Step 4: Tests**

`tests/test_ollama_timeout.py`:

```python
async def test_ollama_timeout_fires_after_threshold():
    """Ollama inference should timeout if takes > threshold."""
    inf = OllamaInference(base_url="http://localhost:11434")
    inf.set_metrics(Metrics())
    
    # Mock the client to hang
    inf._client.generate = AsyncMock(side_effect=asyncio.sleep(100))
    
    with pytest.raises(asyncio.TimeoutError):
        await inf._generate_streaming("test prompt", timeout_s=0.1)
    
    # Metric should increment
    assert inf._metrics._counters["ollama_hang_detected"] == 1

async def test_ollama_normal_inference_not_affected():
    """Normal inference should complete without timeout."""
    inf = OllamaInference(base_url="http://localhost:11434")
    
    # Mock successful response
    inf._client.generate = AsyncMock(return_value="response text")
    
    result = await inf._generate_streaming("test prompt", timeout_s=10)
    assert result == "response text"
```

### Acceptance Criteria

- [ ] Ollama inference calls wrapped in `asyncio.wait_for(timeout=DA_OLLAMA_TIMEOUT_S)`
- [ ] `ollama_hang_detected` metric added to `monitoring/metrics.py`
- [ ] `OllamaTimeoutError` custom exception defined
- [ ] `DA_OLLAMA_TIMEOUT_S` environment variable documented in CLAUDE.md
- [ ] 2+ tests pass (timeout fires, normal case unaffected)
- [ ] Cloud fallback gracefully handles timeout (coordinator already has 30s timeout at gate 3)
- [ ] Manual test: Hang Ollama, verify agent falls back to cloud within timeout

### Effort Estimate

**~1–2 hours** (timeout wrapper + metric + tests)

---

## FINDING 5: Silent Audit Trail Gaps (HIGH)

### Problem

Critical operations **silently fail** without audit-logging, leaving forensic gaps:

1. **CommandExecutor failures (core/command_executor.py L557–558):**
   - Desktop actions (CLICK, OPEN, CLOSE) fail and only emit `log.error()`
   - Not written to `audit_log.db` (the tamper-evident hash-chain security log)
   - User disputes action outcome later → no forensic record of why it failed

2. **Approval hook vibe_summary unavailability (approval_hook.py L163):**
   - If Ollama is down, vibe_summary silently falls back to static prompt
   - No audit event: "vibe_summary unavailable at time T"
   - User approves destructive action → audit only says "approved", not that LLM feedback was absent

3. **Remote indexer query failures (inference/remote_indexer_service.py L104):**
   - Botched query returns 500 + error message
   - Not logged to PC audit trail → coordinator can't correlate failure to command

### Current Code

**CommandExecutor (L557–558):**

```python
def execute(self, cmd: Command) -> CommandResult:
    try:
        # ... execute action ...
    except Exception as exc:
        log.error("CommandExecutor: failed to execute %s: %s", action, exc)  # <- Only logs
        # NOT written to audit_log
        return CommandResult(error=str(exc))
```

**Approval hook (L163):**

```python
def _vibe_summary(self) -> str:
    try:
        # LLM call
        return llm.generate(...)
    except Exception:
        log.debug("vibe_summary failed")  # <- Debug level, silent fallback
        return STATIC_PROMPT  # User doesn't know
```

**Indexer query (L104):**

```python
except Exception as exc:
    log.error("query (%s) failed: %s", which, exc)  # <- Only logged locally
    return web.json_response({"error": str(exc)}, status=500)
    # No link to the desktop's audit trail
```

### Solution

**Step 1: Audit-log critical failures in CommandExecutor**

**File:** `core/command_executor.py`

```python
async def execute(self, cmd: Command) -> CommandResult:
    """Execute a command and audit-log failures."""
    try:
        # ... execute action ...
        # If success, optionally audit the successful critical action
        if action in ("WRITE_FILE", "RUN_TERMINAL", "CLOSE"):  # Destructive
            await audit_log.log("command_executed", detail={
                "action": action,
                "args": cmd.args,
                "status": "success"
            }, command_id=cmd.trace_id)
        return CommandResult(success=True)
    
    except Exception as exc:
        # CRITICAL: Audit the failure before returning
        await audit_log.log("command_execution_failed", detail={
            "action": action,
            "exception": str(exc),
            "traceback": traceback.format_exc(),  # Full context for debugging
            "args": cmd.args,
        }, command_id=cmd.trace_id)
        
        log.error("CommandExecutor: failed to execute %s: %s", action, exc)
        return CommandResult(error=str(exc))
```

**Step 2: Audit vibe_summary unavailability**

**File:** `approval_hook.py`

```python
def _vibe_summary(self) -> str:
    """Generate vibe summary, audit if unavailable."""
    try:
        summary = llm.generate(...)
        return summary
    except Exception as exc:
        # Log as WARNING (not debug) + emit audit event
        log.warning("vibe_summary_unavailable: %s (using static prompt)", exc)
        
        # NEW: Emit audit event so approval trail shows LLM feedback was absent
        try:
            import asyncio
            asyncio.create_task(audit_log.log(
                "vibe_summary_unavailable",
                detail={"reason": str(exc), "fallback": "static_prompt"}
            ))
        except Exception as log_exc:
            log.error("Failed to audit vibe_summary failure: %s", log_exc)
        
        return STATIC_PROMPT
```

**Step 3: Remote indexer failures → desktop audit**

**File:** `inference/remote_indexer_service.py` (no change needed — it's the laptop)

**File:** `sensors/remote_whisper_client.py` or caller (where remoteIndexer is used):

```python
# When consuming remote indexer results:
try:
    results = await remote_indexer.query(q)
    if results.get("error"):
        log.error("Remote indexer error: %s", results["error"])
        # NEW: Audit this failure
        await audit_log.log("remote_indexer_query_failed", detail={
            "query": q,
            "error": results["error"],
            "service": "laptop.indexer"
        })
except Exception as exc:
    log.error("Remote indexer call failed: %s", exc)
    await audit_log.log("remote_indexer_unavailable", detail={
        "error": str(exc),
        "service": "laptop.indexer"
    })
```

### Tests

**File:** `tests/test_audit_trail_coverage.py`

```python
async def test_command_execution_failure_audited():
    """CommandExecutor should audit failures."""
    executor = CommandExecutor()
    mock_audit = AsyncMock()
    executor.set_audit_log(mock_audit)
    
    # Mock a failing action
    executor._actions["CLICK"] = AsyncMock(side_effect=RuntimeError("click failed"))
    
    cmd = Command(source="voice", text="click", action="CLICK", trace_id="t1")
    result = await executor.execute(cmd)
    
    assert result.error is not None
    # Verify audit was called
    mock_audit.log.assert_called_once()
    call_args = mock_audit.log.call_args
    assert call_args[1]["event"] == "command_execution_failed"
    assert "click failed" in call_args[1]["detail"]["exception"]

async def test_vibe_summary_unavailability_audited():
    """Approval hook should audit when vibe_summary is unavailable."""
    hook = ApprovalHook()
    mock_audit = AsyncMock()
    hook.set_audit_log(mock_audit)
    
    # Mock LLM failure
    hook._llm = AsyncMock(side_effect=TimeoutError("LLM timeout"))
    
    summary = hook._vibe_summary()
    
    # Should have tried to audit
    # (may be fire-and-forget, so just check it doesn't crash)
    assert isinstance(summary, str)  # Falls back to static
    # (actual audit verification depends on implementation)

async def test_remote_indexer_failure_audited():
    """Remote indexer failures should be audit-logged."""
    # This test depends on where remote indexer is called
    # Example: in DevAgent or coordinator
    pass
```

### Acceptance Criteria

- [ ] CommandExecutor audit-logs all exceptions (command_execution_failed event)
- [ ] Approval hook audit-logs vibe_summary unavailability (separate event)
- [ ] Remote indexer errors/unavailability logged to desktop audit trail
- [ ] Audit events include: timestamp, action/operation, error details, command/trace ID
- [ ] Audit log never silently fails (errors logged to stderr if audit write fails)
- [ ] 3+ tests cover failure paths with audit verification
- [ ] Manual test: Trigger each failure (executor exception, vibe unavailable, remote indexer down), verify audit.db records the event

### Implementation Notes

**Audit log injection:**
- Pass `audit_log` reference to CommandExecutor via `set_audit_log()`
- Pass to ApprovalHook similarly
- Remote indexer failures caught at call site (DevAgent, coordinator)

**Thread safety:**
- All `audit_log.log()` calls use `fire_and_log()` (async background write)
- Failures don't block the action path

**Audit events structure:**
```python
await audit_log.log(
    event="command_execution_failed",  # Event type
    detail={  # Dict with context
        "action": "CLICK",
        "exception": "TimeoutError: Ollama...",
        "args": {...},
    },
    command_id="trace-uuid",  # Cross-layer linkage
)
```

### Effort Estimate

**~2–3 hours** (audit instrumentation at 3 sites + tests + verification)

---

## Dependency Chain

```
FINDING 4 (Ollama timeout)    — INDEPENDENT, can be picked up anytime
                                Effort: 1–2 hours
                                Impact: Prevents 10+ min hangs

FINDING 5 (Audit trails)      — INDEPENDENT, can be picked up anytime
                                Effort: 2–3 hours
                                Impact: Forensic observability for critical failures
```

Both can be worked **in parallel** or **sequentially**. No blocking dependencies.

---

## Getting Started

### For FINDING 4 (Timeout):

1. Read `inference/local_inference.py` and understand the semaphore guard
2. Add `asyncio.wait_for()` wrapper to inference methods
3. Create `OllamaTimeoutError` exception class
4. Add `ollama_hang_detected` metric
5. Write 2 tests: timeout fires, normal case
6. Manual test: `pkill vllm`, trigger command, verify cloud fallback within timeout
7. Commit with title: `perf(ollama): Add semaphore timeout to prevent 10+ min hangs`

### For FINDING 5 (Audit):

1. Read `storage/audit_log.py` to understand the schema (hash-chain, tamper-evidence)
2. Add audit calls to CommandExecutor.execute() exception handler
3. Add audit call to approval_hook._vibe_summary() exception handler
4. Find remote indexer call site and wrap with audit on failure
5. Write 3 tests covering each path
6. Manual test: Trigger each failure, verify `audit.db` contains event
7. Commit with title: `observability(audit): Log critical failures to audit trail`

### Context Files

- **Audit system:** [`storage/audit_log.py`](../storage/audit_log.py)
- **Metrics system:** [`monitoring/metrics.py`](../monitoring/metrics.py)
- **CommandExecutor:** [`core/command_executor.py`](../core/command_executor.py)
- **Approval hook:** [`approval_hook.py`](../approval_hook.py)
- **Remote indexer:** [`inference/remote_indexer_service.py`](../inference/remote_indexer_service.py)

### Prior Completed Work (Reference)

- **FINDING 1 (Whisper auth):** Commit `a3552e5` — Bearer token middleware, loopback default, pre-commit hook
- **FINDING 3 (Circuit breaker):** Commit `7196022` — In-flight task breaker, metrics, 13 tests

See:
- [`docs/SECURITY-FIXES-2026-06-18.md`](./SECURITY-FIXES-2026-06-18.md) (FINDING 1 deployment guide)
- [`URGENT-FIX-SUMMARY.md`](../URGENT-FIX-SUMMARY.md) (overview)

### Questions / Blockers

If you hit a blocker while implementing:
1. Check the test files in the repository (e.g., `tests/test_audit_log.py`) for patterns
2. Reference completed commits (audit system is already integrated; just needs new event types)
3. Ask for clarification on audit schema (hash-chain details) or metrics injection points

---

## Review Checklist (Before Merging)

- [ ] All tests pass (`pytest tests/test_ollama_timeout.py tests/test_audit_trail_coverage.py -v`)
- [ ] No regression in broader test suite (`pytest tests/ -k "not cluster" -x`)
- [ ] Metrics are exposed and visible in `get_snapshot()`
- [ ] Audit events are written to `audit.db` (verify with `sqlite3 agent.db "SELECT * FROM audit WHERE event='command_execution_failed' LIMIT 1"`)
- [ ] Manual test scenarios pass (see "Acceptance Criteria" for each)
- [ ] Commit message includes "Closes #X" or references the audit findings
- [ ] No sensitive info logged (credentials, API keys, full request bodies)

---

## Metrics to Monitor Post-Deploy

Once deployed, monitor:

**FINDING 4:**
- `ollama_hang_detected` counter — should stay 0 or be very rare
- If rate increases, check `DA_OLLAMA_TIMEOUT_S` tuning
- Alert: `ollama_hang_detected > 5` per hour

**FINDING 5:**
- `audit.command_execution_failed` event count — baseline then watch for spikes
- `audit.vibe_summary_unavailable` count — should be rare (Ollama healthy)
- Alert: Either counter increasing indicates infrastructure issues

---

## Sign-Off

**Date:** 2026-06-18  
**Completed by:** Claude Code (AI assistant)  
**Remaining work:** 2 findings (MEDIUM + HIGH priority)  
**Estimated total effort:** 3–5 hours  
**Ready for pickup:** Yes, both findings are scoped and decoupled
