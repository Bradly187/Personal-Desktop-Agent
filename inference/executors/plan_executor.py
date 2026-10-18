import asyncio
import logging
import os
import re
import secrets
import time
from typing import Optional

from core.action_lifecycle import (
    ActionIdentity, ActionOutcome, ActionStatus, RegistryFullError,
)
from core.approval_authority import (
    ApprovalResponse, plan_digest, sha256_digest, step_action_digest,
)
from inference.executors.skill_executor import prepare_skill_action, skill_action_digest
from inference.sandbox import (
    WorkTrust,
    combine_work_trust,
    work_trust_from_source,
)
from inference.step_executor import prepare_file_candidate
from core.approval_keywords import classify_confirmation
from core.events import (
    TOPIC_PLAN_GENERATED, TOPIC_DAG_APPROVAL,
    TOPIC_GOAL_DEQUEUED, TOPIC_GOAL_COMPLETED,
)
from inference.edit_format import (
    HASHLINE,
    HASHLINE_PROMPT_INSTRUCTIONS,
    SEARCH_REPLACE_PROMPT_INSTRUCTIONS,
    UDIFF,
    UDIFF_PROMPT_INSTRUCTIONS,
)
from inference.plan_parser import (
    AgentResult, AgentStep, _parse_plan, _parse_plan_json, 
    _parse_plan_json_report, _build_plan_repair_prompt
)


log = logging.getLogger(__name__)


def _taint_from_context(agent, context: object) -> None:
    """Monotonically mark plan work derived from non-empty context as untrusted."""
    if not context:
        return
    agent._active_work_trust = combine_work_trust(
        agent._active_work_trust,
        WorkTrust.AUTONOMOUS_OR_UNTRUSTED,
    )


async def _acquire_plan_steps(agent, goal, plan_result, extra_ctx):
    """Parse the planner response into steps, auto-repairing dropped/empty
    plans (specs/dev-agent-plan-contract R1).

    Structured parse → regex fallback. When the structured parse dropped a
    step or produced nothing AND the regex fallback didn't rescue a full
    plan, re-prompt the planner up to `_plan_repair_max` times with a
    corrective message naming the failure. Returns `(steps, plan_result)`
    where `plan_result` is the final (possibly repaired) planner response so
    the EXPLAIN fail-safe and `_active_plan_model` reflect what actually ran.
    With repair disabled (default) this is the legacy parse path plus a
    WARNING when steps are silently dropped — never a silent skip.
    """
    attempts = 0
    while True:
        report = _parse_plan_json_report(plan_result.text)
        steps = report.steps
        used_regex = False
        if not report.parsed_ok or not steps:
            regex_steps = _parse_plan(plan_result.text)
            if regex_steps:
                steps = regex_steps
                used_regex = True

        need_repair = (
            agent._plan_repair_enabled
            and attempts < agent._plan_repair_max
            and not used_regex
            and bool(report.dropped or not steps)
        )
        if not need_repair:
            if report.dropped and not used_regex:
                log.warning(
                    "DevAgent: plan parse dropped %d step(s): %s",
                    len(report.dropped),
                    "; ".join(f"#{d.index} {d.raw_action or d.reason!r}"
                              for d in report.dropped),
                )
            return steps, plan_result

        attempts += 1
        log.info("DevAgent: plan auto-repair %d/%d — %d dropped, %d parsed",
                 attempts, agent._plan_repair_max, len(report.dropped), len(steps))
        corrective = _build_plan_repair_prompt(report)
        repair_ctx = f"{corrective}\n\n{extra_ctx}" if extra_ctx else corrective
        # The re-infer emits its own inference span (tokens/cost) — R3.2.
        repaired = await agent._router.infer(
            domain="plan", user_text=goal, context=repair_ctx)
        if not repaired.ok:
            log.warning("DevAgent: plan auto-repair inference failed (%s) — "
                        "using prior parse", repaired.error)
            return steps, plan_result
        plan_result = repaired

async def plan_and_run(
    agent, goal: str, trace_id: str = "", seed_context: str = "",
    extra_context: str = "", *,
    work_trust: WorkTrust | None = None,
) -> AgentResult:
    """Decompose a complex goal into steps and execute them sequentially.

    Serialized: plan state (_plan_authorized, _cancel_event, _current_goal,
    step counters, GoalSession) is instance-level, so two interleaved plans
    would answer each other's confirmations and un-cancel each other.

    ``trace_id`` (chat UI) correlates plan.generated / dag.* events to one
    socket; empty for non-chat callers → a fresh trace is minted as before.
    ``seed_context`` (specs/resume-working-memory, Gap C) is an optional stable
    block prepended to the plan context — used to seed a resumed plan with what
    the interrupted run already did. Empty → byte-identical to today (R2.2).
    ``extra_context`` (specs/chat-context-attachments R2.4) is an optional
    per-turn block (e.g. extracted file attachments) prepended ahead of all
    other context. Empty → byte-identical to today.
    """
    async with agent._plan_lock:
        try:
            candidate_trust = work_trust
            if candidate_trust is None:
                candidate_trust = getattr(
                    agent,
                    "_next_work_trust",
                    WorkTrust.AUTONOMOUS_OR_UNTRUSTED,
                )
            agent._next_work_trust = WorkTrust.AUTONOMOUS_OR_UNTRUSTED
            try:
                agent._active_work_trust = WorkTrust(candidate_trust)
            except (TypeError, ValueError):
                agent._active_work_trust = WorkTrust.AUTONOMOUS_OR_UNTRUSTED
            return await agent._plan_and_run_locked(
                goal, trace_id, seed_context, extra_context
            )
        finally:
            # External task cancellation can arrive at any await after upfront
            # approval. Always revoke any unconsumed cursor before releasing the
            # per-plan lock; normal paths already reset and this is idempotent.
            _reset_plan_state(agent)

async def _plan_and_run_locked(
    agent, goal: str, cmd_trace_id: str = "", seed_context: str = "",
    turn_context: str = "",
) -> AgentResult:
    t0 = time.monotonic()
    log.info("DevAgent: planning goal %r", goal[:80])

    # Unified agent-run trace (gap C): one trace_id spans the whole plan.
    # Setting it as the current ContextVar means every awaited descendant —
    # ModelRouter.infer's inference spans, scheduler.fan_out children — attach
    # to THIS trace automatically, reconstructing the run as one tree. Zero
    # cost when DA_TRACE is off (new_trace returns "" and spans no-op).
    from monitoring.trace import get_tracer
    _tracer = get_tracer()
    # Reuse the chat-supplied trace_id (so live DAG events correlate to the
    # originating socket); otherwise mint a fresh one as before.
    trace_id = cmd_trace_id or _tracer.new_trace(kind="plan", goal=goal[:80])
    agent._active_trace_id = trace_id
    _trace_tok = _tracer.set_current(trace_id)
    _tracer.record_span("plan", trace_id=trace_id, goal=goal[:80])

    # Uploaded/extracted turn context can materially shape the initial plan.
    # Approval does not erase that provenance, so terminal work remains tainted.
    _taint_from_context(agent, turn_context)

    # Step 1: Generate plan — inject RAG context + git/IDE context
    extra_ctx = agent._format_context()
    _taint_from_context(agent, extra_ctx)
    # Make registered skills available to the planner — data-driven, no
    # per-feature prompt edit. describe_for_prompt() is "" when no skills load.
    if agent._skill_registry is not None:
        skills_desc = agent._skill_registry.describe_for_prompt()
        if skills_desc:
            _taint_from_context(agent, skills_desc)
            extra_ctx = f"{skills_desc}\n\n{extra_ctx}" if extra_ctx else skills_desc
    rag = await agent._rag_context(goal, n=4)
    if rag:
        _taint_from_context(agent, rag)
        extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag

    # Git context injection (item #8): gives LLM branch/diff awareness
    git_ctx = await agent._git_context()
    if git_ctx:
        _taint_from_context(agent, git_ctx)
        extra_ctx = f"{git_ctx}\n\n{extra_ctx}" if extra_ctx else git_ctx

    # Live repo-context (Gap A): stable workspace facts (AGENTS.md/CLAUDE.md
    # rules, layout, git branch/log) lead the dynamic RAG/git-status block so
    # the planner sees its house rules first. Memoized; None when off (R3.1,
    # R4.4). The dynamic working-tree diff stays in _git_context above (R3.3).
    workspace = agent._workspace_context()
    if workspace:
        _taint_from_context(agent, workspace)
        extra_ctx = f"{workspace}\n\n{extra_ctx}" if extra_ctx else workspace

    # Resume working-memory (Gap C): a caller-supplied seed block describing what
    # an interrupted run already did. Leads the context so the planner recovers
    # rather than restarting. Empty for the normal (non-resume) path (R2.2).
    #
    # Cross-session memory (R4): a crash-resume seed is the most specific memory,
    # so it wins. ONLY when no caller seed is supplied (a fresh task) do we pull
    # compact memory from recent *related* prior runs — mutually exclusive, so we
    # never double-seed. Flag-gated (DA_SESSION_MEMORY, default OFF); '' otherwise
    # → byte-identical to today.
    if not seed_context:
        seed_context = await agent._session_seed_context(goal)
    if seed_context:
        _taint_from_context(agent, seed_context)
        extra_ctx = f"{seed_context}\n\n{extra_ctx}" if extra_ctx else seed_context

    # Per-turn context (specs/chat-context-attachments R2.4): extracted file
    # attachments for THIS message lead all other context so the planner sees
    # them first. Empty for the non-attachment path → byte-identical to today.
    if turn_context:
        extra_ctx = f"{turn_context}\n\n{extra_ctx}" if extra_ctx else turn_context

    # If the plan model uses a structured WRITE_FILE format (hashline/udiff),
    # teach it the format up front so its bodies are edit ops, not whole files
    # (edit-format-aci R3.2 prompt side). Only for those models — whole_file is
    # untouched.
    _plan_fmt = agent._router.edit_format_for(agent._router.select_profile("plan").name)
    if _plan_fmt == HASHLINE:
        extra_ctx = (
            f"{HASHLINE_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
            if extra_ctx else HASHLINE_PROMPT_INSTRUCTIONS
        )
    elif _plan_fmt == UDIFF:
        extra_ctx = (
            f"{UDIFF_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
            if extra_ctx else UDIFF_PROMPT_INSTRUCTIONS
        )

    # EDIT_FILE (surgical SEARCH/REPLACE) is available to every plan model
    # regardless of its WRITE_FILE knob, so teach the verb unconditionally —
    # the planner should prefer it for targeted changes to existing files and
    # reserve WRITE_FILE for new/whole-file rewrites (specs/edit-format-aci R5).
    extra_ctx = (
        f"{SEARCH_REPLACE_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
        if extra_ctx else SEARCH_REPLACE_PROMPT_INSTRUCTIONS
    )


    # Assumptions (Gap 1): Ask the planner to explicitly state its assumptions about repo/system state.
    if os.environ.get("DA_PLAN_ASSUMPTIONS", "0").strip().lower() in ("1", "true", "yes", "on"):
        assump = "List any assumptions you are making about the codebase or system state in the `assumptions` array."
        extra_ctx = f"{assump}\n\n{extra_ctx}" if extra_ctx else assump

    plan_result = await agent._router.infer(
        domain="plan",
        user_text=goal,
        context=extra_ctx,
    )
    if not plan_result.ok:
        _tracer.record_span("plan_done", trace_id=trace_id, status="plan_error")
        _tracer.reset_current(_trace_tok)
        return AgentResult(
            goal=goal, domain="plan",
            model_used=plan_result.model,
            success=False,
            error=plan_result.error,
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )

    # Record which model produced the plan so WRITE_FILE steps apply the
    # edit format configured for it (specs/edit-format-aci R3.2).
    agent._active_plan_model = plan_result.model

    # Prefer structured JSON (Ollama `format` on the plan profile) — it
    # eliminates the free-text body-collision / arg-truncation bugs. Fall
    # back to the regex parser when JSON parsing fails (older Ollama /
    # vLLM / remote backends that don't honor `format`). When auto-repair is
    # enabled (specs/dev-agent-plan-contract), a dropped/empty plan is
    # re-prompted instead of silently degraded; `plan_result` may be replaced
    # by the repaired response.
    steps, plan_result = await _acquire_plan_steps(agent, goal, plan_result, extra_ctx)
    if not steps:
        # Planner returned neither valid JSON nor a parseable plan, and repair
        # (if any) didn't recover one — fail safe: surface the response as a
        # single read-only EXPLAIN, never a guessed action (R1.5).
        steps = [AgentStep(action="EXPLAIN", body=plan_result.text)]

    log.info("DevAgent: plan has %d steps", len(steps))

    assumptions = []
    if os.environ.get("DA_PLAN_ASSUMPTIONS", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            import json
            start = plan_result.text.find("{")
            end = plan_result.text.rfind("}")
            if start != -1 and end != -1:
                plan_obj = json.loads(plan_result.text[start:end+1])
                if isinstance(plan_obj, dict):
                    assumptions = plan_obj.get("assumptions", [])
        except Exception:
            pass

    # Stable plan ordinals are part of each destructive action digest.  Runtime
    # execution preserves these for the ordinary plan; replans get their own
    # identities when admitted into the remaining queue.
    for ordinal, planned_step in enumerate(steps[: agent.MAX_STEPS], 1):
        planned_step.step_num = ordinal

    # Upfront plan approval gate: speak summary → voice yes/no.
    # "denied" ABORTS the plan — an explicit "no" (or fail-safe DENY on a
    # destructive plan) must stop every step, not just the three git verbs.
    # "approved" authorizes all steps; "auto" (read-only convenience grant)
    # runs the plan but leaves _plan_authorized False so any destructive
    # step a later replan injects still requires per-op confirmation.
    verdict = await agent._approve_plan_upfront( goal, steps, assumptions=assumptions)
    if verdict is True:        # legacy bool contract (tests / older callers)
        verdict = "approved"
    elif verdict is False:
        verdict = "denied"
    if verdict == "denied":
        log.info("DevAgent: plan REJECTED by user — aborting before execution")
        _reset_plan_state(agent)
        _tracer.record_span("plan_done", trace_id=trace_id, status="rejected")
        _tracer.reset_current(_trace_tok)
        return AgentResult(
            goal=goal, domain="plan",
            model_used=plan_result.model,
            success=False,
            error="Plan rejected by user",
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
    # Compatibility fields remain observable in Phase 5, but only the typed
    # grant can authorize execution.  A mocked/legacy string verdict without a
    # grant therefore cannot silently become blanket authority.
    agent._plan_authorized = (
        verdict == "approved" and agent._approval_grant is not None
    )
    agent._approved_verbs = frozenset(
        s.action.upper() for s in steps[: agent.MAX_STEPS]
    ) if agent._plan_authorized else frozenset()
    agent._cancel_event.clear()
    agent._current_goal = goal
    agent._total_steps = min(len(steps), agent.MAX_STEPS)
    agent._current_step = 0
    agent._escalated_this_run = False
    agent._rollback_summary = None

    # Live DAG: publish the approved plan as a node/edge graph and map each
    # step object to its 1-based plan position so dag.* events line up with
    # the deps edges. No-op when no chat request is in flight.
    _plan_steps = list(steps[: agent.MAX_STEPS])
    agent._step_seq = {id(s): i for i, s in enumerate(_plan_steps, 1)}
    await agent._publish_live(TOPIC_PLAN_GENERATED, {
        "goal": goal[:120],
        "steps": [
            {"n": i, "action": s.action, "args": (s.args or "")[:80], "deps": list(s.deps)}
            for i, s in enumerate(_plan_steps, 1)
        ],
    })

    # Durable ledger: write a 'running' run row now so a crash mid-plan is
    # recoverable (reconciled to 'interrupted' on next startup).
    run_id = await agent._start_run(goal, plan_result.model)

    # Step 2: Closed-loop execution (observe → act → replan-on-failure).
    # A failed step never blindly continues (that compounds errors): the
    # controller asks the planner for a bounded recovery plan, and halts if
    # none is available or the replan budget is spent.
    executed: list[AgentStep] = []
    remaining: list[AgentStep] = list(steps[: agent.MAX_STEPS])
    replans = 0
    cancelled = False
    halted_reason: Optional[str] = None
    interrupted_unknown = False
    compensated = False   # ensure rollback runs exactly once per terminal path

    # Execution strategy:
    #  - If the planner declared step dependencies AND a scheduler is wired,
    #    run the dependency DAG in waves (gap A) — independent steps run
    #    concurrently. On the first failure / cancellation / unmet dep it
    #    hands the remainder back to the sequential loop below (with replan).
    #  - Otherwise, the proven sequential path runs, after fanning out any
    #    leading read-only context steps (gap #1).
    if agent._scheduler is not None and _plan_has_deps(steps):
        cancelled, dag_failed_step = await agent._run_dag_waves( 
            remaining, executed, run_id
        )
        # A DAG-wave failure is handled exactly like a sequential step
        # failure: replan from the failure observation. The tail handed back
        # has the failed step's dependents already pruned, so we never run a
        # step whose precondition failed.
        if dag_failed_step is not None and not cancelled:
            if dag_failed_step.lifecycle_status is ActionStatus.OUTCOME_UNKNOWN:
                interrupted_unknown = True
                halted_reason = (
                    f"interrupted: {dag_failed_step.action} outcome remains unknown "
                    f"(action_id={dag_failed_step.action_id})"
                )
                log.warning("DevAgent: %s", halted_reason)
                # Never compensate or replan while the original destructive
                # worker may still commit its side effect.
                remaining = []
            else:
                recovered = False
                if replans < agent.MAX_REPLANS and not agent._cancel_event.is_set():
                    replans += 1
                    new_remaining = await _try_replan(agent, goal, executed, remaining)
                    if new_remaining is not None:
                        remaining = new_remaining
                        agent._total_steps = len(executed) + len(remaining)
                        recovered = True
                        log.info("DevAgent: replanned after DAG failure %s — "
                                 "%d new step(s) (replan %d/%d)",
                                 dag_failed_step.action, len(remaining),
                                 replans, agent.MAX_REPLANS)
                if not recovered:
                    halted_reason = (
                        f"halted after failed {dag_failed_step.action} (no recovery plan)"
                    )
                    log.warning("DevAgent: %s", halted_reason)
                    await agent._halt_and_compensate(
                        run_id, goal, replans, dag_failed_step.action
                    )
                    compensated = True
                    remaining = []
    else:
        await _gather_readonly_prefix(agent, remaining, executed, run_id)

    while remaining and not cancelled:
        if len(executed) >= agent.MAX_STEPS:
            halted_reason = f"reached MAX_STEPS ({agent.MAX_STEPS})"
            log.warning("DevAgent: %s", halted_reason)
            # Roll back completed side effects — a partial plan halted by the
            # step cap should not leave half-done destructive work.
            _incomplete = await agent._run_compensations(run_id, triggered_by="max_steps")
            await agent._record_escalation(run_id, goal, "max_steps", None, replans,
                                          incomplete=_incomplete)
            compensated = True
            break
        if agent._cancel_event.is_set():
            log.info("DevAgent: plan cancelled at step %d", len(executed) + 1)
            cancelled = True
            break

        step = remaining.pop(0)
        agent._current_step = len(executed) + 1
        step.run_id = run_id
        step.step_num = agent._current_step
        agent._total_steps = len(executed) + 1 + len(remaining)
        log.info("DevAgent: step %d  action=%s  args=%r",
                 agent._current_step, step.action, step.args[:60])

        await agent._emit_step_started(step)
        ok = await agent._run_step_with_retry( step)
        _tracer.record_span("step", trace_id=trace_id, action=step.action, ok=ok)
        executed.append(step)
        await agent._persist_step(run_id, len(executed), step)
        if ok:
            continue

        if step.lifecycle_status is ActionStatus.OUTCOME_UNKNOWN:
            interrupted_unknown = True
            halted_reason = (
                f"interrupted: {step.action} outcome remains unknown "
                f"(action_id={step.action_id})"
            )
            log.warning("DevAgent: %s", halted_reason)
            # No retry, recovery replan, or saga compensation can safely run
            # while the original destructive worker remains unresolved.
            remaining = []
            break

        # Proven failure — try a bounded recovery replan; otherwise halt.
        if replans < agent.MAX_REPLANS and not agent._cancel_event.is_set():
            replans += 1
            new_steps = await _try_replan(agent, goal, executed, remaining)
            if new_steps is not None:
                remaining = new_steps
                agent._total_steps = len(executed) + len(remaining)
                log.info(
                    "DevAgent: replanned after failed %s — %d new step(s) (replan %d/%d)",
                    step.action, len(remaining), replans, agent.MAX_REPLANS,
                )
                continue
        halted_reason = f"halted after failed {step.action} (no recovery plan)"
        log.warning("DevAgent: %s", halted_reason)
        await agent._halt_and_compensate(run_id, goal, replans, step.action)
        compensated = True
        break

    # Cancellation (from the sequential loop above OR from _run_dag_waves)
    # rolls back completed side effects — a cancelled plan must not leave
    # half-done destructive work. Runs once, only if a terminal path above
    # didn't already compensate.
    if cancelled and not compensated:
        # A user cancel is deliberate and does NOT itself escalate. But a
        # compensation that FAILED or was SKIPPED during that rollback (E3/E5)
        # is a durable-integrity problem the human must see — a half-undone
        # destructive plan left in an unknown state. _run_compensations
        # agent-escalates each incomplete rollback (reason 'compensation_failed')
        # to the review queue, so the deliberate cancel stays silent while an
        # incomplete rollback still reaches a human, even when the DB is down.
        await agent._run_compensations(run_id, triggered_by="user_cancel")
        compensated = True

    # Step 3: unknown outcomes get a fixed quiet message; asking the planner to
    # improvise a completion summary could imply success or suggest a retry.
    if interrupted_unknown:
        reflect_text = (
            "The last action may still be finishing. I stopped the remaining "
            "destructive work and preserved its action ID for reconciliation."
        )
    else:
        reflect_text = await agent._reflect(goal, executed, plan_result.model)

    agent._push_context(
        f"{'Interrupted/unknown' if interrupted_unknown else 'Completed'} plan: {goal}\n"
        + "\n".join(
            f"  {step.action} → "
            f"{step.lifecycle_status.value if step.lifecycle_status else ('ok' if step.success else 'failed')}"
            for step in executed
        )
    )

    succeeded = (not cancelled) and (halted_reason is None)
    result = AgentResult(
        goal=goal,
        domain="plan",
        model_used=plan_result.model,
        steps=executed,
        response_text=reflect_text or plan_result.text,
        success=succeeded,
        error=halted_reason if interrupted_unknown else None,
        total_latency_ms=(time.monotonic() - t0) * 1000,
    )
    agent._results_log.append(result)
    status = (
        "interrupted" if interrupted_unknown
        else "cancelled" if cancelled
        else "completed" if succeeded
        else "failed"
    )
    await agent._finalize_run(run_id, result, status)

    # Unknown work stays quiet and resumable; no completion speech or automatic
    # compensating activity races the retained worker.
    if not interrupted_unknown:
        await agent._speak_plan_completion(result, cancelled)
    _reset_plan_state(agent)

    _tracer.record_span("plan_done", trace_id=trace_id,
                        steps=len(executed), success=succeeded, status=status)
    _tracer.reset_current(_trace_tok)
    return result

async def _run_step_with_retry(agent, step: AgentStep) -> bool:
    """Execute one step with honest unknown handling and read-only retry only."""
    action = step.action.upper()
    retryable = action in agent._RETRYABLE_VERBS
    attempts = 2 if retryable else 1
    for attempt in range(attempts):
        step_t0 = time.monotonic()
        try:
            if retryable:
                # Read-only work may be cancelled at the caller boundary and
                # retried because no externally visible effect is possible.
                step.result = await asyncio.wait_for(
                    agent._execute_step(step), timeout=agent.STEP_TIMEOUT_S
                )
                step.lifecycle_status = ActionStatus.COMPLETED
                step.success = True
                step.latency_ms = (time.monotonic() - step_t0) * 1000
                return True

            if not step.action_id:
                step.action_id = secrets.token_urlsafe(32)
            identity = ActionIdentity(
                action_id=step.action_id,
                action=action,
                trace_id=agent._active_trace_id,
                run_id=step.run_id,
                step_num=step.step_num,
            )
            if action in agent._COMMAND_DELEGATED_VERBS:
                delegated = await agent._execute_step(step)
                outcome = (
                    delegated
                    if isinstance(delegated, ActionOutcome)
                    else ActionOutcome(
                        ActionStatus.COMPLETED,
                        action,
                        agent._active_trace_id,
                        step.run_id,
                        step.step_num,
                        step.action_id,
                        result=delegated,
                    )
                )
            else:
                outcome = await agent._action_registry.execute(
                    identity,
                    lambda: agent._execute_step(step),
                    timeout_s=agent.STEP_TIMEOUT_S,
                    destructive=True,
                    reconciliation_grace_s=agent.RECONCILIATION_GRACE_S,
                )
            step.lifecycle_status = outcome.status
            step.latency_ms = (time.monotonic() - step_t0) * 1000
            if outcome.status is ActionStatus.COMPLETED:
                step.result = str(outcome.result or "")
                step.success = True
                return True
            step.result = outcome.error or outcome.status.value
            if outcome.status is ActionStatus.OUTCOME_UNKNOWN:
                step.result = f"{step.result} (action_id={step.action_id})"
            step.success = None if outcome.status is ActionStatus.OUTCOME_UNKNOWN else False
            if outcome.status is ActionStatus.OUTCOME_UNKNOWN:
                log.warning(
                    "DevAgent: step %s outcome unknown; destructive branch stopped (action_id=%s)",
                    action, step.action_id,
                )
                return False
            log.error("DevAgent: step %s failed: %s", action, step.result)
        except RegistryFullError as exc:
            # Capacity is checked before the work factory starts.
            step.lifecycle_status = ActionStatus.FAILED
            step.result = f"ERROR: action denied before execution: {exc}"
            step.success = False
            step.latency_ms = (time.monotonic() - step_t0) * 1000
            return False
        except asyncio.TimeoutError:
            step.lifecycle_status = ActionStatus.FAILED
            step.result = f"ERROR: step timed out after {agent.STEP_TIMEOUT_S}s"
            step.success = False
            step.latency_ms = (time.monotonic() - step_t0) * 1000
            log.error(
                "DevAgent: read-only step %s timed out after %ss (attempt %d/%d)",
                action, agent.STEP_TIMEOUT_S, attempt + 1, attempts,
            )
        except Exception as exc:
            step.lifecycle_status = ActionStatus.FAILED
            step.result = f"ERROR: {exc}"
            step.success = False
            step.latency_ms = (time.monotonic() - step_t0) * 1000
            log.error(
                "DevAgent: step %s failed (attempt %d/%d): %s",
                action, attempt + 1, attempts, exc,
            )
    return False

def _leading_parallel_prefix(agent, remaining: list[AgentStep]) -> list[AgentStep]:
    """The leading contiguous run of pure read-only steps at the front of `remaining`."""
    prefix: list[AgentStep] = []
    for s in remaining:
        if s.action.upper() in agent._PARALLEL_VERBS:
            prefix.append(s)
        else:
            break
    return prefix

async def _gather_readonly_prefix(
    agent,
    remaining: list[AgentStep],
    executed: list[AgentStep],
    run_id: int,
) -> None:
    """Fan out a leading run of independent read-only steps concurrently.

    Closes gap #1 for the common 'gather context, then act' plan shape. No-op
    unless a scheduler is wired and >= 2 such steps lead the plan.

    Failure semantics are preserved EXACTLY: on any failed/timed-out child the
    parallel results are discarded and the steps are left in `remaining` for
    the normal sequential loop (which applies retry + replan-on-failure). Read
    verbs are idempotent, so the rare re-run is safe and side-effect-free.
    """
    if agent._scheduler is None or agent._cancel_event.is_set():
        return
    prefix = _leading_parallel_prefix(agent, remaining)
    if len(prefix) < 2:
        return

    results = await agent._scheduler.fan_out(
        [agent._run_step_with_retry( s) for s in prefix],
        label=f"devagent_readonly[{len(prefix)}]",
    )
    if not all(r is True for r in results):
        log.info(
            "DevAgent: parallel read-only prefix had a failure — falling back "
            "to sequential execution (replan-on-failure preserved)"
        )
        return

    # All succeeded — adopt the batch: advance `executed` and persist in order.
    del remaining[: len(prefix)]
    for s in prefix:
        executed.append(s)
        await agent._persist_step(run_id, len(executed), s)
    agent._current_step = len(executed)
    agent._total_steps = len(executed) + len(remaining)
    log.info("DevAgent: ran %d read-only context step(s) in parallel", len(prefix))

@staticmethod
def _plan_has_deps(steps: list[AgentStep]) -> bool:
    """True if the planner declared any inter-step dependency (engages the DAG)."""
    return any(s.deps for s in steps)

@staticmethod
def _dependents_closure(pending: dict[int, AgentStep], failed_idx: int) -> set[int]:
    """Indices in `pending` that (transitively) declare `after:` the failed step.

    Those steps' precondition can never be satisfied now, so they must NOT
    run — they're dropped from the deferred tail rather than handed to the
    dep-agnostic sequential drain.
    """
    closure: set[int] = set()
    bad = {failed_idx}
    changed = True
    while changed:
        changed = False
        for i, s in pending.items():
            if i in closure:
                continue
            if any(d in bad for d in s.deps):
                closure.add(i)
                bad.add(i)
                changed = True
    return closure

async def _run_dag_waves(
    agent,
    remaining: list[AgentStep],
    executed: list[AgentStep],
    run_id: int,
) -> tuple[bool, Optional[AgentStep]]:
    """Execute a dependency-ordered plan in waves, fanning out independent steps.

    Each wave runs every step whose declared deps are already satisfied:
    read-only fan-out-safe steps run CONCURRENTLY via the scheduler's sub-agent
    pool; destructive steps and other barriers run SOLO. Destructive steps are
    additionally held to their approved plan order.

    Stops on the first failure, on a dependency cycle / dep-on-failed-step
    (no ready steps), or on cancellation. On a failure it DROPS the failed
    step's transitive dependents from the tail (their precondition is gone)
    and returns the failed step so the caller routes into the replan path —
    a failed step's dependents must never blindly run. Returns
    (cancelled, failed_step|None). Mutates `executed` (append, in completion
    order) and `remaining` (the not-completed, still-runnable tail).
    """
    # 1-based position → step, preserving the planner's numbering for deps.
    pending: dict[int, AgentStep] = {i: s for i, s in enumerate(remaining, 1)}
    completed: set[int] = set()
    cancelled = False
    failed_step: Optional[AgentStep] = None
    failed_idx: Optional[int] = None

    while pending and failed_step is None and not cancelled:
        if agent._cancel_event.is_set():
            cancelled = True
            break
        ready = [i for i, s in pending.items()
                 if all(d in completed for d in s.deps)]
        if not ready:
            # Cycle, or a dependency landed on a step that didn't complete —
            # let the sequential loop sort out the remainder. Log the specific
            # unmet deps per pending step (not just the count) so a stuck DAG
            # is diagnosable instead of silently degrading (E19).
            unmet = {i: sorted(set(s.deps) - completed) for i, s in pending.items()}
            log.warning("DevAgent[dag]: no ready steps (cycle/unmet dep) — "
                        "%d step(s) deferred to sequential; unmet deps %r",
                        len(pending), unmet)
            break

        safe = [i for i in ready if pending[i].action.upper() in agent._FANOUT_SAFE_VERBS]
        barriers = [i for i in ready if i not in safe]

        # Destructive actions are claimed and executed in the exact approved
        # sequence. Even if a later destructive node is dependency-ready, hold
        # it until every earlier destructive node has completed.
        first_destructive = next(
            (i for i in sorted(pending)
             if getattr(pending[i], "_approval_destructive", False)),
            None,
        )
        if first_destructive is not None:
            barriers = [
                i for i in barriers
                if not getattr(pending[i], "_approval_destructive", False)
                or i == first_destructive
            ]

        if not safe and not barriers:
            log.warning(
                "DevAgent[dag]: later destructive step ready before approved order; "
                "deferring remaining steps to sequential execution"
            )
            break

        # Bind durable identity before any task in the wave can start.
        for i in safe:
            pending[i].run_id = run_id
            pending[i].step_num = agent._step_seq.get(id(pending[i]), i)
            await agent._emit_step_started(pending[i])
        # Fan-out-safe ready steps run concurrently (or inline if just one).
        if len(safe) >= 2 and agent._scheduler is not None:
            results = await agent._scheduler.fan_out(
                [agent._run_step_with_retry( pending[i]) for i in safe],
                label=f"dag_wave[{len(safe)}]",
            )
        else:
            results = [await agent._run_step_with_retry( pending[i]) for i in safe]

        for idx, ok in zip(safe, results):
            step = pending.pop(idx)
            executed.append(step)
            await agent._persist_step(run_id, len(executed), step)
            if ok is True:
                completed.add(idx)
            elif step.lifecycle_status is ActionStatus.OUTCOME_UNKNOWN:
                # Unknown dominates a proven failure in the same concurrent
                # wave: any live destructive worker forbids recovery activity.
                failed_step, failed_idx = step, idx
            elif failed_step is None:
                failed_step, failed_idx = step, idx
        if failed_step is not None:
            break

        # Barriers run one at a time, in plan order.
        for idx in sorted(barriers):
            if agent._cancel_event.is_set():
                cancelled = True
                break
            pending[idx].run_id = run_id
            pending[idx].step_num = agent._step_seq.get(id(pending[idx]), idx)
            await agent._emit_step_started(pending[idx])
            ok = await agent._run_step_with_retry( pending[idx])
            step = pending.pop(idx)
            executed.append(step)
            await agent._persist_step(run_id, len(executed), step)
            if ok is True:
                completed.add(idx)
            else:
                failed_step, failed_idx = step, idx
                break

    # On failure, drop the failed step's transitive dependents — they can
    # never legally run. Survivors stay in the tail as replan context.
    dropped: set[int] = set()
    if failed_idx is not None:
        dropped = _dependents_closure(pending, failed_idx)
    if dropped:
        log.info("DevAgent[dag]: dropping %d dependent(s) of failed step %d",
                 len(dropped), failed_idx)
    tail = [pending[i] for i in sorted(pending) if i not in dropped]
    remaining[:] = tail
    log.info("DevAgent[dag]: completed %d step(s) in waves, %d deferred%s",
             len(completed), len(tail), " (cancelled)" if cancelled else
             (" (failure → replan)" if failed_step is not None else ""))
    return cancelled, failed_step

def build_replan_prompt(
    agent, goal: str, executed: list[AgentStep], remaining: list[AgentStep],
    *, enabled: bool = False,
) -> tuple[str, dict]:
    """Build the recovery-replan user prompt + return (prompt, traj_stats).

    Extracted so the replan eval (`evals.run --mode replan`) scores the EXACT
    prompt production sends — a true closed loop, not a copy. `enabled` gates
    trajectory reduction (`inference/trajectory.render_trajectory`): False
    reproduces the legacy per-step rendering byte-for-byte.
    """
    from inference.trajectory import render_trajectory, dedup_enabled
    traj_text, traj_stats = render_trajectory(
        executed, style="replan",
        readonly_verbs=agent._PARALLEL_VERBS, enabled=enabled,
        dedup_reads=dedup_enabled(),
    )
    lines = [f"Goal: {goal}", "", "Steps already executed (with outcomes):", traj_text]
    if remaining:
        lines.append("")
        lines.append("Original remaining steps (not yet run):")
        for s in remaining:
            lines.append(f"  [{s.action} {s.args[:60]}]")
    lines += [
        "",
        "The last step FAILED. Produce a REVISED numbered plan for the remaining "
        "work that recovers from the failure, using the same [ACTION args] step "
        "format. Do not repeat already-completed work. If the goal cannot proceed, "
        "reply with a single [EXPLAIN <reason>] step.",
    ]
    return "\n".join(lines), traj_stats

async def _replan(
    agent, goal: str, executed: list[AgentStep], remaining: list[AgentStep]
) -> list[AgentStep]:
    """Ask the planner for a revised plan for the REMAINING work after a failure.

    Feeds the executed steps + their outcomes (the observation signal) back to
    the plan-domain model so it can recover. Returns parsed steps, or [] if the
    planner errors or declines.
    """
    # Synthesize the executed trajectory before re-feeding it (token economics —
    # spec specs/trajectory-reduction/). enabled=False reproduces the legacy
    # per-step rendering byte-for-byte; the flag (DA_TRAJECTORY_REDUCE) gates
    # the reduction until the eval baseline locks.
    from inference.trajectory import reduction_enabled
    _reduce = reduction_enabled()
    prompt, traj_stats = build_replan_prompt(agent, 
        goal, executed, remaining, enabled=_reduce
    )
    if _reduce and traj_stats["chars_saved"] > 0:
        try:
            from monitoring.trace import get_tracer
            get_tracer().record_span(
                "replan",
                traj_steps_in=traj_stats["steps_in"],
                traj_steps_rendered=traj_stats["lines_out"],
                traj_chars_saved=traj_stats["chars_saved"],
            )
        except Exception:
            pass
    try:
        r = await agent._router.infer(domain="plan", user_text=prompt, context=None)
        if r.ok and r.text:
            try:
                steps = _parse_plan_json(r.text)
            except Exception:
                steps = []
            if not steps:
                steps = _parse_plan(r.text)
            
            # Gap 2: Replan Critic
            if steps and os.environ.get("DA_REPLAN_CRITIC", "0").strip().lower() in ("1", "true", "yes", "on"):
                try:
                    from inference.critic import Critic
                    critic = Critic(agent._router, model_domain="plan")
                    verdict = await critic.review_plan(goal, r.text)
                    if verdict.decision in ("revise", "block"):
                        log.warning("DevAgent: replan rejected by Critic: %s", verdict.summary())
                        msg = f"Replan rejected by Critic ({verdict.decision}):\n"
                        for f in verdict.findings:
                            msg += f"- [{f.severity}] {f.message}\n"
                        if verdict.suggested_fix:
                            msg += f"\nSuggestion: {verdict.suggested_fix}"
                        # Return a synthetic step to inject the critic's findings as an observation
                        return [AgentStep(action="CRITIC_REJECT", body=msg)]
                except Exception as e:
                    log.debug("DevAgent._replan critic check failed: %s", e)

            return steps
    except Exception as exc:
        log.debug("DevAgent._replan failed: %s", exc)
    return []

async def _try_replan(
    agent, goal: str, executed: list[AgentStep], remaining: list[AgentStep]
) -> Optional[list[AgentStep]]:
    """One bounded recovery replan. Returns the new (budget-capped) remaining
    steps, or None if the planner declined/errored (caller should halt).

    Recomputes destructiveness: the upfront approval covered the ORIGINAL
    plan's verbs, so a replan that injects a destructive verb the user never
    heard described revokes the blanket authorization — that step (and every
    later destructive step) then goes through per-op confirmation.
    """
    new_steps = await _replan(agent, goal, executed, remaining)
    
    # Handle Critic rejection: log it and return [] so the caller's replan
    # budget loop decides whether to retry. Guarded so any unexpected error
    # halts safely instead of propagating up and killing the whole plan.
    if new_steps and new_steps[0].action == "CRITIC_REJECT":
        try:
            rejection_step = AgentStep(action="PLAN", body="Proposed recovery plan")
            rejection_step.step_num = len(executed) + 1
            executed.append(rejection_step)
            # Supply model_used (required field on AgentResult — no default).
            # Use the model identifier from the step if available, else sentinel.
            from inference.dev_agent import AgentResult
            model_str = getattr(new_steps[0], "model", None) or "critic"
            _critic_res = AgentResult(
                goal=goal, domain="plan", success=False,
                error=new_steps[0].body,
                model_used=model_str,
            )
            log.warning(
                "Critic rejected replan for goal=%r: %s",
                goal, new_steps[0].body,
            )
        except Exception as exc:  # pragma: no cover
            log.error("CRITIC_REJECT handler failed unexpectedly: %s", exc)
        # Return [] so the caller's loop will try replanning again if budget allows
        return []

    # S2.5 / E18: Planner honesty. A replan that yields only EXPLAIN steps
    # means it cannot proceed. Filter them out so it parses to zero real steps.
    # This will return None and properly halt the plan instead of a false success.
    real_steps = [s for s in new_steps if s.action != "EXPLAIN"]
    if not real_steps:
        return None
    # Verb-level blanket revocation is intentionally gone.  StepExecutor
    # recomputes the exact action digest after canonicalization and reuses the
    # whole-plan grant only on exact membership.  Changed args/path/body,
    # identity, dependencies, or a new destructive step therefore receives a
    # per-operation request even when its verb is unchanged.
    budget = max(0, agent.MAX_STEPS - len(executed))
    return new_steps[:budget]

async def _reconcile_interrupted_unknowns(agent, run_id: int) -> tuple[bool, list[str]]:
    """Return (blocked, terminal evidence) for persisted unknown action IDs."""
    db = agent._db()
    if not db or not getattr(db, "available", False):
        return False, []
    steps = await db.runs.get_steps_for_run(run_id)
    unknown_ids: list[str] = []
    for step in steps:
        match = re.search(r"action_id=([^\s)]+)", str(step.get("result") or ""))
        if match:
            unknown_ids.append(match.group(1))
    if not unknown_ids:
        return False, []

    observations = {item.action_id: item for item in await agent._action_registry.snapshot()}
    summaries: list[str] = []
    for action_id in unknown_ids:
        outcome = observations.get(action_id)
        if outcome is None or outcome.status is ActionStatus.OUTCOME_UNKNOWN:
            return True, []
        summaries.append(
            f"action_id={action_id} {outcome.action} reconciled {outcome.status.value}"
        )
    return False, summaries


async def resume_pending_plan(agent) -> Optional[dict]:
    """Offer to resume the most recent interrupted plan, gated on voice confirm.

    Accessibility safety: never auto-resumes — an interrupted plan may contain
    destructive steps, so it requires an explicit spoken "yes" (via
    _confirm_destructive_op). Re-runs plan_and_run for the goal (a fresh plan
    that the closed-loop controller adapts), and returns the resumed run dict,
    or None if there's nothing to resume / the user declines.
    """
    db = agent._db()
    if not db or not getattr(db, "available", False):
        return None
    runs = await db.runs.get_interrupted_runs(limit=1)
    if not runs:
        return None
    run = runs[0]
    goal = run.get("goal", "")
    run_id = run.get("id")
    blocked_unknown = False
    reconciled: list[str] = []
    if run_id is not None:
        blocked_unknown, reconciled = await _reconcile_interrupted_unknowns(
            agent, int(run_id)
        )
    if blocked_unknown:
        log.warning(
            "DevAgent.resume_pending_plan: run %s still has unresolved action work; "
            "resume and compensation remain blocked",
            run_id,
        )
        return None
    if not await agent._confirm_destructive_op(
        f"Resume the interrupted task: {goal[:60]}?",
        approval_action="RESUME_PLAN",
        security_args={"run_id": run_id},
        content=goal,
    ):
        log.info("DevAgent.resume_pending_plan: user declined resume of run %s", run.get("id"))
        # Declining a resume rolls back the crashed run's completed side
        # effects — the user chose not to finish it, so partial destructive
        # work shouldn't be left behind.
        run_id = run.get("id")
        if run_id is not None:
            await agent._run_compensations(int(run_id), triggered_by="user_cancel")
        return None
    log.info("DevAgent.resume_pending_plan: resuming run %s — %r", run.get("id"), goal[:60])
    # Working-memory seed (Gap C): derive what the interrupted run already did
    # from its persisted steps and seed the resumed plan with it, so the planner
    # recovers instead of restarting blind. Flag-gated (DA_RESUME_MEMORY) and
    # degrades to an empty seed (today's behavior) on any failure (R2, R3.2).
    seed = await _resume_seed_context(agent, run.get("id"), goal, run=run)
    if reconciled:
        evidence = "Reconciled prior action outcomes:\n" + "\n".join(reconciled)
        seed = f"{evidence}\n\n{seed}" if seed else evidence
    await agent.plan_and_run(
        goal,
        seed_context=seed,
        work_trust=WorkTrust.TRUSTED_MANUAL,
    )
    return run

async def _resume_seed_context(agent, run_id, goal: str, run: dict = None) -> str:
    """Build the resume working-memory seed block, or '' (Gap C, R2/R3).

    Off (DA_RESUME_MEMORY unset) or any failure → '' so resume is byte-identical
    to today. Derived from the durable agent_steps — no schema change (R3.1)."""
    from inference.working_memory import memory_enabled
    if not memory_enabled() or run_id is None:
        return ""
    try:
        from inference.working_memory import summarize_run, render_seed
        db = agent._db()
        steps = await db.runs.get_steps_for_run(int(run_id))
        if not steps:
            return ""
            
        run_end_ts = 0.0
        if run and "ts" in run:
            run_end_ts = run["ts"] + sum(s.get("latency_ms", 0) for s in steps) / 1000.0
            
        return render_seed(summarize_run(goal, steps, run_end_ts=run_end_ts))
    except Exception as exc:
        log.debug("DevAgent._resume_seed_context failed: %s", exc)
        return ""

async def drain_goal_queue(agent, max_goals: int = 0) -> int:
    """Drain the durable goal backlog (gap D): claim → run → mark terminal.

    Single-flight: claim_next_goal's SELECT-then-guarded-UPDATE is only
    race-safe with one consumer, and concurrent drainers would interleave
    plan state. If a drain is already active (startup drain still running
    when a voice "authorize" enqueues a new goal), this call signals the
    active drainer to re-check the queue after it thinks it's empty, and
    returns 0 — the goal is never abandoned.

    Flare gate: before each claim, waits on the scheduler's dev-admission
    event so no NEW heavy plan starts mid-flare (the real production
    enforcement of pause_dev for this path).

    Stops when the queue is empty, on cancellation, or after `max_goals`
    (0 = until empty). Returns the number processed. Each goal's outcome is
    persisted, so this is safe to call again at any time (e.g. after a
    crash — see AgentDB.requeue_stale_running).
    """
    db = agent._db()
    if not db or not getattr(db, "available", False):
        return 0
    if agent._drain_lock.locked():
        # An active drainer exists — tell it to re-check after its final
        # empty claim so a goal enqueued in that window isn't stranded.
        agent._drain_signal = True
        log.info("DevAgent.drain_goal_queue: drain already active — signalled re-check")
        return 0
    async with agent._drain_lock:
        processed = 0
        while True:
            agent._drain_signal = False
            while not (max_goals and processed >= max_goals):
                if agent._cancel_event.is_set():
                    break
                # Flare admission gate: never START a heavy plan mid-flare.
                sched = agent._scheduler
                if sched is not None and getattr(sched, "dev_paused", False):
                    log.info(
                        "DevAgent.drain_goal_queue: dev admission paused "
                        "(flare) — waiting before next claim"
                    )
                    await sched.wait_dev_admission()
                goal = await db.goals.claim_next_goal()
                if goal is None:
                    break
                gid = int(goal["id"])
                log.info("DevAgent.drain_goal_queue: running goal %s — %r",
                         gid, goal["goal"][:60])
                await agent._publish_bg(TOPIC_GOAL_DEQUEUED, {
                    "goal_id": gid,
                    "goal": goal["goal"][:200],
                    "source_trigger": goal.get("source_trigger"),
                })
                _g_status, _g_ok = "failed", False
                try:
                    agent._next_work_trust = work_trust_from_source(
                        goal.get("source_trigger")
                    )
                    result = await agent.plan_and_run(goal["goal"])
                    _g_ok = bool(result.success)
                    _g_status = "done" if _g_ok else "failed"
                    await db.goals.complete_goal(gid, _g_status, error=result.error)
                except Exception as exc:
                    log.error("DevAgent.drain_goal_queue: goal %s raised: %s", gid, exc)
                    _g_status, _g_ok = "failed", False
                    await db.goals.complete_goal(gid, "failed", error=str(exc))
                await agent._publish_bg(TOPIC_GOAL_COMPLETED, {
                    "goal_id": gid, "status": _g_status, "success": _g_ok,
                })
                processed += 1
            # Re-check once if another caller requested a drain while we ran
            # (until-empty mode only; bounded calls return at their cap).
            if not agent._drain_signal or max_goals:
                break
        if processed:
            log.info("DevAgent.drain_goal_queue: processed %d goal(s)", processed)
        return processed

async def _destructive_plan_digests(
    agent, goal: str, steps: list[AgentStep]
) -> tuple[str, ...]:
    """Resolve and digest each exact destructive action in approved plan order."""
    digests: list[str] = []
    trace_id = agent._active_trace_id or ""
    for ordinal, step in enumerate(steps[: agent.MAX_STEPS], 1):
        step.step_num = step.step_num or ordinal
        action = step.action.upper()
        step._approval_destructive = False

        if action in ("SKILL_CALL", "SKILL_QUERY"):
            material = await asyncio.to_thread(prepare_skill_action, agent, step)
            if not material.is_send:
                continue
            if not material.skill_id or not material.tool:
                raise ValueError("destructive skill step is missing skill or tool")
            step._approval_destructive = True
            digests.append(skill_action_digest(
                material, step, trace_id=trace_id, goal=goal
            ))
            continue

        if action not in agent._DESTRUCTIVE_VERBS:
            continue
        step._approval_destructive = True
        canonical_path: str | None = None
        canonical_cwd: str | None = None
        content: str | None = None
        if action in ("WRITE_FILE", "EDIT_FILE"):
            canonical_path, content = await prepare_file_candidate(agent, step)
        elif action in ("RUN_TERMINAL", "GIT_COMMIT", "GIT_CHECKOUT", "GITHUB_PR"):
            decision = await asyncio.to_thread(
                agent._execution_scope.authorize_cwd, None
            )
            if not decision.allowed or decision.canonical_path is None:
                raise PermissionError(decision.reason or f"{action} cwd denied")
            canonical_cwd = str(decision.canonical_path)
        digests.append(step_action_digest(
            step,
            trace_id=trace_id,
            goal=goal,
            canonical_path=canonical_path,
            canonical_cwd=canonical_cwd,
            content=content,
            approval_context="plan",
        ))
    return tuple(digests)


async def _approve_plan_upfront(agent, goal: str, steps: list[AgentStep], assumptions: list[str] = None) -> str:
    """Collect one exact whole-plan response while preserving fail-safe denial."""
    from core.goal_session import GoalSessionStore

    verbs = [s.action for s in steps[: agent.MAX_STEPS]]
    verb_summary = ", ".join(verbs[:6])
    if len(verbs) > 6:
        verb_summary += f" … (+{len(verbs) - 6} more)"
    n = min(len(steps), agent.MAX_STEPS)
    message = f"I'll run {n} step{'s' if n != 1 else ''}: {verb_summary}. Approve all?"

    if os.environ.get("DA_PLAN_PREVIEW", "0").strip().lower() in ("1", "true", "yes", "on"):
        threshold = int(os.environ.get("DA_PLAN_PREVIEW_THRESHOLD", "3"))
        if len(steps) >= threshold:
            try:
                actions = []
                for s in steps[:agent.MAX_STEPS]:
                    if s.action:
                        args_trunc = str(s.args)[:80] if s.args else ""
                        actions.append(f"Step {s.step_num}: {s.action} {args_trunc}")
                assumptions_text = ""
                if assumptions:
                    assumptions_text = "Assumptions made by planner:\n" + "\n".join(f"- {a}" for a in assumptions) + "\n\n"
                prompt = (
                    f"Goal: {goal}\n"
                    f"{assumptions_text}"
                    f"Steps proposed:\n" + "\n".join(actions) + "\n\n"
                    "Provide a 1-sentence plain English spoken summary of what this plan intends to do. "
                    "Do NOT list the API verbs (like write_file). Just summarize the outcome."
                )
                res = await asyncio.wait_for(
                    agent._router.infer(domain="plan", user_text=prompt, context=None),
                    timeout=5.0
                )
                if res and res.ok and res.text:
                    preview = res.text.strip()
                    if preview.startswith('"') and preview.endswith('"'):
                        preview = preview[1:-1]
                    if preview:
                        message = f"{preview} Approve all?"
            except Exception as exc:
                log.warning("DevAgent._approve_plan_upfront: preview generation failed: %s", exc)

    log.info("DevAgent: requesting plan approval — %s", message)

    try:
        plan_step_digests = await _destructive_plan_digests(agent, goal, steps)
        plan_is_destructive = bool(plan_step_digests)
        if plan_is_destructive:
            request_digest = plan_digest(
                plan_step_digests,
                goal=goal,
                trace_id=agent._active_trace_id or "",
            )
        else:
            # Read-only consent is correlated too, but this synthetic digest is
            # never retained as execution authority.
            request_digest = sha256_digest({
                "version": 1,
                "kind": "read_only_plan_consent",
                "trace_id": agent._active_trace_id or "",
                "goal": goal,
                "steps": [
                    {"n": s.step_num or i, "action": s.action, "args": s.args, "body": s.body}
                    for i, s in enumerate(steps[: agent.MAX_STEPS], 1)
                ],
            })
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        log.info("DevAgent._approve_plan_upfront: approval material invalid (%s) → DENY", exc)
        return "denied"

    try:
        request = agent._approval_authority.create_request(
            trace_id=agent._active_trace_id or "",
            action_digest=request_digest,
            description=message,
        )
        agent._approval_authority.open_response_window(request)
    except Exception as exc:
        if plan_is_destructive:
            log.info("DevAgent._approve_plan_upfront: authority unavailable (%s) → DENY", exc)
            return "denied"
        log.info("DevAgent._approve_plan_upfront: read-only authority unavailable (%s) → auto-run", exc)
        GoalSessionStore.create(goal=goal, domain="plan")
        return "auto"

    await agent._publish_live(TOPIC_DAG_APPROVAL, {
        "message": message,
        "destructive": plan_is_destructive,
        "request_id": request.request_id,
        "action_digest": request.action_digest,
        "goal": goal[:200],
        "steps": [
            {"n": s.step_num or i, "action": s.action,
             "args": (s.args or "")[:agent._ARGS_SNIPPET_CHARS]}
            for i, s in enumerate(steps[: agent.MAX_STEPS], 1)
        ],
    })

    async def _close_remote_window() -> bool:
        sealed = False
        try:
            await asyncio.to_thread(
                agent._approval_authority.close_response_window, request
            )
            sealed = True
        except Exception as exc:
            log.info(
                "DevAgent._approve_plan_upfront: could not seal responders (%s)",
                exc,
            )
        await asyncio.to_thread(agent._approval_authority.close_pending_request, request)
        await agent._publish_live("dag.approval_closed", {
            "request_id": request.request_id,
        })
        return sealed

    def _finish(verdict: str) -> str:
        approved_digests = plan_step_digests if plan_is_destructive else (request.action_digest,)
        plan_goal = goal if plan_is_destructive else None
        try:
            grant = agent._approval_authority.consume_response(
                request, approved_digests=approved_digests, plan_goal=plan_goal
            )
        except Exception as exc:
            log.info("DevAgent._approve_plan_upfront: authority failed (%s) → DENY", exc)
            grant = None
        if plan_is_destructive:
            agent._approval_grant = grant
            if verdict != "approved" or grant is None:
                return "denied"
        elif verdict == "approved" and grant is None:
            return "denied"
        if verdict in ("approved", "auto"):
            GoalSessionStore.create(goal=goal, domain="plan")
        return verdict

    def _fallback(reason: str) -> str:
        if plan_is_destructive:
            log.info("DevAgent._approve_plan_upfront: %s + destructive plan → DENY", reason)
            return _finish("denied")
        log.info("DevAgent._approve_plan_upfront: %s + read-only plan → auto-run", reason)
        return _finish("auto")

    async def _publish(decision: str) -> None:
        response = ApprovalResponse(
            request_id=request.request_id,
            action_digest=request.action_digest,
            decision="approve" if decision == "approve" else "deny",
            responded_at=time.time(),
        )
        try:
            await asyncio.to_thread(agent._approval_authority.write_response, response)
        except Exception as exc:
            log.info("DevAgent._approve_plan_upfront: response publication rejected (%s)", exc)

    try:
        from tts.polly_stream import get_client as _get_tts
        await asyncio.to_thread(_get_tts().speak_sync, message)
    except Exception as exc:
        await _close_remote_window()
        return _fallback(f"TTS unavailable ({exc})")

    remote_sealed = False
    try:
        await asyncio.to_thread(agent._approval_authority.publish_pending_request, request)
        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline:
            if agent._approval_authority.response_path(request.request_id).exists():
                return _finish("approved")
            await asyncio.sleep(0.1)
    except OSError as exc:
        log.info("DevAgent._approve_plan_upfront: responder window failed (%s)", exc)
    finally:
        remote_sealed = await _close_remote_window()

    # A responder that won the close boundary is fully published before the
    # response window lock releases; consume it instead of starting fallback.
    if agent._approval_authority.response_path(request.request_id).exists():
        return _finish("approved")
    if not remote_sealed:
        return _fallback("responder window closure unverified")

    try:
        import numpy as np
        import sounddevice as sd
        audio = await asyncio.to_thread(
            lambda: sd.rec(int(4.0 * 16_000), samplerate=16_000,
                           channels=1, dtype="float32").flatten()
        )
        await asyncio.to_thread(sd.wait)
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.005:
            return _fallback("silence")
        if agent._confirm_whisper is None:
            from faster_whisper import WhisperModel
            agent._confirm_whisper = await asyncio.to_thread(
                WhisperModel, "tiny", device="cpu", compute_type="int8"
            )

        def _transcribe() -> str:
            segs, _ = agent._confirm_whisper.transcribe(
                audio, language="en", beam_size=1, vad_filter=False
            )
            return " ".join(s.text for s in segs).lower().strip()

        transcript = await asyncio.to_thread(_transcribe)
    except Exception as exc:
        return _fallback(f"mic fallback failed ({exc})")

    verdict = classify_confirmation(transcript)
    if verdict == "deny":
        log.info("DevAgent._approve_plan_upfront: REJECTED — %r", transcript)
        await _publish("deny")
        return _finish("denied")
    if verdict == "approve":
        log.info("DevAgent._approve_plan_upfront: approved — %r", transcript)
        await _publish("approve")
        return _finish("approved")
    return _fallback(f"ambiguous reply {transcript!r}")


def _reset_plan_state(agent) -> None:
    """Clean up goal-session and status fields after a plan run."""
    from core.goal_session import GoalSessionStore
    GoalSessionStore.cancel()
    try:
        agent._approval_authority.revoke_grant(agent._approval_grant)
    except Exception:
        pass
    agent._approval_grant = None
    agent._plan_authorized = False
    agent._approved_verbs = frozenset()
    agent._escalated_this_run = False
    agent._rollback_summary = None
    agent._cancel_event.clear()
    agent._current_goal = None
    agent._current_step = 0
    agent._total_steps = 0
    agent._active_plan_model = ""
    agent._active_work_trust = WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    agent._critic_revise_counts = {}

