"""Multi-agent orchestration — fan-out + adversarial-verify over the resident model.

Phase 4 of the Claude-Code capability-gap closure (specs/workflow-orchestration).
Claude Code's Workflow tool fans work out to N concurrent sub-agents and verifies
findings before committing them. This brings a bounded version of that to the
agent.

**Why fresh-context inference, not full plan loops.** ``DevAgent.plan_and_run`` is
serialized by an instance ``_plan_lock`` (interleaved plans would answer each
other's confirmations), and the single RTX-5090 model serializes inference at the
Ollama layer anyway — so fanning out full plan loops would neither parallelize nor
stay safe. Instead a sub-agent here is a single **fresh-context** inference call
(``ModelRouter.infer(domain, context="")`` — the exact Critic/Tester pattern,
AGENTS.md #6): multiple focused prompts to the already-loaded model, orchestrated
concurrently via the ``AccessibilityScheduler``'s ``fan_out`` (its own
sub-agent semaphore, deadlock-free). **No new model is loaded.**

**Guardrails.** Experimental + **OFF by default** (``workflow_orchestration.enabled``
in ``~/.claude/ipad_bridge/config.json``; an explicit ``enabled=`` arg overrides
for tests). **Skip-on-flare** (AGENTS.md #5) — a wired flare check short-circuits
to a no-op ``skipped_flare`` result. Pure orchestration: it issues inference and
optionally an adversarial verify pass, but executes **no** desktop actions / file
writes / shell (no new bypass of the command pipeline's gates). Best-effort DB
journaling to ``agent_workflows`` never breaks a run. Never runs on the 60 Hz path
(AGENTS.md #2) — it's an offline/heavy primitive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

_CONFIG_PATH = Path.home() / ".claude" / "ipad_bridge" / "config.json"


def _config_enabled() -> bool:
    """Read ``workflow_orchestration.enabled`` (default False — ship dark)."""
    try:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return bool(cfg.get("workflow_orchestration", {}).get("enabled", False))
    except (OSError, ValueError):
        pass
    return False


@dataclass
class SubTask:
    """One unit of fan-out work: a focused prompt answered in a fresh context."""
    name: str
    prompt: str


@dataclass
class Stage:
    """One stage of a :meth:`WorkflowRunner.pipeline` chain.

    ``build_prompt(item, prior_texts) -> prompt`` produces this stage's prompt
    from the original item and the ordered list of prior stages' output **texts**.
    The prior text is threaded into the *prompt* — never the model context — so
    every stage stays a fresh-context inference call (AGENTS.md #6)."""
    name: str
    build_prompt: Callable[[str, list[str]], str]


@dataclass
class SubResult:
    name: str
    ok: bool
    text: str = ""
    error: str = ""
    verified: Optional[bool] = None   # None = no verify pass ran


@dataclass
class WorkflowResult:
    name: str
    status: str                       # completed | skipped_flare | disabled | error
    results: list[SubResult] = field(default_factory=list)
    subtask_count: int = 0
    success_count: int = 0
    verified_count: Optional[int] = None
    latency_ms: float = 0.0
    error: str = ""


class WorkflowRunner:
    """Orchestrates concurrent fresh-context sub-agents over the resident model."""

    def __init__(
        self,
        router,
        *,
        scheduler=None,
        agent_db=None,
        flare_check: Optional[Callable[[], bool]] = None,
        enabled: Optional[bool] = None,
        domain: str = "general",
    ) -> None:
        self._router = router
        self._scheduler = scheduler
        self._agent_db = agent_db
        self._flare_check = flare_check
        # enabled: explicit arg wins (tests); else the config flag (default OFF).
        self._enabled = _config_enabled() if enabled is None else bool(enabled)
        self._domain = domain

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _should_skip_flare(self) -> bool:
        """True if a flare is active (AGENTS.md #5). A check that errors fails
        safe to SKIP — orchestration is non-essential heavy work."""
        if self._flare_check is None:
            return False
        try:
            return bool(self._flare_check())
        except Exception:
            return True

    async def fan_out(
        self,
        subtasks: list[SubTask],
        *,
        name: str = "fanout",
        goal: Optional[str] = None,
        verify_criterion: Optional[str] = None,
        verify_domain: str = "plan",
    ) -> WorkflowResult:
        """Run ``subtasks`` concurrently; optionally adversarially verify each.

        Returns a :class:`WorkflowResult`. ``verify_criterion`` (if given) runs a
        second fresh-context reviewer pass per successful sub-result — a YES/NO
        judge that defaults to NOT-verified on any ambiguity/error (fail-safe).
        """
        t0 = time.monotonic()

        if not self._enabled:
            return WorkflowResult(name=name, status="disabled",
                                  subtask_count=len(subtasks))
        if self._should_skip_flare():
            log.info("WorkflowRunner.fan_out(%s): skipped (flare)", name)
            await self._journal(name, goal, "fan_out", len(subtasks), 0, None,
                                 "skipped_flare", t0)
            return WorkflowResult(name=name, status="skipped_flare",
                                  subtask_count=len(subtasks))
        if not subtasks:
            return WorkflowResult(name=name, status="completed")

        try:
            results = await self._run_concurrent(
                [self._infer_one(st) for st in subtasks], label=name)
            # _run_concurrent may surface exceptions as objects (return_exceptions);
            # normalize any to a failed SubResult so the result list aligns 1:1.
            norm: list[SubResult] = []
            for st, r in zip(subtasks, results):
                if isinstance(r, SubResult):
                    norm.append(r)
                else:
                    norm.append(SubResult(st.name, False, error=f"{r}"))

            verified_count = await self._maybe_verify(
                norm, verify_criterion, verify_domain, name)

            success = sum(1 for r in norm if r.ok)
            await self._journal(name, goal, "fan_out", len(subtasks), success,
                                 verified_count, "completed", t0)
            return WorkflowResult(
                name=name, status="completed", results=norm,
                subtask_count=len(subtasks), success_count=success,
                verified_count=verified_count,
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as exc:  # never raise out of orchestration
            log.warning("WorkflowRunner.fan_out(%s) failed: %s", name, exc)
            await self._journal(name, goal, "fan_out", len(subtasks), 0, None,
                                 "error", t0, error=str(exc))
            return WorkflowResult(name=name, status="error",
                                  subtask_count=len(subtasks), error=str(exc),
                                  latency_ms=(time.monotonic() - t0) * 1000)

    async def pipeline(
        self,
        items: list[str],
        stages: list[Stage],
        *,
        name: str = "pipeline",
        goal: Optional[str] = None,
        verify_criterion: Optional[str] = None,
        verify_domain: str = "plan",
    ) -> WorkflowResult:
        """Run each item through ALL ``stages`` in order — staged refinement.

        Per item, stage k's prompt is built from the original item plus the prior
        stages' output texts (``Stage.build_prompt``); each stage is a
        fresh-context ``infer(context="")`` call (AGENTS.md #6 — no new model).
        The final stage's text is the item's :class:`SubResult.text`. Items run
        with **no barrier between stages** (each item's chain is independent); a
        stage error **fails that one item closed** (skips its remaining stages)
        without aborting siblings. Same OFF-default / skip-on-flare / never-raise /
        ``mode="pipeline"`` journaling envelope as :meth:`fan_out`.
        """
        t0 = time.monotonic()

        if not self._enabled:
            return WorkflowResult(name=name, status="disabled",
                                  subtask_count=len(items))
        if self._should_skip_flare():
            log.info("WorkflowRunner.pipeline(%s): skipped (flare)", name)
            await self._journal(name, goal, "pipeline", len(items), 0, None,
                                 "skipped_flare", t0)
            return WorkflowResult(name=name, status="skipped_flare",
                                  subtask_count=len(items))
        if not items or not stages:
            return WorkflowResult(name=name, status="completed",
                                  subtask_count=len(items))

        try:
            labels = [f"item{i}" for i in range(len(items))]
            results = await self._run_concurrent(
                [self._run_pipeline_one(item, stages, lbl)
                 for item, lbl in zip(items, labels)],
                label=name)
            norm: list[SubResult] = []
            for lbl, r in zip(labels, results):
                norm.append(r if isinstance(r, SubResult)
                            else SubResult(lbl, False, error=f"{r}"))

            verified_count = await self._maybe_verify(
                norm, verify_criterion, verify_domain, name)

            success = sum(1 for r in norm if r.ok)
            await self._journal(name, goal, "pipeline", len(items), success,
                                 verified_count, "completed", t0)
            return WorkflowResult(
                name=name, status="completed", results=norm,
                subtask_count=len(items), success_count=success,
                verified_count=verified_count,
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as exc:  # never raise out of orchestration
            log.warning("WorkflowRunner.pipeline(%s) failed: %s", name, exc)
            await self._journal(name, goal, "pipeline", len(items), 0, None,
                                 "error", t0, error=str(exc))
            return WorkflowResult(name=name, status="error",
                                  subtask_count=len(items), error=str(exc),
                                  latency_ms=(time.monotonic() - t0) * 1000)

    # -- internals ----------------------------------------------------------- #

    async def _maybe_verify(
        self, norm: list[SubResult], verify_criterion: Optional[str],
        verify_domain: str, label: str,
    ) -> Optional[int]:
        """Optional adversarial verify pass shared by fan_out + pipeline. Returns
        the confirmed count, or None when no criterion is given. Sets each
        ``SubResult.verified``; any reviewer error → NOT verified (fail-safe)."""
        if not verify_criterion:
            return None

        # Gap 3: Route workflow verify checks to the cloud model for higher capability
        import os
        if os.environ.get("DA_WORKFLOW_VERIFY_CLOUD", "0").strip().lower() in ("1", "true", "yes", "on"):
            verify_domain = "cloud"

        verdicts = await self._run_concurrent(
            [self._verify_one(sr, verify_criterion, verify_domain) for sr in norm],
            label=f"{label}:verify")
        count = 0
        for sr, v in zip(norm, verdicts):
            sr.verified = bool(v) if not isinstance(v, BaseException) else False
            if sr.verified:
                count += 1
        return count

    async def _run_pipeline_one(
        self, item: str, stages: list[Stage], label: str,
    ) -> SubResult:
        """Fold one item through the ordered stages. Fail-closed: the first stage
        error (prompt build, infer raise, or non-ok) returns a failed SubResult and
        skips the remaining stages. The final stage's text is the result text."""
        prior_texts: list[str] = []
        for stage in stages:
            try:
                prompt = stage.build_prompt(item, list(prior_texts))
            except Exception as exc:
                return SubResult(label, False,
                                 error=f"stage {stage.name} prompt: {exc}")
            try:
                r = await self._router.infer(domain=self._domain,
                                             user_text=prompt, context="")
            except Exception as exc:
                return SubResult(label, False, error=f"stage {stage.name}: {exc}")
            if not getattr(r, "ok", True):
                return SubResult(label, False, error=(
                    f"stage {stage.name}: "
                    + (getattr(r, "error", None) or "infer not ok")))
            prior_texts.append(getattr(r, "text", "") or "")
        return SubResult(label, True, text=prior_texts[-1] if prior_texts else "")

    async def _run_concurrent(self, coros: list[Awaitable], *, label: str) -> list:
        """Run coroutines concurrently — via the scheduler's bounded sub-agent
        pool when wired, else a plain gather. Both keep input order and surface
        per-item exceptions rather than aborting the batch."""
        if not coros:
            return []
        if self._scheduler is not None and hasattr(self._scheduler, "fan_out"):
            return await self._scheduler.fan_out(coros, label=label,
                                                 return_exceptions=True)
        return await asyncio.gather(*coros, return_exceptions=True)

    async def _infer_one(self, st: SubTask) -> SubResult:
        try:
            r = await self._router.infer(domain=self._domain, user_text=st.prompt,
                                         context="")
            if not getattr(r, "ok", True):
                return SubResult(st.name, False,
                                 error=getattr(r, "error", None) or "infer not ok")
            return SubResult(st.name, True, text=getattr(r, "text", "") or "")
        except Exception as exc:
            return SubResult(st.name, False, error=str(exc))

    async def _verify_one(self, sub: SubResult, criterion: str, domain: str) -> bool:
        """Adversarial YES/NO judge in a fresh context. Fail-safe: any error or
        ambiguity → NOT verified (never a false-positive confirmation)."""
        if not sub.ok or not sub.text.strip():
            return False
        prompt = (
            f"You are a strict reviewer. Criterion: {criterion}\n\n"
            f"Candidate result:\n{sub.text[:2000]}\n\n"
            "Does the candidate satisfy the criterion? Answer with exactly YES or "
            "NO on the first line."
        )
        try:
            r = await self._router.infer(domain=domain, user_text=prompt, context="")
            if not getattr(r, "ok", True):
                return False
            first = (getattr(r, "text", "") or "").strip().splitlines()
            return bool(first) and first[0].strip().upper().startswith("YES")
        except Exception:
            return False

    async def _journal(self, name, goal, mode, subtask_count, success_count,
                       verified_count, status, t0, error=None) -> None:
        db = self._agent_db
        if db is None or not getattr(db, "available", True):
            return
        try:
            await db.insert_workflow(
                name=name, goal=goal, mode=mode, subtask_count=subtask_count,
                success_count=success_count, verified_count=verified_count,
                status=status, latency_ms=(time.monotonic() - t0) * 1000,
                error=error,
            )
        except Exception as exc:  # journaling is best-effort
            log.debug("WorkflowRunner journal failed: %s", exc)
