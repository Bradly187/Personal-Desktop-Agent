import json
import logging
from inference.plan_parser import AgentStep, _extract_json_obj

log = logging.getLogger(__name__)

async def verify_math_with_cas(executor, question: str, answer: str) -> str:
    reg = getattr(executor, "_skill_registry", None)
    if reg is None or not reg.tool_schema("sympy", "verify"):
        return ""
    try:
        spec = await extract_cas_check(executor, question, answer)
        if not spec or not spec.get("kind"):
            return ""
        args = {
            "kind": str(spec.get("kind", "")),
            "expression": str(spec.get("expression", "") or ""),
            "variable": str(spec.get("variable") or "x"),
            "claimed": str(spec.get("claimed") or ""),
            "lower": str(spec.get("lower") or ""),
            "upper": str(spec.get("upper") or ""),
        }
        if not args["expression"]:
            return ""
        step = AgentStep(action="SKILL_QUERY",
                         args=f"sympy verify {json.dumps(args)}")
        
        from inference.executors.skill_executor import execute_skill_step
        verdict = (await execute_skill_step(executor, step) or "").strip()
        if not verdict or verdict.startswith("No CAS-checkable"):
            return ""
        return f"**SymPy verification:** {verdict}"
    except Exception as exc:
        log.debug("math CAS verification skipped: %s", exc)
        return ""

async def extract_cas_check(executor, question: str, answer: str) -> dict:
    prompt = (
        "You convert a solved math problem into ONE machine-checkable SymPy "
        "verification. Output ONLY a JSON object, no other text.\n"
        "Keys:\n"
        '  "kind": one of "solve","integrate","differentiate","simplify",'
        '"factor","evaluate" — or null if the problem is a proof or '
        "conceptual answer with no single closed-form result to check.\n"
        '  "expression": the core expression or equation, SymPy-parseable '
        "(use ** for powers, * for multiplication; for solve include the "
        "full equation).\n"
        '  "variable": the main variable (default "x").\n'
        '  "claimed": the answer\'s final result as a SymPy-parseable '
        "expression (for solve: comma-separated roots; for a definite "
        "integral: the numeric value), or null if unclear.\n"
        '  "lower","upper": the integration bounds for a definite integral, '
        "else null.\n\n"
        f"Problem: {question}\n\nProposed answer:\n{answer[:1500]}"
    )
    r = await executor._router.infer(domain="general", user_text=prompt)
    if not getattr(r, "ok", False) or not getattr(r, "text", ""):
        return {}
    return _extract_json_obj(r.text)
