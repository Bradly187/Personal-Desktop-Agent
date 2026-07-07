"""CloudPlanRouter — route the agentic planner's ``domain="plan"`` inference to
Claude Sonnet 4.6 via Amazon Bedrock, while keeping execution (and the offline
fallback) on the local PC.

Why this exists (specs/cloud-plan-routing)
------------------------------------------
``DevAgent.plan_and_run`` generates its executable step array with
``ModelRouter.infer(domain="plan")`` → ``qwen3-coder:30b`` (~18 GB). On the 32 GB
RTX 5090, with the command + general models already resident on top of the
baseline + Whisper floor, that plan model cannot co-reside — it forces a
~50–60 s evict/reload swap (the "VRAM budget exceeded" thrash). Plan generation is
off the 60 Hz path, one-shot, infrequent, and quality-sensitive, so it is the
ideal thing to offload: Sonnet 4.6 is a stronger planner, ~half Opus cost, and
frees the 18 GB entirely.

This is a TRANSPARENT PROXY around the real ``ModelRouter``: it intercepts ONLY
``infer(domain="plan")`` and serves it from Bedrock; every other domain and every
other method (``select_profile``, ``edit_format_for``, ``infer_stream``,
``analyse_screen``, ``get_status``, …) delegates unchanged via ``__getattr__``.

Distinct from ``inference/cloud_dev_agent.py``: that routes dev-domain *answer*
queries to the cloud at the ``HybridCoordinator`` layer and returns early with no
execution. This keeps the full agentic loop and swaps only the planner's brain.

Safety (AGENTS.md #4, #7)
-------------------------
- **Fail-safe:** missing SDK / unset credential / un-granted model / network or
  API error / schema-invalid output → transparent fallback to the wrapped local
  ``infer("plan")``. The agent behaves exactly as today; offline planning still
  works. The local plan model is never removed.
- **Privacy:** the plan context carries the user's source (repo facts, the git
  working-tree diff, RAG snippets, file paths). Both the goal and the context are
  scrubbed via ``ContentFilter`` before egress; a **critical** finding (private
  key / API key / cloud credential) forces the LOCAL path for that call.
- **No execution:** pure inference — it emits a plan, never an action.

Structured output
------------------
Reuses ``model_router._PLAN_PROMPT`` (verbs + ``after`` semantics) and
``model_router._PLAN_JSON_SCHEMA`` (the ``{"steps":[…]}`` contract
``DevAgent._parse_plan_json_report`` consumes) as the single source of truth, so
cloud plan output parses byte-compatibly with the local Ollama ``format`` path.
The schema is enforced with Bedrock forced tool-use (one ``emit_plan`` tool,
``tool_choice`` forced, thinking disabled — forced tool_choice + extended thinking
is not a supported combo).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from inference.model_router import (
    RouterResult,
    _PLAN_JSON_SCHEMA,
    _PLAN_PROMPT,
)

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_PLAN_TOOL_NAME = "emit_plan"


def cloud_plan_enabled() -> bool:
    """True when DA_CLOUD_PLAN is set truthy (default OFF → byte-identical legacy)."""
    return os.environ.get("DA_CLOUD_PLAN", "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


class CloudPlanRouter:
    """Transparent ``ModelRouter`` proxy that serves ``domain="plan"`` from
    Sonnet 4.6 (Bedrock) and delegates everything else to the wrapped router."""

    def __init__(
        self,
        inner: Any,
        *,
        content_filter: Optional[Any] = None,
        agent_db: Optional[Any] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._inner = inner
        self._content_filter = content_filter
        self._agent_db = agent_db
        self._base_model = model or os.environ.get("DA_CLOUD_PLAN_MODEL") or _DEFAULT_MODEL
        self.model = self._base_model  # backend-mapped at client-build time
        try:
            self._timeout = float(
                timeout if timeout is not None
                else os.environ.get("DA_CLOUD_PLAN_TIMEOUT_S", "60")
            )
        except (TypeError, ValueError):
            self._timeout = 60.0
        self._client = None             # lazily-built AsyncAnthropicBedrock
        self._rate_limiter = None       # shared 'anthropic' bucket; wired by coordinator

    # ------------------------------------------------------------------ #
    # Transparent delegation
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes NOT defined on this instance/class, so the
        # overridden infer() and the private attrs above are never shadowed.
        return getattr(self._inner, name)

    def set_rate_limiter(self, limiter) -> None:
        """Share the coordinator's RateLimiter 'anthropic' bucket. Fail-open if
        never wired. Also forwards to the inner router if it accepts one."""
        self._rate_limiter = limiter
        fwd = getattr(self._inner, "set_rate_limiter", None)
        if callable(fwd):
            try:
                fwd(limiter)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Plan intercept
    # ------------------------------------------------------------------ #

    async def infer(
        self,
        domain: str,
        user_text: str,
        screenshot_b64: Optional[str] = None,
        context: Optional[str] = None,
    ) -> "RouterResult":
        # R1.2: only plan is intercepted; everything else is the local router.
        if domain != "plan":
            return await self._inner.infer(domain, user_text, screenshot_b64, context)

        # R3.1/R3.2: scrub goal + context; a critical finding forces local.
        clean_text, clean_ctx, force_local = await self._scrub_for_cloud(user_text, context)
        if force_local:
            log.info("CloudPlanRouter: critical secret in plan context — forcing LOCAL plan")
            return await self._inner.infer(domain, user_text, screenshot_b64, context)

        t0 = time.monotonic()
        try:
            text, usage = await self._cloud_plan(clean_text, clean_ctx)
        except Exception as exc:
            # R2.1: any failure → transparent local fallback.
            log.info("CloudPlanRouter: cloud plan unavailable (%s) — falling back to local", exc)
            return await self._inner.infer(domain, user_text, screenshot_b64, context)

        latency_ms = (time.monotonic() - t0) * 1000
        await self._record_cost(usage, latency_ms)
        log.info("CloudPlanRouter: plan via %s (%.0f ms, %d chars)",
                 self.model, latency_ms, len(text))
        return RouterResult(
            text=text,
            model=self.model,
            domain="plan",
            latency_ms=latency_ms,
            free_form=True,
            backend="bedrock",
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _scrub_for_cloud(
        self, user_text: str, context: Optional[str]
    ) -> tuple[str, Optional[str], bool]:
        """Return (clean_text, clean_context, force_local).

        Scrubs both fields via ContentFilter. force_local is True when ANY finding
        is critical (private key / API key / cloud credential) — we never send
        even-redacted critical-secret context to the cloud (R3.2). No filter wired
        → pass through unchanged (the caller still benefits from the VRAM win).
        """
        if self._content_filter is None:
            return user_text, context, False
        critical = False
        try:
            clean_text, f1 = await self._content_filter.scrub(user_text or "")
            clean_ctx: Optional[str] = context
            f2: list = []
            if context:
                clean_ctx, f2 = await self._content_filter.scrub(context)
            critical = any(
                getattr(f, "severity", "warning") == "critical" for f in (*f1, *f2)
            )
            if (f1 or f2) and not critical:
                log.info("CloudPlanRouter: redacted %d secret(s) before cloud plan",
                         len(f1) + len(f2))
            return clean_text, clean_ctx, critical
        except Exception as exc:
            # A scrub error must not silently leak — fail safe to LOCAL.
            log.warning("CloudPlanRouter: scrub failed (%s) — forcing LOCAL plan", exc)
            return user_text, context, True

    def _get_client(self):
        """Lazily construct the AsyncAnthropicBedrock client (raises if unavailable)."""
        if self._client is None:
            try:
                import anthropic  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("anthropic SDK not installed") from exc
            from core.cloud_backend import resolve_backend
            backend = resolve_backend()  # raises actionable msg if credential missing
            self._client = backend.make_client(async_=True, timeout=self._timeout)
            self.model = backend.map_model(self._base_model)
        return self._client

    async def _cloud_plan(
        self, goal: str, context: Optional[str]
    ) -> tuple[str, Any]:
        """Call Sonnet 4.6 with forced tool-use; return (plan_json_text, usage).

        Raises on any failure (caller falls back to local). A response without an
        ``emit_plan`` tool_use block is treated as schema-invalid (also raises).
        """
        client = self._get_client()

        # Best-effort egress throttle (shared 'anthropic' bucket); fail-open.
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.check("anthropic")
            except Exception as exc:
                log.debug("CloudPlanRouter rate-limit check skipped: %s", exc)

        user_text = goal if not context else f"Context:\n{context}\n\n[Goal]\n{goal}"
        tool = {
            "name": _PLAN_TOOL_NAME,
            "description": "Emit the executable action plan as a structured step array.",
            "input_schema": _PLAN_JSON_SCHEMA,
        }
        message = await client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=_PLAN_PROMPT,
            tools=[tool],
            tool_choice={"type": "tool", "name": _PLAN_TOOL_NAME},
            messages=[{"role": "user", "content": user_text}],
        )
        plan_input = None
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == _PLAN_TOOL_NAME:
                plan_input = getattr(block, "input", None)
                break
        if not isinstance(plan_input, dict) or "steps" not in plan_input:
            raise ValueError("cloud plan response missing emit_plan tool input")
        # json.dumps → the exact {"steps":[…]} text _parse_plan_json_report expects.
        return json.dumps(plan_input), getattr(message, "usage", None)

    async def _record_cost(self, usage: Any, latency_ms: float) -> None:
        """Persist Bedrock token usage to the cost ledger (R4.1). Best-effort."""
        if not (self._agent_db and getattr(self._agent_db, "available", False)):
            return
        try:
            await self._agent_db.inferences.insert_inference(
                command_id=None,
                model=self.model,
                domain="plan",
                prompt=None,
                response=None,
                tokens_in=getattr(usage, "input_tokens", None) if usage else None,
                tokens_out=getattr(usage, "output_tokens", None) if usage else None,
                latency_ms=latency_ms,
                backend="bedrock",
                error=None,
            )
        except Exception as exc:
            log.debug("CloudPlanRouter: cost-ledger insert skipped: %s", exc)
