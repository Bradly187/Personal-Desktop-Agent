"""Independent code Critic for the DevAgent WRITE_FILE path (specs/dev-agent-critic).

The plan→execute→reflect loop has no independent review of generated code before
it is accepted — `_reflect` is the model summarizing its OWN work. This adds the
"Critic" pattern: after a WRITE_FILE edit passes the lint gate
(`inference/edit_format.py`) but BEFORE it commits, a fresh-context, adversarial
review pass judges the diff against the step goal and returns a structured verdict.

"Independent" here means independent *context and role*, not independent *weights*
(single-machine, VRAM-bounded — AGENTS.md #6): the Critic reuses the already-loaded
plan/general model with a reviewer role-prompt and NO trajectory/generator context.
It generalizes the in-tree precedent `DevAgent._verify_math_with_cas` (generate →
independent verify → verdict) from math to code.

Pure orchestration over ONE model call; deterministic verdict parsing (regex/JSON,
never raises into the loop — the caller treats any failure as fail-safe escalate).
The model is injected as a `router` with an async `infer(domain, user_text, context)`
returning an object with `.text` / `.ok`, so this module is unit-tested without a GPU.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Verdict vocabulary.
PASS = "pass"        # change correctly accomplishes the goal; no real problems
REVISE = "revise"    # fixable problems — do not commit; replan with the findings
BLOCK = "block"      # serious correctness/security problem; must not be applied
_DECISIONS = frozenset({PASS, REVISE, BLOCK})

# Severities the Critic treats as never-passable (a flagged one floors PASS→REVISE).
_DEFAULT_BLOCK_SEVERITIES = ("security", "correctness")

# Keep the reviewed diff bounded so the review call stays cheap.
_MAX_DIFF_CHARS = 6000


@dataclass
class Finding:
    severity: str        # "correctness" | "security" | "style" | "info"
    message: str
    target: str = ""     # file/line/symbol the finding concerns


@dataclass
class CriticVerdict:
    decision: str                        # PASS | REVISE | BLOCK
    confidence: float = 0.0              # 0..1
    findings: list = field(default_factory=list)   # list[Finding]
    suggested_fix: str = ""
    escalate: bool = False               # set by the caller: force the approval gate

    def summary(self) -> str:
        head = f"{self.decision} (conf={self.confidence:.2f})"
        if self.findings:
            head += ": " + "; ".join(f"[{f.severity}] {f.message}" for f in self.findings[:3])
        return head


_REVIEW_PROMPT = """\
[IDENTITY]
You are an independent, pessimistic, and highly critical Code Security & Correctness Reviewer (The Scrutinizer). You did NOT write this change.

[PRIME DIRECTIVE]
Tear the proposed change apart. Find logic flaws, security vulnerabilities, and failures to meet the stated GOAL.

[STRICT CONSTRAINTS]
- Do not generate new features or expand the scope of the goal.
- Do not fix the code for them; only provide short hints if necessary.
- Do not compliment the code.
- Do not pass a change if you are uncertain (prefer "revise" over "pass").

[FORMAT]
Respond with ONLY a JSON object and nothing else:
{{"decision": "pass" | "revise" | "block",
  "confidence": <0.0-1.0>,
  "findings": [{{"severity": "correctness"|"security"|"style"|"info", "message": "...", "target": "..."}}],
  "suggested_fix": "<short hint or empty>"}}

- "pass": the change flawlessly accomplishes the goal with absolutely no issues.
- "revise": there are fixable problems — list each in findings.
- "block": a serious correctness or security problem; the change must NOT be applied.
Be terse.

[CONTEXT]
GOAL: {goal}
FILE: {path}

CHANGE (unified diff, old -> new):
{diff}"""


_PLAN_REVIEW_PROMPT = """\
[IDENTITY]
You are an independent, pessimistic, and highly critical Plan Reviewer (The Scrutinizer). You did NOT write this plan.

[PRIME DIRECTIVE]
Review the proposed recovery plan strictly against the stated GOAL. Look for correctness bugs, logical errors, or repeated failed steps.

[STRICT CONSTRAINTS]
- Do not generate new features or expand the scope of the goal.
- Do not write the code for them.
- Do not compliment the plan.
- Do not pass a plan if you are uncertain (prefer "revise" over "pass").

[FORMAT]
Respond with ONLY a JSON object and nothing else:
{{"decision": "pass" | "revise" | "block",
  "confidence": <0.0-1.0>,
  "findings": [{{"severity": "correctness"|"security"|"style"|"info", "message": "...", "target": "..."}}],
  "suggested_fix": "<short hint or empty>"}}

- "pass": the plan flawlessly addresses the goal.
- "revise": there are fixable problems or repeated failures — list each in findings.
- "block": a serious safety or correctness issue.
Be terse.

[CONTEXT]
GOAL: {goal}

RECOVERY PLAN:
{plan}"""


def _build_diff(old_text: str, new_text: str, path: str) -> str:
    """A bounded unified diff old->new (or the new file when there is no old)."""
    if not old_text:
        body = new_text[:_MAX_DIFF_CHARS]
        return f"(new file)\n{body}"
    diff = difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    )
    text = "\n".join(diff)
    if len(text) > _MAX_DIFF_CHARS:
        text = text[:_MAX_DIFF_CHARS] + "\n… [diff truncated]"
    return text or "(no textual change)"


def _extract_json_obj(text: str) -> dict:
    """Best-effort single-JSON-object extraction (tolerates a code fence + prose)."""
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


def parse_verdict(text: str, block_severities=_DEFAULT_BLOCK_SEVERITIES) -> CriticVerdict:
    """Parse a model verdict into a CriticVerdict (deterministic, never raises).

    Conservative on ambiguity (AGENTS.md #4): an unparseable/empty response or an
    unknown decision becomes REVISE (do not silently pass). A finding whose
    severity is in ``block_severities`` floors a PASS up to at least REVISE — a
    flagged security/correctness issue can never slip through as a pass.
    """
    obj = _extract_json_obj(text)
    if not obj:
        return CriticVerdict(decision=REVISE, confidence=0.0,
                             findings=[Finding("info", "unparseable critic response")])
    decision = str(obj.get("decision", "")).strip().lower()
    if decision not in _DECISIONS:
        decision = REVISE
    try:
        confidence = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    findings: list = []
    for f in obj.get("findings", []) or []:
        if isinstance(f, dict):
            findings.append(Finding(
                severity=str(f.get("severity", "info")).strip().lower() or "info",
                message=str(f.get("message", "")).strip(),
                target=str(f.get("target", "")).strip(),
            ))
    # Safety floor: a flagged block-severity finding cannot pass.
    if decision == PASS and any(f.severity in block_severities for f in findings):
        decision = REVISE
    return CriticVerdict(decision=decision, confidence=confidence, findings=findings,
                         suggested_fix=str(obj.get("suggested_fix", "")).strip())


class Critic:
    """Reviews a resulting WRITE_FILE edit against the step goal, on the already-
    loaded plan/general model with a fresh reviewer context (no generator CoT)."""

    def __init__(self, router, model_domain: str = "plan",
                 block_severities=_DEFAULT_BLOCK_SEVERITIES) -> None:
        self._router = router
        self._model_domain = model_domain
        self._block_severities = tuple(block_severities)

    async def review(self, goal: str, path: str, old_text: str, new_text: str) -> CriticVerdict:
        """Return a structured verdict. Raises on an inference failure so the
        caller can apply its fail-safe (escalate-to-confirm, never silent pass)."""
        prompt = _REVIEW_PROMPT.format(
            goal=(goal or "(no goal stated)")[:500],
            path=path or "(unknown)",
            diff=_build_diff(old_text, new_text, path),
        )
        # Fresh context: only the review prompt — the Critic must not see the
        # generator's reasoning (R1.2). context="" keeps it independent.
        result = await self._router.infer(
            domain=self._model_domain, user_text=prompt, context="",
        )
        if not getattr(result, "ok", True):
            raise RuntimeError(getattr(result, "error", None) or "critic inference not ok")
        verdict = parse_verdict(getattr(result, "text", "") or "", self._block_severities)
        log.info("Critic[%s]: %s", path, verdict.summary())
        return verdict

    async def review_plan(self, goal: str, plan_text: str) -> CriticVerdict:
        """Review a proposed recovery plan against the stated goal."""
        prompt = _PLAN_REVIEW_PROMPT.format(
            goal=(goal or "(no goal stated)")[:500],
            plan=plan_text[:6000],
        )
        result = await self._router.infer(
            domain=self._model_domain, user_text=prompt, context="",
        )
        if not getattr(result, "ok", True):
            raise RuntimeError(getattr(result, "error", None) or "critic plan inference not ok")
        verdict = parse_verdict(getattr(result, "text", "") or "", self._block_severities)
        log.info("Critic[Replan]: %s", verdict.summary())
        return verdict
