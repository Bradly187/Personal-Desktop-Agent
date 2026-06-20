"""sympy_server — local, offline symbolic-math skill (stdio MCP).

The math-tool the planner reaches for when it needs a GROUND-TRUTH computation
rather than the math model's reasoning. `deepseek-r1:8b` reasons well but, like
every LLM, can quietly get the final algebra/numeric wrong; a SKILL_QUERY to
this server returns the exact result, which the model then explains.

Fully local — pure CPU compute, NO network, NO credentials, NO egress — so it
stays consistent with the single-machine, local-first posture and never needs
the outbound scrub/voice-gate path (every tool is read-only; send_tools == []).

Security note on parsing: untrusted-ish expression strings are parsed with
sympy's `parse_expr` over a CURATED global namespace (only safe math names) and
the auto-symbol transformation, so an unknown name like `os` becomes the symbol
`os`, never an attribute/callable — `sympify`/`eval` on the raw string is never
used. Input here originates from the trusted single user, but the tight parser
keeps a malformed/hostile expression from doing anything but fail to parse.

Capabilities (all read-only):
  solve_equation          — real/complex solutions of an equation in one variable
  integrate_expression    — definite or indefinite integral
  differentiate_expression— nth derivative
  simplify_expression     — algebraic simplification
  factor_expression       — factor a polynomial/expression
  evaluate_numeric        — high-precision numeric value

Each result is returned as plain text PLUS a LaTeX line, so the math model can
drop it straight into its `$...$` / `$$...$$` answer.

Run standalone:  python -m skills.servers.sympy_server
"""

from __future__ import annotations

import sympy
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sympy")

# standard_transformations already includes auto_symbol (undefined names ->
# Symbol). We add implicit multiplication ("2x" -> 2*x) and `^` -> `**` so the
# user can phrase expressions naturally.
_TRANSFORMS = standard_transformations + (
    implicit_multiplication_application,
    convert_xor,
)

# The ONLY names parse_expr may resolve to real objects. Anything else the
# auto-symbol transformation turns into a plain Symbol — so the parser can never
# reach `__import__`, attribute access, or arbitrary callables.
_SAFE_NAMES = {
    name: getattr(sympy, name)
    for name in (
        "sin", "cos", "tan", "cot", "sec", "csc",
        "asin", "acos", "atan", "atan2",
        "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
        "exp", "log", "sqrt", "Abs", "sign",
        "factorial", "gamma", "binomial",
        "pi", "E", "I", "oo", "zoo", "nan",
        "Rational", "Integer", "Float", "Symbol", "symbols",
        "Sum", "Product", "Derivative", "Integral", "Matrix",
    )
}
_SAFE_NAMES["ln"] = sympy.log  # common alias for natural log


def _parse(expr_str: str):
    """Parse one expression over the curated namespace. Raises ValueError on an
    empty/unparseable string (callers convert that to a readable message)."""
    expr_str = (expr_str or "").strip()
    if not expr_str:
        raise ValueError("empty expression")
    return parse_expr(
        expr_str,
        transformations=_TRANSFORMS,
        global_dict=_SAFE_NAMES,
        evaluate=True,
    )


def _fmt(result) -> str:
    """A result rendered as plain text plus a LaTeX line for the math model."""
    try:
        latex = sympy.latex(result)
    except Exception:                      # exotic objects without a latex form
        latex = ""
    return f"{result}" + (f"\nLaTeX: {latex}" if latex else "")


# ---------------------------------------------------------------------------
# Plain logic (no MCP, no network, unit-testable) — each catches its own
# errors and returns a readable string, mirroring arxiv_server._query_api.
# ---------------------------------------------------------------------------

def _solve(equation: str, variable: str = "x") -> str:
    try:
        sym = sympy.Symbol(variable)
        if "=" in equation:
            lhs, rhs = equation.split("=", 1)
            expr = _parse(lhs) - _parse(rhs)
        else:
            expr = _parse(equation)
        sols = sympy.solve(expr, sym)
        if not sols:
            return f"No solutions found for {variable}."
        plain = ", ".join(f"{variable} = {s}" for s in sols)
        latex = ", ".join(f"{variable} = {sympy.latex(s)}" for s in sols)
        return f"{plain}\nLaTeX: {latex}"
    except Exception as exc:
        return f"Could not solve {equation!r}: {exc}"


def _integrate(expression: str, variable: str = "x",
               lower: str = "", upper: str = "") -> str:
    try:
        sym = sympy.Symbol(variable)
        expr = _parse(expression)
        lower, upper = str(lower).strip(), str(upper).strip()
        if lower and upper:
            result = sympy.integrate(expr, (sym, _parse(lower), _parse(upper)))
            return _fmt(result)
        result = sympy.integrate(expr, sym)
        return f"{result} + C\nLaTeX: {sympy.latex(result)} + C"
    except Exception as exc:
        return f"Could not integrate {expression!r}: {exc}"


def _differentiate(expression: str, variable: str = "x", order: int = 1) -> str:
    try:
        sym = sympy.Symbol(variable)
        n = max(1, int(order))
        return _fmt(sympy.diff(_parse(expression), sym, n))
    except Exception as exc:
        return f"Could not differentiate {expression!r}: {exc}"


def _simplify(expression: str) -> str:
    try:
        return _fmt(sympy.simplify(_parse(expression)))
    except Exception as exc:
        return f"Could not simplify {expression!r}: {exc}"


def _factor(expression: str) -> str:
    try:
        return _fmt(sympy.factor(_parse(expression)))
    except Exception as exc:
        return f"Could not factor {expression!r}: {exc}"


def _evaluate(expression: str, digits: int = 15) -> str:
    try:
        d = max(1, min(int(digits), 100))
        return f"{sympy.N(_parse(expression), d)}"
    except Exception as exc:
        return f"Could not evaluate {expression!r}: {exc}"


# ---------------------------------------------------------------------------
# Verification — independent ground-truth recompute + agreement check.
# Powers the math model's "always verify against the CAS" path: the model's
# free-form answer is reduced (upstream) to {kind, expression, claimed}, and
# this recomputes the result and compares it to the claimed answer. All the
# symbolic-equivalence logic lives here, where SymPy is the dependency.
# ---------------------------------------------------------------------------

_VERIFY_KINDS = {"solve", "integrate", "differentiate",
                 "simplify", "factor", "evaluate"}


def _compute_raw(kind: str, expression: str, variable: str = "x",
                 lower: str = "", upper: str = ""):
    """Ground-truth result as a SymPy object (list of roots for 'solve')."""
    sym = sympy.Symbol(variable)
    if kind == "solve":
        if "=" in expression:
            lhs, rhs = expression.split("=", 1)
            e = _parse(lhs) - _parse(rhs)
        else:
            e = _parse(expression)
        return sympy.solve(e, sym)
    expr = _parse(expression)
    if kind == "integrate":
        lo, hi = str(lower).strip(), str(upper).strip()
        if lo and hi:
            return sympy.integrate(expr, (sym, _parse(lo), _parse(hi)))
        return sympy.integrate(expr, sym)
    if kind == "differentiate":
        return sympy.diff(expr, sym)
    if kind == "simplify":
        return sympy.simplify(expr)
    if kind == "factor":
        return sympy.factor(expr)
    if kind == "evaluate":
        return sympy.N(expr)
    raise ValueError(f"unknown kind: {kind}")


def _truth_str(kind: str, truth, variable: str) -> str:
    if kind == "solve":
        if not truth:
            return "no solutions"
        return ", ".join(f"{variable} = {s}" for s in truth)
    return f"{truth}"


def _equiv_val(a, b) -> bool:
    try:
        return sympy.simplify(a - b) == 0
    except Exception:
        return False


def _parse_root_list(claimed: str, variable: str):
    """Parse a claimed solution set ('x = 2, x = -2', '2, -2', '±2', '2 or -2')
    into a list of SymPy values, or None if it can't be parsed cleanly."""
    s = claimed.replace(f"{variable} =", "").replace(f"{variable}=", "")
    s = s.replace(" or ", ",").replace(";", ",")
    out = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk[0] in "±∓" or chunk.startswith("+-") or chunk.startswith("-+"):
            base = chunk.lstrip("±∓+- ").strip()
            try:
                v = _parse(base)
            except Exception:
                return None
            out.append(v)
            out.append(-v)
        else:
            try:
                out.append(_parse(chunk))
            except Exception:
                return None
    return out or None


def _agree(kind: str, truth, claimed: str, variable: str):
    """True/False if the claim can be compared to the ground truth; None if the
    claim couldn't be parsed/compared (verdict then stays 'uncertain')."""
    try:
        if kind == "solve":
            claimed_vals = _parse_root_list(claimed, variable)
            if claimed_vals is None:
                return None
            truth_vals = list(truth)
            if len(claimed_vals) != len(truth_vals):
                return False
            return all(any(_equiv_val(c, t) for t in truth_vals)
                       for c in claimed_vals)
        c = _parse(claimed)
        if kind == "evaluate":
            return abs(complex(sympy.N(truth)) - complex(sympy.N(c))) < 1e-6
        return sympy.simplify(truth - c) == 0
    except Exception:
        return None


def _verify(kind: str, expression: str, variable: str = "x",
            claimed: str = "", lower: str = "", upper: str = "") -> str:
    kind = (kind or "").strip().lower()
    if kind not in _VERIFY_KINDS:
        return "No CAS-checkable claim."
    try:
        truth = _compute_raw(kind, expression, variable, lower, upper)
    except Exception as exc:
        return f"SymPy could not compute the check: {exc}"
    truth_s = _truth_str(kind, truth, variable)
    claimed = (claimed or "").strip()
    if not claimed:
        return f"SymPy independently computed: {truth_s}"
    verdict = _agree(kind, truth, claimed, variable)
    if verdict is True:
        return f"AGREE — SymPy independently computed {truth_s}, matching the answer."
    if verdict is False:
        return (f"DISAGREE — SymPy independently computed {truth_s}, "
                f"but the answer states {claimed}.")
    return (f"SymPy independently computed: {truth_s} "
            f"(couldn't auto-compare to the stated answer).")


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def solve_equation(equation: str, variable: str = "x") -> str:
    """Solve an equation for a variable (read-only). Accepts an equation with
    '=' (e.g. 'x^2 + 2x = 3') or an expression assumed equal to zero."""
    return _solve(equation, variable)


@mcp.tool()
def integrate_expression(expression: str, variable: str = "x",
                         lower: str = "", upper: str = "") -> str:
    """Integrate an expression (read-only). Pass both lower and upper for a
    definite integral; omit them for the indefinite integral (+ C)."""
    return _integrate(expression, variable, lower, upper)


@mcp.tool()
def differentiate_expression(expression: str, variable: str = "x",
                             order: int = 1) -> str:
    """Differentiate an expression to the nth order (read-only)."""
    return _differentiate(expression, variable, order)


@mcp.tool()
def simplify_expression(expression: str) -> str:
    """Algebraically simplify an expression (read-only)."""
    return _simplify(expression)


@mcp.tool()
def factor_expression(expression: str) -> str:
    """Factor a polynomial or expression (read-only)."""
    return _factor(expression)


@mcp.tool()
def evaluate_numeric(expression: str, digits: int = 15) -> str:
    """Evaluate an expression to a high-precision numeric value (read-only)."""
    return _evaluate(expression, digits)


@mcp.tool()
def verify(kind: str, expression: str, variable: str = "x",
           claimed: str = "", lower: str = "", upper: str = "") -> str:
    """Independently recompute a result and compare it to a claimed answer
    (read-only). `kind` is one of solve/integrate/differentiate/simplify/
    factor/evaluate. Used to verify the math model's answer against the CAS."""
    return _verify(kind, expression, variable, claimed, lower, upper)


if __name__ == "__main__":
    mcp.run(transport="stdio")
