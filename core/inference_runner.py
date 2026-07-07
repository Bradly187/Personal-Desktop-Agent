"""InferenceRunner — local/cloud model dispatch extracted from HybridCoordinator.

Owns the two inference paths a Gate-2/3/4 decision routes to:
  - ``run_local``: OllamaInference (or another LocalInference backend), guarded
    by a circuit-breaker timeout so a wedged backend can't stall the pipeline.
  - ``run_cloud``: Anthropic API (Amazon Bedrock backend) via ``_CloudInference``,
    with content-filter scrubbing and a rate-limiter check before egress.

All coordinator-owned dependencies (local/cloud backends, trainer, agent_db,
content_filter, rate_limiter) are injected as zero-arg accessor callables
rather than captured by value, and are resolved at call time — several of
these (``rate_limiter`` in particular) are wired onto HybridCoordinator *after*
construction via a ``set_*`` method, and tests routinely monkeypatch
``coord._content_filter`` / ``coord._cloud`` / etc. post-construction, so a
captured-by-value reference would silently go stale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from dataclasses import replace as _dc_replace

from core.gate_evaluator import _EFFECTIVE_CFG
from inference.local_inference import (
    _SYSTEM_PROMPT,
    get_inference_capture,
    set_inference_capture,
)
from monitoring.trace import get_tracer
from monitoring.cost_ledger import estimate_cost

if TYPE_CHECKING:
    from core.command_executor import Command
    from core.hybrid_coordinator import CoordinatorConfig

log = logging.getLogger(__name__)

# Task-local accumulator linking inference rows to their command row (the
# inference insert happens before insert_command, so command_id is unknown at
# write time and backfilled by route() — see AgentDB.commands.link_inferences_to_command).
_PENDING_INFERENCE_IDS: ContextVar[Optional[list]] = ContextVar(
    "da_pending_inference_ids", default=None
)

# Cloud system prompt — mirrors local _SYSTEM_PROMPT but adds misrecognition
# guidance specific to voice+accessibility input. Kept separate so we can
# tune cloud behaviour independently of the local LLM prompt.
_CLOUD_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT
    + "\n\nAdditional guidance for cloud disambiguation:\n"
    "- Common voice misrecognitions to correct automatically:\n"
    '  "clothes"→CLOSE  "scroll done"→SCROLL down  "hot key"→HOTKEY\n'
    '  "oh pen"→OPEN  "clique"→CLICK  "tight"→TYPE\n'
    "- For multi-step commands (\"close then open\"): execute the FIRST action "
    "and emit CLARIFY for the remaining steps.\n"
    "- For gesture-source commands: POINT→CLICK FIST→CLOSE PALM→SCROLL up."
)


class _CloudInference:
    """Anthropic SDK Claude backend. Lazy import."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._client = None
        self._effective_model = model   # backend-mapped at client-build time
        self._backend = "bedrock"

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise RuntimeError("anthropic not installed — run: pip install anthropic")
            # Resolve the backend (direct Anthropic vs Amazon Bedrock) and the
            # credential in one place. resolve_backend() raises a clear, actionable
            # message when the needed key is missing, instead of the raw SDK "Could
            # not resolve authentication method" traceback at request time.
            from core.cloud_backend import resolve_backend
            backend = resolve_backend()
            self._client = backend.make_client()
            self._backend = backend.name
            self._effective_model = backend.map_model(self._model)
        return self._client

    async def infer(self, cmd: "Command") -> str:
        try:
            client = self._get_client()
        except RuntimeError as exc:
            log.error("CloudInference unavailable: %s", exc)
            return f"CLARIFY cloud unavailable: {exc}"

        context_lines = ""
        if cmd.session_context:
            joined = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
            context_lines = f"\n\nRecent commands:\n{joined}"

        user_content = f"Command: {cmd.text}{context_lines}"
        # Task-local fine-tuning capture (see inference.local_inference) — a
        # ContextVar so concurrent route() tasks can't misattribute prompts.
        prompt_json = json.dumps([
            {"role": "system", "content": _CLOUD_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ])
        set_inference_capture(prompt_json)

        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=self._effective_model,
                max_tokens=64,
                temperature=0.0,
                system=_CLOUD_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            action = response.content[0].text.strip().splitlines()[0].strip()
            if hasattr(response, "usage") and response.usage:
                set_inference_capture(
                    prompt_json,
                    response.usage.input_tokens, response.usage.output_tokens,
                )
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("CloudInference[%s]: %r → %r (%.0f ms)",
                     self._backend, cmd.text, action, latency_ms)
            return action
        except Exception as exc:
            # Surface authentication problems (invalid/expired/empty key, 401) with
            # an actionable hint rather than the raw SDK error text. AuthenticationError
            # subclasses the SDK's APIStatusError with status 401.
            msg = str(exc)
            is_auth = (
                type(exc).__name__ == "AuthenticationError"
                or getattr(exc, "status_code", None) == 401
                or "authentication" in msg.lower()
                or "x-api-key" in msg.lower()
            )
            if is_auth:
                log.error("CloudInference auth failure: %s", exc)
                return (
                    "CLARIFY cloud auth failed: AWS_BEARER_TOKEN_BEDROCK is missing or invalid. "
                    "Set a valid Amazon Bedrock API key and restart the agent."
                )
            # Raw SDK/network error stays in the log; the user hears a stable
            # sentence rather than leaked internals (E16).
            log.error("CloudInference failed: %s", exc)
            return "CLARIFY the cloud service had an error. Please try again."


class InferenceRunner:
    _CLOUD_TIMEOUT_S = 10.0  # circuit-breaker: cloud inference must complete within this window

    def __init__(
        self,
        cfg: "CoordinatorConfig",
        *,
        local: Callable[[], Any],
        cloud: Callable[[], Optional[_CloudInference]],
        trainer: Callable[[], Any],
        agent_db: Callable[[], Any],
        content_filter: Callable[[], Any],
        rate_limiter: Callable[[], Any],
        note_cloud_call: Callable[[], None],
    ) -> None:
        self._cfg = cfg
        self._local = local
        self._cloud = cloud
        self._trainer = trainer
        self._agent_db = agent_db
        self._content_filter = content_filter
        self._rate_limiter = rate_limiter
        self._note_cloud_call = note_cloud_call

    def effective_cfg(self) -> "CoordinatorConfig":
        """Return the task-local effective config set by _route_impl, or self._cfg.

        Mirrors GateEvaluator.effective_cfg() — same ContextVar, so pain-day
        threshold relaxations (local_timeout_s) and cfg reads
        (anthropic_model) see the same effective config gate evaluation used.
        """
        return _EFFECTIVE_CFG.get() or self._cfg

    async def run_local(self, cmd: "Command", cfg: "CoordinatorConfig | None" = None) -> str:
        effective = cfg if cfg is not None else self.effective_cfg()
        trainer = self._trainer()
        examples = (
            await trainer.get_few_shot_examples(cmd)
            if trainer else None
        )
        counterexamples = (
            await trainer.get_few_shot_counterexamples(cmd)
            if trainer else None
        )
        # Clear the task-local capture so a backend that doesn't set it (or a
        # mocked infer in tests) can't surface a previous command's prompt.
        set_inference_capture(None)
        t0 = time.monotonic()
        local = self._local()
        # Circuit-breaker: a wedged local backend (Ollama hung, GPU stuck during
        # a flare, stalled model reload) must not stall the pipeline forever.
        # On timeout, degrade to CLARIFY rather than blocking the accessibility
        # path indefinitely. Mirrors the cloud-path guard in run_cloud().
        try:
            async with asyncio.timeout(effective.local_timeout_s):  # type: ignore[attr-defined]
                action_str = await local.infer(
                    cmd, few_shot_examples=examples, counterexamples=counterexamples
                )
        except TimeoutError:
            log.error(
                "InferenceRunner: local inference timed out after %.0fs — CLARIFY fallback",
                effective.local_timeout_s,
            )
            return "CLARIFY local inference timed out"
        latency_ms = (time.monotonic() - t0) * 1000
        # NOTE: Do NOT update the latency EMA here — route()'s finally block
        # already updates it with the total route latency (which includes
        # inference). Doing it here too would double-count inference time in
        # Gate 4's EMA.
        agent_db = self._agent_db()
        if agent_db and agent_db.available:
            status = local.get_status()
            error = action_str if action_str.startswith("CLARIFY inference") else None
            _prompt, _tokens_in, _tokens_out = get_inference_capture()
            _inf_id = await agent_db.inferences.insert_inference(
                command_id=None,   # backfilled by route() via link_inferences_to_command
                model=status.get("model", "unknown"),
                domain="command",
                prompt=_prompt,
                response=action_str,
                tokens_in=_tokens_in,
                tokens_out=_tokens_out,
                latency_ms=latency_ms,
                backend=status.get("backend", "ollama"),
                error=error,
            )
            _ids = _PENDING_INFERENCE_IDS.get()
            if _ids is not None and _inf_id and _inf_id > 0:
                _ids.append(_inf_id)
        try:
            _st = local.get_status()
            _ti, _to = get_inference_capture()[1:]
            get_tracer().record_span(
                "inference", route="local", backend=_st.get("backend"),
                model=_st.get("model"), dur_ms=round(latency_ms, 1),
                tokens_in=_ti, tokens_out=_to,
                cost_usd=estimate_cost(_st.get("model", ""), _ti or 0, _to or 0) or None,
            )
        except Exception:
            pass
        return action_str

    async def run_cloud(self, cmd: "Command", cfg: "CoordinatorConfig | None" = None) -> str:
        """Route to Anthropic API (Claude).

        Content filter scrubs secrets/PII from the command text before
        transmitting to external APIs. Findings are logged to the audit trail.

        A 10-second asyncio.timeout acts as a circuit-breaker: if the API hangs
        (network drop, service hiccup) the coroutine is cancelled and the
        pipeline degrades to a CLARIFY response rather than stalling indefinitely.
        """
        self._note_cloud_call()  # GAP-10: denial-of-wallet tripwire
        # Scrub secrets before sending to external API — from BOTH the command
        # text AND the session_context the cloud prompt embeds. A sensitive
        # prior command that Gate 0 correctly forced local must not leak later
        # inside an innocuous command's context window.
        content_filter = self._content_filter()
        if content_filter:
            clean_text, findings = await content_filter.scrub(cmd.text)
            total = len(findings)
            ctx_overrides: dict = {}
            if cmd.session_context:
                clean_ctx = []
                for line in cmd.session_context:
                    cl, f = await content_filter.scrub(line)
                    clean_ctx.append(cl)
                    total += len(f)
                ctx_overrides["session_context"] = clean_ctx
            if total:
                log.info("ContentFilter: redacted %d secret(s) before cloud call", total)
            # replace() preserves every field (action, source, params, …) and
            # only overrides text/context — the manual rebuild dropped the
            # required `action` and used a nonexistent `_gaze_coords` kwarg.
            if findings or ctx_overrides:
                cmd = _dc_replace(cmd, text=clean_text, **ctx_overrides)

        # Clear the task-local capture so a stale prompt from a previous
        # inference in this task can't be attributed to this cloud call.
        set_inference_capture(None)
        t0 = time.monotonic()
        cloud = self._cloud()
        if cloud is None:
            return "CLARIFY cloud not configured"
        try:
            async with asyncio.timeout(self._CLOUD_TIMEOUT_S):  # type: ignore[attr-defined]
                # Throttle cloud egress INSIDE the timeout (#18): a saturated
                # 'anthropic' bucket otherwise stalled the command path past the
                # advertised budget. Fail-open if no limiter/bucket is configured;
                # shares the bucket with CloudDevAgent.
                rate_limiter = self._rate_limiter()
                if rate_limiter is not None:
                    await rate_limiter.check("anthropic")
                action_str = await cloud.infer(cmd)
        except TimeoutError:
            log.error(
                "InferenceRunner: cloud inference timed out after %.0fs — CLARIFY fallback",
                self._CLOUD_TIMEOUT_S,
            )
            # Record the timed-out call as a span too (was previously invisible).
            effective = cfg if cfg is not None else self.effective_cfg()
            try:
                get_tracer().record_span(
                    "inference", route="cloud", model=effective.anthropic_model,
                    dur_ms=round((time.monotonic() - t0) * 1000, 1), status="timeout",
                )
            except Exception:
                pass
            return "CLARIFY cloud inference timed out"
        effective = cfg if cfg is not None else self.effective_cfg()
        latency_ms = (time.monotonic() - t0) * 1000
        # Capture token usage once: shared by the trace span (cost-per-step in the
        # replay timeline) and the durable inferences row below.
        _prompt, _tokens_in, _tokens_out = get_inference_capture()
        try:
            get_tracer().record_span(
                "inference", route="cloud", model=effective.anthropic_model,
                dur_ms=round(latency_ms, 1),
                tokens_in=_tokens_in, tokens_out=_tokens_out,
                cost_usd=estimate_cost(effective.anthropic_model, _tokens_in or 0, _tokens_out or 0) or None,
            )
        except Exception:
            pass
        agent_db = self._agent_db()
        if agent_db and agent_db.available:
            error = action_str if action_str.startswith("CLARIFY") else None
            _inf_id = await agent_db.inferences.insert_inference(
                command_id=None,   # backfilled by route() via link_inferences_to_command
                model=effective.anthropic_model,
                domain="command",
                prompt=_prompt,
                response=action_str,
                tokens_in=_tokens_in,
                tokens_out=_tokens_out,
                latency_ms=latency_ms,
                backend="anthropic",
                error=error,
            )
            _ids = _PENDING_INFERENCE_IDS.get()
            if _ids is not None and _inf_id and _inf_id > 0:
                _ids.append(_inf_id)
        return action_str
