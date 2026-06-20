"""Deterministic trajectory reduction for the DevAgent replan/reflect loop.

Spec: `specs/trajectory-reduction/requirements.md`.

`DevAgent.plan_and_run` re-serializes its entire executed-step history every time
a step fails (`_replan`/`_try_replan`) and once more at the end (`_reflect`). On a
long plan that history is re-sent three-plus times — the classic agent token sink
where input tokens dominate spend (arXiv 2509.23586). The plan domain runs locally
(`qwen3-coder:30b`), so the immediate win is lower prompt-processing latency and a
smaller KV-cache footprint; when a run escalates to the cloud it is real Bedrock
spend avoided.

`render_trajectory` synthesizes the trajectory deterministically — keep the most
recent steps verbatim, abstract older successes to one line, collapse runs of
older read-only observations, and ALWAYS preserve failure signal (the whole reason
the trajectory is re-sent). No LLM call, no I/O, no mutation of the input steps —
so it adds no inference cost of its own and is trivially testable.

With `enabled=False` it reproduces the caller's legacy per-step rendering
byte-for-byte (pure refactor); `DevAgent` ships it behind `DA_TRAJECTORY_REDUCE`
(default off) until the eval baseline locks.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inference.dev_agent import AgentStep


def reduction_enabled() -> bool:
    """True when DA_TRAJECTORY_REDUCE is explicitly truthy. Default OFF (spec R2.4)."""
    return os.environ.get("DA_TRAJECTORY_REDUCE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _render_full(i: int, s: "AgentStep", style: str,
                 success_chars: int, failure_chars: int) -> str:
    """Render one step at full fidelity, matching the caller's legacy format exactly.

    `replan` style (one line) and `reflect` style (two lines) reproduce the two
    inline loops they replace byte-for-byte, so `enabled=False` is a pure refactor.
    """
    snippet = (s.result or "")
    snippet = snippet[:success_chars] if s.success else snippet[:failure_chars]
    if style == "reflect":
        sym = "✓" if s.success else "✗"   # ✓ / ✗
        return f"  {i}. {sym} {s.action} {s.args[:60]}\n     → {snippet}"
    # replan
    status = "ok" if s.success else "FAILED"
    return f"  {i}. [{status}] {s.action} {s.args[:60]} → {snippet}"


def _collapse_targets(targets: list[tuple[int, "AgentStep"]], limit: int = 6) -> str:
    """Name the targets of a collapsed read-only run, preserving paths intact (R1.6).

    Uses each step's full `args` (never truncated mid-string), de-duplicated and
    order-preserving, capped to `limit` with a '+N more' overflow tag.
    """
    names: list[str] = []
    for _, s in targets:
        name = (s.args or s.action).strip()
        if name and name not in names:
            names.append(name)
    if len(names) > limit:
        extra = len(names) - limit
        names = names[:limit] + [f"+{extra} more"]
    return ", ".join(names)


def _abstract_line(idx: int, s: "AgentStep") -> str:
    """One-line abstraction of an older successful step: ≤80-char outcome, full
    args (file paths intact, R1.6)."""
    outcome = (s.result or "").replace("\n", " ").strip()[:80]
    arrow = f" → {outcome}" if outcome else ""
    return f"  {idx}. [ok] {s.action} {s.args}{arrow}".rstrip()


def render_trajectory(
    steps: list["AgentStep"],
    *,
    style: str = "replan",
    keep_verbatim: int = 3,
    success_chars: int = 300,
    failure_chars: int = 300,
    readonly_verbs: frozenset = frozenset(),
    collapse_readonly_runs: bool = True,
    enabled: bool = True,
) -> tuple[str, dict]:
    """Render an executed-step trajectory to text, optionally token-reduced.

    Returns ``(text, stats)`` where ``stats`` = ``{steps_in, lines_out,
    chars_in, chars_out, chars_saved}``. Deterministic; performs no I/O and never
    mutates a step (R1.1, R3.3).

    When ``enabled`` is False, or the trajectory has ``<= keep_verbatim`` steps,
    the output is the full per-step rendering (legacy behavior / pass-through,
    R2.4 & R3.2). Otherwise the leading ``len(steps) - keep_verbatim`` steps are
    abstracted: failed steps keep their full failure signal regardless of age
    (R1.4/R3.1), consecutive older read-only successes collapse into one line
    (R1.5), and other older successes shrink to a single ≤80-char outcome line
    (R1.3). The most recent ``keep_verbatim`` steps are always verbatim (R1.2).
    """
    def full(i: int, s: "AgentStep") -> str:
        return _render_full(i, s, style, success_chars, failure_chars)

    n = len(steps)
    full_lines = [full(i, s) for i, s in enumerate(steps, 1)]
    chars_in = sum(len(line) for line in full_lines)

    if not enabled or n <= keep_verbatim:
        text = "\n".join(full_lines)
        return text, {
            "steps_in": n, "lines_out": len(full_lines),
            "chars_in": chars_in, "chars_out": chars_in, "chars_saved": 0,
        }

    cutoff = n - keep_verbatim          # steps[0:cutoff] abstracted; tail verbatim
    out_lines: list[str] = []
    ro = {v.upper() for v in readonly_verbs}
    i = 0
    while i < cutoff:
        s = steps[i]
        idx1 = i + 1
        # Failures are exempt from abstraction — they are the recovery signal.
        if s.success is False:
            out_lines.append(full(idx1, s))
            i += 1
            continue
        # Collapse a run of consecutive older read-only successes — but keep the
        # LAST one VERBATIM. Collapsing strips results down to filenames; the most
        # recent read (the one right before the agent acted) usually carries the
        # decisive diagnostic, and dropping it measurably degraded recovery
        # ordering on long-prefix replans (dev_replan eval finding). So only the
        # head of the run is summarized; its last step keeps its full result.
        if collapse_readonly_runs and s.action.upper() in ro:
            j = i
            run: list[tuple[int, "AgentStep"]] = []
            while j < cutoff:
                sj = steps[j]
                if sj.success is not False and sj.action.upper() in ro:
                    run.append((j + 1, sj))
                    j += 1
                else:
                    break
            if len(run) >= 2:
                head, (last_idx, last_step) = run[:-1], run[-1]
                if len(head) >= 2:
                    lo, hi = head[0][0], head[-1][0]
                    out_lines.append(
                        f"  {lo}–{hi}. [ok] {len(head)} read-only steps: "
                        f"{_collapse_targets(head)}"
                    )
                else:  # single head step → a plain one-line abstract (no "1 steps")
                    out_lines.append(_abstract_line(head[0][0], head[0][1]))
                out_lines.append(full(last_idx, last_step))   # most-recent read kept verbatim
                i = j
                continue
            # a lone read-only step falls through to the generic abstract line
        # Generic abstraction: one line, ≤80-char outcome, full args (paths intact R1.6).
        out_lines.append(_abstract_line(idx1, s))
        i += 1

    # Verbatim window — the most recent keep_verbatim steps in full fidelity.
    for k in range(cutoff, n):
        out_lines.append(full(k + 1, steps[k]))

    text = "\n".join(out_lines)
    chars_out = sum(len(line) for line in out_lines)
    return text, {
        "steps_in": n, "lines_out": len(out_lines),
        "chars_in": chars_in, "chars_out": chars_out,
        "chars_saved": max(0, chars_in - chars_out),
    }
