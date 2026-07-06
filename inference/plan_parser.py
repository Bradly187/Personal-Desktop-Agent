import json
import re
from dataclasses import dataclass, field
from typing import Optional

# Step model
# ---------------------------------------------------------------------------

# Action verbs the planner model is allowed to emit
_PLAN_ACTIONS = {
    "WRITE_FILE", "EDIT_FILE", "RUN_TERMINAL", "CLICK", "OPEN", "HOTKEY",
    "EXPLAIN", "SEARCH_WEB", "READ_SCREEN", "READ_FILE", "GREP",
    "SCROLL", "TYPE",
    # Git-native verbs (item #3 / #8 in roadmap)
    "GIT_STATUS", "GIT_DIFF", "GIT_COMMIT", "GIT_CHECKOUT",
    # GitHub integration
    "GITHUB_PR",
    # Web retrieval (replaces browser-open SEARCH_WEB for context injection)
    "FETCH_URL",
    # Skills — MCP-client tool calls (SKILL_QUERY=read, SKILL_CALL=send/mutate).
    "SKILL_QUERY", "SKILL_CALL",
    # Personal knowledge base — semantic search over the user's own documents.
    "SEARCH_PERSONAL",
    # Planner-driven read-only investigation sub-agent (specs/dev-agent-delegate-verb).
    "DELEGATE",
}

_STEP_PATTERN = re.compile(
    r"^\s*(?:Step\s*\d+[:.]\s*)?"          # optional "Step N:"
    r"\[?"                                   # optional [
    r"(WRITE_FILE|EDIT_FILE|RUN_TERMINAL|CLICK|OPEN|HOTKEY|EXPLAIN|SEARCH_WEB"
    r"|READ_SCREEN|READ_FILE|GREP|SCROLL|TYPE"
    r"|GIT_STATUS|GIT_DIFF|GIT_COMMIT|GIT_CHECKOUT|GITHUB_PR|FETCH_URL"
    r"|SKILL_QUERY|SKILL_CALL|SEARCH_PERSONAL|DELEGATE)"
    r"(?:\s+([^\]\n]+))?"                   # optional args (up to a closing ] or EOL)
    r"\s*\]?",                              # optional ]
    re.IGNORECASE,
)

# Planner teaching for the DELEGATE verb — injected into the plan context ONLY when
# DA_DELEGATE is on (specs/dev-agent-delegate-verb R4.4), so the planner vocabulary
# is byte-identical to today when the feature is off.
_DELEGATE_PROMPT_INSTRUCTIONS = (
    "You may emit [DELEGATE <question>] to hand a scoped, READ-ONLY investigation "
    "to a bounded sub-agent (it can read files / grep / fetch but cannot write, run "
    "shell, or take any action). Prefer it when you need to find something out "
    "before acting — e.g. [DELEGATE which module defines the FooBar class]. The "
    "sub-agent returns a short finding you can use in later steps. Use it sparingly; "
    "for a single quick read prefer READ_FILE/GREP directly."
)

# Personal-document query detection lives in storage.personal_kb so the
# coordinator can share it (forcing such queries local) without importing this
# heavier module.
from storage.personal_kb import is_personal_query as _is_personal_query


@dataclass
class AgentStep:
    action: str
    args: str = ""
    body: str = ""          # multi-line content (e.g. file content)
    result: Optional[str] = None
    success: Optional[bool] = None
    latency_ms: float = 0.0
    # 1-based indices of steps this one depends on (gap A). Empty = no declared
    # dependency. Parsed from an optional `(after: N, M)` / `[deps: N]` annotation.
    deps: list[int] = field(default_factory=list)
    # Saga compensation args captured at EXECUTE time (e.g. a WRITE_FILE
    # pre-write snapshot: JSON {path, existed, backup}). When set, it overrides
    # the static _compensation_for default so rollback can RESTORE an overwritten
    # file instead of blindly deleting it.
    comp_args: Optional[str] = None

    # --- New fields for saga integrity ---
    run_id: Optional[int] = None
    step_num: Optional[int] = None
    db_id: Optional[int] = None
    comp_id: Optional[int] = None


_DEPS_PATTERN = re.compile(
    r"(?:after|deps|depends\s+on)\s*[:=]?\s*([\d,\s&and]+)", re.IGNORECASE
)


def _parse_deps(line: str) -> list[int]:
    """Extract 1-based dependency step numbers from a plan line annotation.

    Recognises e.g. '(after: 1, 3)', '[deps 2]', 'depends on 1 and 2'. Returns a
    sorted, de-duplicated list; empty when no annotation is present.
    """
    m = _DEPS_PATTERN.search(line)
    if not m:
        return []
    nums = {int(tok) for tok in re.findall(r"\d+", m.group(1))}
    return sorted(nums)


@dataclass
class AgentResult:
    goal: str
    domain: str
    model_used: str
    steps: list[AgentStep] = field(default_factory=list)
    response_text: str = ""    # for single-turn (non-plan) responses
    success: bool = True
    error: Optional[str] = None
    total_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

def _extract_json_obj(text: str) -> dict:
    """Best-effort extraction of a single JSON object from model text.

    Tolerates a ```json code fence and surrounding prose by taking the span
    from the first '{' to the last '}'. Returns {} when nothing parses (the
    caller then skips — e.g. CAS verification degrades to no-op). Never raises.
    """
    if not text:
        return {}
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b <= a:
        return {}
    try:
        obj = json.loads(s[a:b + 1])
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


@dataclass
class DroppedStep:
    """A step the structured parse could not accept (specs/dev-agent-plan-contract)."""
    index: int          # 1-based position in the model's `steps` array
    raw_action: str     # what the model emitted (may be "")
    reason: str         # "unknown action" | "not an object"


@dataclass
class PlanParseReport:
    """Outcome of a structured plan parse, recording drops instead of swallowing.

    `parsed_ok` is True when the response was a JSON object with a `steps` array
    (even if some items were dropped); False means the caller should try the
    regex fallback. `dropped` names every item that didn't make it into `steps`.
    """
    steps: list[AgentStep]
    dropped: list[DroppedStep]
    parsed_ok: bool


def _parse_plan_json_report(text: str) -> PlanParseReport:
    """Structured-output (Ollama `format`) plan parse that RECORDS dropped steps
    instead of silently skipping them (specs/dev-agent-plan-contract R1.1).

    Expects `{"steps": [{action, args, body, after}, ...]}`. Returns
    `parsed_ok=False` (not a raise) when the text isn't an object with a `steps`
    array, so the caller can fall back to the regex parser. Unknown-action /
    malformed items are appended to `dropped` rather than vanishing. Never raises.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return PlanParseReport(steps=[], dropped=[], parsed_ok=False)
    raw_steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(raw_steps, list):
        return PlanParseReport(steps=[], dropped=[], parsed_ok=False)
    steps: list[AgentStep] = []
    dropped: list[DroppedStep] = []
    for idx, item in enumerate(raw_steps, 1):
        if not isinstance(item, dict):
            dropped.append(DroppedStep(index=idx, raw_action="", reason="not an object"))
            continue
        action = str(item.get("action", "")).strip().upper()
        if action not in _PLAN_ACTIONS:
            dropped.append(DroppedStep(index=idx, raw_action=action, reason="unknown action"))
            continue
        args = str(item.get("args", "") or "").strip()
        body = str(item.get("body", "") or "")
        after = item.get("after") or []
        deps = sorted({int(d) for d in after if isinstance(d, (int, float, str))
                       and str(d).strip().lstrip("-").isdigit()})
        steps.append(AgentStep(action=action, args=args, body=body, deps=deps))
    return PlanParseReport(steps=steps, dropped=dropped, parsed_ok=True)


def _parse_plan_json(text: str) -> list[AgentStep]:
    """Back-compat wrapper: parse into steps, raising on a non-step-array response
    so the caller falls back to the regex parser. Unknown verbs are dropped (see
    `_parse_plan_json_report` for the drop-recording variant used by auto-repair)."""
    report = _parse_plan_json_report(text)
    if not report.parsed_ok:
        raise ValueError("plan JSON has no 'steps' array")
    return report.steps


def _build_plan_repair_prompt(report: PlanParseReport) -> str:
    """Corrective message naming what failed in the previous plan, for a bounded
    re-prompt (specs/dev-agent-plan-contract R1.2/R1.3). Reuses `_PLAN_ACTIONS`
    as the single source of valid verbs so it can't drift from the schema."""
    valid = ", ".join(sorted(_PLAN_ACTIONS))
    problems: list[str] = []
    for d in report.dropped:
        if d.reason == "unknown action":
            problems.append(f'- step {d.index} used an unknown action "{d.raw_action}"')
        else:
            problems.append(f"- step {d.index} was malformed ({d.reason})")
    if not report.steps and not report.dropped:
        problems.append("- no valid \"steps\" array was found in your response")
    problem_block = "\n".join(problems) or "- the plan could not be fully parsed"
    return (
        "Your previous plan could not be fully parsed and was NOT executed:\n"
        f"{problem_block}\n\n"
        'Re-emit the COMPLETE plan as a JSON object of the form '
        '{"steps": [{"action": <ACTION>, "args": "...", "body": "...", "after": [n]}]}. '
        f"Use ONLY these actions: {valid}. Include every step you intend to run."
    )


def _parse_plan(text: str) -> list[AgentStep]:
    """Extract AgentStep objects from a free-text planner response (fallback)."""
    steps: list[AgentStep] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _STEP_PATTERN.match(lines[i])
        if m:
            action = m.group(1).upper()
            args = (m.group(2) or "").strip()
            deps = _parse_deps(lines[i])    # gap A: optional dependency annotation
            # Collect body lines until the next step or end
            body_lines = []
            i += 1
            while i < len(lines):
                if _STEP_PATTERN.match(lines[i]):
                    break
                body_lines.append(lines[i])
                i += 1
            # Strip leading/trailing blank lines from body
            body = "\n".join(body_lines).strip()
            # Remove markdown code fences from body
            body = re.sub(r"^```[a-z]*\n?", "", body, flags=re.MULTILINE)
            body = re.sub(r"\n?```$", "", body, flags=re.MULTILINE).strip()
            steps.append(AgentStep(action=action, args=args, body=body, deps=deps))
        else:
            i += 1
    return steps