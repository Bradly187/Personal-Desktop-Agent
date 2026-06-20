"""sympy skill — local symbolic-math MCP server.

Plain logic functions tested directly (no network, no model, deterministic);
one real stdio round-trip proves the end-to-end MCP loop; manifest validation +
a hijack guard protect routing. Mirrors tests/test_skill_breadth.py.

Run:
    python -m pytest tests/test_sympy_skill.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import sympy

from skills.servers import sympy_server as sm

_MANIFEST_DIR = Path(__file__).parent.parent / "skills" / "manifests"


# ---------------------------------------------------------------------------
# solve
# ---------------------------------------------------------------------------

def test_solve_quadratic_with_equals():
    out = sm._solve("x^2 + 2x = 3", "x")
    assert "x = 1" in out and "x = -3" in out
    assert "LaTeX:" in out


def test_solve_expression_assumed_zero():
    out = sm._solve("x^2 - 4", "x")
    assert "x = 2" in out and "x = -2" in out


def test_solve_other_variable():
    out = sm._solve("2*y - 10 = 0", "y")
    assert "y = 5" in out


def test_solve_no_solution_is_honest():
    assert "No solutions" in sm._solve("x = x + 1", "x")


def test_solve_bad_input_is_readable():
    out = sm._solve(")(", "x")
    assert "Could not solve" in out


# ---------------------------------------------------------------------------
# integrate
# ---------------------------------------------------------------------------

def test_indefinite_integral_has_constant():
    out = sm._integrate("x^2", "x")
    assert "x**3/3" in out and "+ C" in out


def test_definite_integral_exact():
    out = sm._integrate("x^2", "x", "0", "1")
    assert out.startswith("1/3")
    assert "LaTeX:" in out


def test_integral_of_sin():
    out = sm._integrate("sin(x)", "x")
    assert "-cos(x)" in out


# ---------------------------------------------------------------------------
# differentiate / simplify / factor / evaluate
# ---------------------------------------------------------------------------

def test_first_derivative():
    out = sm._differentiate("x^3 + 2x", "x")
    assert "3*x**2 + 2" in out


def test_second_derivative_via_order():
    out = sm._differentiate("x^3", "x", order=2)
    assert "6*x" in out


def test_simplify_trig_identity():
    out = sm._simplify("sin(x)^2 + cos(x)^2")
    assert out.startswith("1")


def test_factor_polynomial():
    out = sm._factor("x^2 - 1")
    assert "(x - 1)*(x + 1)" in out


def test_evaluate_numeric_pi():
    out = sm._evaluate("pi", digits=10)
    assert out.startswith("3.14159")


def test_evaluate_exact_fraction():
    assert sm._evaluate("2/3 + 1/6", digits=5).startswith("0.83333")


# ---------------------------------------------------------------------------
# Parser safety — hostile/unknown names degrade to symbols, never execute
# ---------------------------------------------------------------------------

def test_unknown_name_degrades_to_symbols():
    # An unknown name resolves to plain Symbol(s) — never the `os` module.
    # (implicit-multiplication splits "os" into o*s; both are Symbols.)
    expr = sm._parse("os")
    assert expr.free_symbols
    assert all(isinstance(s, sympy.Symbol) for s in expr.free_symbols)
    assert "module" not in str(type(expr)).lower()


def test_namespace_excludes_dangerous_builtins():
    for bad in ("__import__", "os", "sys", "eval", "exec", "open", "system"):
        assert bad not in sm._SAFE_NAMES


def test_dangerous_payload_does_not_execute(tmp_path):
    # Observable proof of no-exec: if the parser eval'd its input this file would
    # be created. The curated namespace + auto-symbol parser must never run it.
    marker = tmp_path / "pwned.txt"
    payload = f"__import__('pathlib').Path(r'{marker}').write_text('x')"
    sm._evaluate(payload)
    assert not marker.exists()


def test_empty_expression_rejected():
    assert "Could not evaluate" in sm._evaluate("   ")


# ---------------------------------------------------------------------------
# verify — independent recompute + agreement verdict
# ---------------------------------------------------------------------------

def test_verify_solve_agrees():
    out = sm._verify("solve", "x^2 - 4", "x", claimed="2, -2")
    assert out.startswith("AGREE")


def test_verify_solve_agrees_with_plusminus_and_prefix():
    assert sm._verify("solve", "x^2 - 4", "x", claimed="±2").startswith("AGREE")
    assert sm._verify("solve", "x^2 - 4", "x",
                      claimed="x = 2, x = -2").startswith("AGREE")


def test_verify_solve_disagrees():
    out = sm._verify("solve", "x^2 - 4", "x", claimed="2, 3")
    assert out.startswith("DISAGREE")
    assert "x = 2" in out and "x = -2" in out


def test_verify_definite_integral_agrees():
    out = sm._verify("integrate", "x^2", "x", claimed="1/3", lower="0", upper="1")
    assert out.startswith("AGREE")


def test_verify_derivative_disagrees():
    out = sm._verify("differentiate", "x^3", "x", claimed="2*x**2")
    assert out.startswith("DISAGREE")
    assert "3*x**2" in out


def test_verify_evaluate_numeric_close():
    assert sm._verify("evaluate", "pi", "x", claimed="3.14159265").startswith("AGREE")


def test_verify_uncertain_when_claim_unparseable():
    out = sm._verify("simplify", "sin(x)^2 + cos(x)^2", "x", claimed="the identity")
    assert "independently computed" in out and "1" in out


def test_verify_no_claim_reports_truth_only():
    out = sm._verify("factor", "x^2 - 1", "x", claimed="")
    assert "independently computed" in out and "(x - 1)*(x + 1)" in out


def test_verify_rejects_unknown_kind():
    assert sm._verify("prove", "x", "x", claimed="").startswith("No CAS-checkable")


# ---------------------------------------------------------------------------
# DevAgent integration — math answers are verified against the CAS
# ---------------------------------------------------------------------------

def _router_returning(math_text: str, extract_json: str):
    """Mock ModelRouter: math infer returns the answer; the general-domain
    extraction infer returns the JSON spec. Distinguished by domain kwarg."""
    router = MagicMock()

    async def _infer(domain=None, user_text="", **kw):
        return _RR(math_text if domain == "math" else extract_json)
    router.infer = AsyncMock(side_effect=_infer)
    return router


class _RR:
    def __init__(self, text):
        self.text = text
        self.model = "deepseek-r1:8b"
        self.domain = "math"
        self.error = None
        self.ok = True


async def _sympy_registry(tmp_path):
    from skills.registry import SkillRegistry
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    manifest = json.loads((_MANIFEST_DIR / "sympy.json").read_text(encoding="utf-8"))
    manifest["server"]["command"] = sys.executable
    (mdir / "sympy.json").write_text(json.dumps(manifest), encoding="utf-8")
    reg = SkillRegistry(manifest_dir=mdir)
    await reg.start()
    return reg


async def test_math_answer_appends_cas_agreement(tmp_path):
    from inference.dev_agent import DevAgent
    reg = await _sympy_registry(tmp_path)
    try:
        router = _router_returning(
            "The roots are $x = \\pm 2$.",
            '{"kind":"solve","expression":"x^2 - 4","variable":"x","claimed":"2, -2"}',
        )
        agent = DevAgent(router=router)
        agent._classifier = MagicMock()
        agent._classifier.classify = MagicMock(return_value="math")
        agent._rag_context = AsyncMock(return_value="")
        agent._format_context = lambda: ""
        agent._push_context = lambda *a, **k: None
        agent._persist_run = AsyncMock()
        agent.set_skill_registry(reg)

        res = await agent.handle("solve x^2 = 4")
        assert res.domain == "math"
        assert "SymPy verification:" in res.response_text
        assert "AGREE" in res.response_text
    finally:
        await reg.stop()
        from core.domain_classifier import DomainClassifier
        DomainClassifier.register_skill_keywords(set())


async def test_math_verification_no_op_when_not_checkable(tmp_path):
    from inference.dev_agent import DevAgent
    reg = await _sympy_registry(tmp_path)
    try:
        router = _router_returning(
            "This is a proof by induction; the base case holds and ...",
            '{"kind": null}',
        )
        agent = DevAgent(router=router)
        agent._classifier = MagicMock()
        agent._classifier.classify = MagicMock(return_value="math")
        agent._rag_context = AsyncMock(return_value="")
        agent._format_context = lambda: ""
        agent._push_context = lambda *a, **k: None
        agent._persist_run = AsyncMock()
        agent.set_skill_registry(reg)

        res = await agent.handle("prove the sum formula")
        assert "SymPy verification:" not in res.response_text
    finally:
        await reg.stop()
        from core.domain_classifier import DomainClassifier
        DomainClassifier.register_skill_keywords(set())


def test_extract_json_obj_tolerates_fences_and_prose():
    from inference.dev_agent import _extract_json_obj
    assert _extract_json_obj('```json\n{"kind":"solve"}\n```')["kind"] == "solve"
    assert _extract_json_obj('Sure! {"a": 1} done')["a"] == 1
    assert _extract_json_obj("no json here") == {}
    assert _extract_json_obj("") == {}


# ---------------------------------------------------------------------------
# Manifest + routing safety
# ---------------------------------------------------------------------------

def _manifest() -> dict:
    return json.loads((_MANIFEST_DIR / "sympy.json").read_text(encoding="utf-8"))


def test_manifest_valid_and_enabled():
    m = _manifest()
    assert m["skill_id"] == "sympy"
    assert m["enabled"] is True              # local, offline, no egress → on
    assert m["transport"] == "stdio"
    assert m["tools"]["send_tools"] == []    # every tool is read-only
    for intent in m["intents"].values():
        assert intent["tool"] in m["tools"]["allow"]
        assert intent["keywords"]
        assert intent["plan"] is True        # NL → expression needs the planner


def test_keywords_do_not_hijack_other_domains():
    # Math intent keywords must not steal code/command/personal utterances —
    # the bare-word ("integrate", "factor") false-positive trap.
    utterances = [
        "integrate the payment service with stripe",
        "find my bug in the parser",
        "search my codebase for the fusion tick loop",
        "write a python function to train a transformer model",
        "prove that the matrix is positive definite",
        "scroll down", "open the web browser", "click the button",
        "what are my reminders", "review queue",
        "what did I write in my notes about my doctor",
    ]
    keywords = [kw.lower()
                for intent in _manifest()["intents"].values()
                for kw in intent["keywords"]]
    for u in utterances:
        hits = [kw for kw in keywords if kw in u.lower()]
        assert not hits, f"{hits} hijacks {u!r}"


# ---------------------------------------------------------------------------
# One real stdio round-trip — proves the end-to-end MCP loop
# ---------------------------------------------------------------------------

async def test_sympy_skill_end_to_end(tmp_path):
    from skills.registry import SkillRegistry

    mdir = tmp_path / "manifests"
    mdir.mkdir()
    manifest = _manifest()
    manifest["server"]["command"] = sys.executable
    (mdir / "sympy.json").write_text(json.dumps(manifest), encoding="utf-8")

    reg = SkillRegistry(manifest_dir=mdir)
    await reg.start()
    try:
        assert reg.has_skills()
        res = await reg.call("sympy", "differentiate_expression",
                             {"expression": "x^3 + 2x", "variable": "x"})
        assert res["status"] == "ok"
        assert "3*x**2 + 2" in res["text"]
        # read-only tool must not be flagged as a gated send
        assert reg.is_send_tool("sympy", "differentiate_expression") is False
    finally:
        await reg.stop()
        from core.domain_classifier import DomainClassifier
        DomainClassifier.register_skill_keywords(set())   # don't leak keywords
