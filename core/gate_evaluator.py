"""GateEvaluator — Gate 0-4 routing decisions extracted from HybridCoordinator.

Gate logic (source-dependent):
  touch / multimodal                → bypass all 4 gates → local
  voice_local                       → skip Gate 1 → gates 2-4
  gesture / voice                   → full 4-gate evaluation

Gate 0 — Privacy:     command text contains no sensitive-data patterns
Gate 1 — Confidence:  whisper_logprob >= min AND gesture_conf >= min
Gate 2 — Complexity:  token_count <= max AND no complexity keywords
Gate 3 — VRAM:        vram_free_gb >= vram_free_min_gb  (via pynvml)
Gate 4 — Latency EMA: latency_ema_ms <= latency_budget_ms

See core/hybrid_coordinator.py module docstring for the full gate-decision
label reference (_GATE_DECISION_LABELS).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from storage.personal_kb import is_personal_query as _is_personal_query

if TYPE_CHECKING:
    from core.command_executor import Command
    from core.hybrid_coordinator import CoordinatorConfig

log = logging.getLogger(__name__)

# Task-local effective config — set by HybridCoordinator._route_impl before
# gate evaluation so concurrent route() tasks each see their own
# pain-day-adjusted config without mutating a shared self._cfg (#4). A
# ContextVar (not a method arg threaded through every call) keeps gate/
# inference signatures stable, so test monkey-patches like
# `coord._run_local = fake` keep working. Falls back to the instance's own
# self._cfg when unset (e.g. the dev pre-gate, which always runs with the
# base config, before this is set).
_EFFECTIVE_CFG: ContextVar[Optional["CoordinatorConfig"]] = ContextVar(
    "da_effective_cfg", default=None
)

_COMPLEXITY_KEYWORDS = (
    "and then", "after that", "for each", "followed by",
    "then open", "then click", "then type", "while", "repeat",
)


class GateEvaluator:
    """Owns Gate 0-4 decision state (latency EMA, VRAM cache, cloud-call budget).

    Gates 2-4 dispatch to inference on pass/fail, so ``run_local``/``run_cloud``
    are injected rather than referencing HybridCoordinator directly — this
    module has no back-reference to the coordinator.
    """

    def __init__(
        self,
        cfg: "CoordinatorConfig",
        *,
        run_local: Callable[["Command"], Awaitable[str]],
        run_cloud: Callable[["Command"], Awaitable[str]],
        approval_config: Callable[[], dict],
        audit=None,
        tts_speak: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._cfg = cfg
        self._run_local = run_local
        self._run_cloud = run_cloud
        self._approval_config = approval_config
        self._audit = audit
        self._tts_speak = tts_speak

        self.latency_ema: Optional[float] = None

        # Gate 3 VRAM cache — avoid calling pynvml on every command
        self._vram_cache: tuple[bool, float] | None = None  # (result, monotonic_time)
        self._vram_cache_ttl: float = 2.0  # seconds
        # Hard bound on the NVML probe await (E9): a wedged driver must not stall
        # the command path. Generous vs. a healthy probe (~ms), tight vs. a hang.
        self._vram_probe_timeout_s: float = 1.0

        # GAP-10: denial-of-wallet tripwire. Count cloud API calls this session
        # and speak a one-time warning once they exceed cloud_call_budget.
        self._session_cloud_calls: int = 0
        self._cloud_budget_warned: bool = False

    # ---------------------------------------------------------------------- #
    # Gates
    # ---------------------------------------------------------------------- #

    def effective_cfg(self) -> "CoordinatorConfig":
        """Return the task-local effective config set by _route_impl, or self._cfg.

        This allows pain-day threshold relaxations to propagate to all gate
        and inference helpers without passing a cfg parameter through the
        entire call chain (which would break test monkey-patches).
        """
        return _EFFECTIVE_CFG.get() or self._cfg

    def gate0(self, cmd: "Command", cfg: "CoordinatorConfig | None" = None) -> bool:
        """Gate 0 — Privacy. True = pass (safe to consider cloud).

        Checks command text AND cmd.params["text"] (typed/dictated content)
        against patterns for credentials, financial data, and PII. A match
        forces local routing regardless of subsequent gates (#2).

        `cfg` should be the effective config for this route() invocation;
        defaults to self._cfg when omitted (used from the dev pre-gate,
        which always runs with the base config).
        """
        effective = cfg if cfg is not None else self.effective_cfg()
        # Personal-document queries always stay local — this covers the COMMAND
        # path too (e.g. "open my notes about <topic>" classifies as command,
        # where Gates 1-4 could otherwise escalate the text to cloud). Checked
        # before the enabled flag: disabling keyword Gate-0 must not disable it.
        if _is_personal_query(cmd.text):
            return False
        if not effective.gate0_enabled:
            return True
        patterns = effective.gate0_sensitive_patterns
        text = cmd.text.lower()
        if any(pat in text for pat in patterns):
            return False
        # Also scan typed/dictated content in cmd.params["text"] so a
        # "TYPE my password is hunter2" with the secret in params doesn't
        # escape to cloud via a split text/params payload (#2).
        param_text = (cmd.params.get("text") or "").lower() if cmd.params else ""
        if param_text and any(pat in param_text for pat in patterns):
            return False
        return True

    async def gate1(
        self, cmd: "Command", cfg: "CoordinatorConfig | None" = None
    ) -> tuple[bool | None, "Command"]:
        """Gate 1 — Confidence.

        Returns:
          (True,  cmd) — pass
          (False, cmd) — fail-voice (low whisper logprob)
          (None,  cmd) — fail-gesture (discard)
        """
        cfg = cfg if cfg is not None else self.effective_cfg()
        logprob_ok = cmd.whisper_logprob >= cfg.whisper_logprob_min
        gesture_ok = cmd.gesture_confidence >= cfg.gesture_confidence_min

        if logprob_ok and gesture_ok:
            return True, cmd

        # Distinguish gesture failure (source=gesture) vs voice failure
        if cmd.source == "gesture" and not gesture_ok:
            return None, cmd  # discard

        # Voice low confidence — signal for re-transcription
        log.debug(
            "Gate 1 fail (voice): logprob=%.3f gesture=%.3f",
            cmd.whisper_logprob,
            cmd.gesture_confidence,
        )
        return False, cmd

    async def gates_2_to_4(
        self, cmd: "Command", cfg: "CoordinatorConfig | None" = None
    ) -> tuple[str, str, str]:
        """Run Gates 2-4. Returns (action_str, gate_that_decided, route_label)."""
        effective = cfg if cfg is not None else self.effective_cfg()
        # Gate 2 — Complexity
        if not self.gate2(cmd, effective):
            log.debug("Gate 2 fail (complexity): %r", cmd.text)
            return await self._run_cloud(cmd), "gate2_complexity", "cloud"

        # Gate 3 — VRAM
        if not await self.gate3():
            log.debug("Gate 3 fail (VRAM)")
            return await self._run_cloud(cmd), "gate3_vram", "cloud"

        # Gate 4 — Latency EMA
        if not self.gate4(effective):
            log.debug(
                "Gate 4 fail (latency EMA=%.0f ms)", self.latency_ema or 0
            )
            return await self._run_cloud(cmd), "gate4_latency", "cloud"

        return await self._run_local(cmd), "all_pass", "local"

    def gate2(self, cmd: "Command", cfg: "CoordinatorConfig | None" = None) -> bool:
        """Gate 2 — Complexity. True = pass (route local)."""
        effective = cfg if cfg is not None else self.effective_cfg()
        text = cmd.text.lower()
        if any(kw in text for kw in _COMPLEXITY_KEYWORDS):
            return False
        token_count = len(cmd.text.split())
        return token_count <= effective.max_local_tokens

    async def gate3(self) -> bool:
        """Gate 3 — VRAM. True = pass.

        Caches the result for 2 seconds to avoid probing the GPU on every command.
        The probe (pynvml: nvmlInit/GetMemoryInfo/nvmlShutdown) is a blocking CUDA
        driver round-trip, so on a cache miss it runs in a thread — never on the
        event loop, where it would stall every concurrent accessibility/voice
        task for the duration of the probe (#6).
        """
        now = time.monotonic()
        if self._vram_cache is not None:
            cached_result, cached_time = self._vram_cache
            if now - cached_time < self._vram_cache_ttl:
                return cached_result

        try:
            from core import vram
            # vram.free_vram_gb returns UNKNOWN_FREE_GB (fail-open) if pynvml is
            # unavailable, so an unmeasurable GPU still passes (don't penalise local).
            # Bound the await (E9): a wedged NVML/CUDA driver can hang the worker
            # thread, and asyncio.to_thread is not cancellable — without the
            # wait_for, this route() task would block indefinitely. On timeout the
            # orphaned thread keeps running but we fail-open and move on.
            probe_timeout = self._vram_probe_timeout_s
            free_gb = await asyncio.wait_for(
                asyncio.to_thread(vram.free_vram_gb), timeout=probe_timeout
            )
            result = free_gb >= self._cfg.vram_free_min_gb  # gate3 always uses self._cfg (no effective_cfg needed)
        except TimeoutError:
            log.warning("Gate 3 VRAM probe timed out after %.1fs — assuming pass",
                        self._vram_probe_timeout_s)
            result = True
        except Exception as exc:
            log.debug("Gate 3 VRAM probe error (assuming pass): %s", exc)
            result = True

        self._vram_cache = (result, now)
        return result

    def gate4(self, cfg: "CoordinatorConfig | None" = None) -> bool:
        """Gate 4 — Latency EMA. True = pass.

        Budget is sourced per-domain via the SLO layer (gap H); the gated path is
        the command domain, so this resolves to the command budget (600 ms default)
        unless the trainer has set a per-domain override.
        """
        effective = cfg if cfg is not None else self.effective_cfg()
        if self.latency_ema is None:
            return True  # no history yet — optimistically run local
        return self.latency_ema <= effective.latency_budget_for("command")

    def update_ema(self, latency_ms: float) -> None:
        alpha = self._cfg.latency_ema_alpha
        if self.latency_ema is None:
            self.latency_ema = latency_ms
        else:
            self.latency_ema = alpha * latency_ms + (1 - alpha) * self.latency_ema

    # ---------------------------------------------------------------------- #
    # Cloud-call budget (GAP-10 denial-of-wallet tripwire)
    # ---------------------------------------------------------------------- #

    def cloud_call_budget(self) -> int:
        try:
            return int(self._approval_config().get("cloud_call_budget", 20))
        except Exception:
            return 20

    def note_cloud_call(self) -> None:
        """GAP-10: count cloud API calls per session; warn once past the budget.

        Denial-of-wallet tripwire — slow loops under the per-call cap can still
        accumulate significant cost. Advisory (warn + audit, fire-and-forget): it
        does NOT hard-block the cloud path, so a legitimate long session is never
        wedged mid-task. The counter resets each session.
        """
        self._session_cloud_calls += 1
        budget = self.cloud_call_budget()
        if self._session_cloud_calls <= budget or self._cloud_budget_warned:
            return
        self._cloud_budget_warned = True
        log.warning(
            "GateEvaluator: cloud-call budget exceeded (%d > %d) — DoW tripwire",
            self._session_cloud_calls, budget,
        )
        from core.async_utils import fire_and_log
        if self._audit and getattr(self._audit, "available", False):
            fire_and_log(
                self._audit.log_security_event(
                    detail=(f"denial-of-wallet tripwire: {self._session_cloud_calls} "
                            f"cloud calls this session (budget {budget})"),
                    severity="warning",
                    params={"cloud_calls": self._session_cloud_calls, "budget": budget}),
                log, label="audit dow")
        msg = (f"Heads up — this session has made {self._session_cloud_calls} cloud "
               "calls, over the budget. Keeping an eye on cost.")
        if self._tts_speak is not None:
            fire_and_log(self._tts_speak(msg), log, label="dow warning")
