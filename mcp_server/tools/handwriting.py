"""Handwritten math expression recognizer using pix2tex (LaTeX OCR).

pix2tex runs a ViT + GPT-2 model on CUDA and converts a PNG of handwritten or
printed math into a LaTeX string. The model is loaded lazily on first call so
startup time is unaffected when handwriting is not used.

Requires:
    pip install pix2tex          # ~1.5 GB download, uses CUDA automatically
    pip install pylatexenc       # LaTeX → unicode symbol conversion

Degrades gracefully: if either package is absent the recognizer returns an
error dict instead of raising.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

try:
    from pix2tex.cli import LatexOCR
    _PIX2TEX_AVAILABLE = True
except ImportError:
    _PIX2TEX_AVAILABLE = False
    log.warning("pix2tex not installed — handwriting recognition disabled. "
                "Install with: pip install pix2tex")

try:
    from pylatexenc.latex2text import LatexNodes2Text
    _LATEX2TEXT_AVAILABLE = True
except ImportError:
    _LATEX2TEXT_AVAILABLE = False
    log.warning("pylatexenc not installed — LaTeX→unicode conversion will use "
                "basic fallback. Install with: pip install pylatexenc")


# ---------------------------------------------------------------------------
# Lazy model singleton
# ---------------------------------------------------------------------------

_model: Optional["LatexOCR"] = None


def _get_model() -> "LatexOCR":
    global _model
    if _model is None:
        log.info("Loading pix2tex LaTeX OCR model (first use — may take ~10s) ...")
        _model = LatexOCR()
        log.info("pix2tex model loaded.")
    return _model


# ---------------------------------------------------------------------------
# LaTeX → unicode
# ---------------------------------------------------------------------------

_GREEK = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "θ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ",
    r"\pi": "π", r"\varpi": "π", r"\rho": "ρ", r"\sigma": "σ",
    r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ", r"\varphi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Upsilon": "Υ",
    r"\Phi": "Φ", r"\Psi": "Ψ", r"\Omega": "Ω",
}

_SYMBOLS = {
    r"\infty": "∞", r"\pm": "±", r"\mp": "∓", r"\times": "×",
    r"\div": "÷", r"\cdot": "·", r"\leq": "≤", r"\geq": "≥",
    r"\neq": "≠", r"\approx": "≈", r"\sim": "∼", r"\equiv": "≡",
    r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\supset": "⊃",
    r"\cup": "∪", r"\cap": "∩", r"\emptyset": "∅",
    r"\forall": "∀", r"\exists": "∃", r"\nabla": "∇",
    r"\partial": "∂", r"\int": "∫", r"\sum": "∑", r"\prod": "∏",
    r"\sqrt": "√", r"\ldots": "…", r"\cdots": "⋯",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐", r"\leftrightarrow": "↔",
    r"\to": "→",
}


def latex_to_unicode(latex: str) -> str:
    """Convert a LaTeX math string to a unicode approximation for display."""
    if _LATEX2TEXT_AVAILABLE:
        try:
            return LatexNodes2Text().latex_to_text(latex).strip()
        except Exception:
            pass  # fall through to manual conversion

    result = latex

    # \frac{a}{b} → (a)/(b)
    result = re.sub(
        r"\\frac\{([^{}]+)\}\{([^{}]+)\}",
        r"(\1)/(\2)",
        result,
    )

    # \sqrt{x} → √(x)
    result = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", result)
    result = result.replace(r"\sqrt", "√")

    # ^{n} → superscript-like ^n
    result = re.sub(r"\^\{([^{}]+)\}", r"^(\1)", result)

    # _{n} → subscript-like _n
    result = re.sub(r"_\{([^{}]+)\}", r"_(\1)", result)

    # Named functions — keep as-is (sin, cos, log, etc.)
    for cmd in (r"\sin", r"\cos", r"\tan", r"\arcsin", r"\arccos", r"\arctan",
                r"\log", r"\ln", r"\exp", r"\lim", r"\max", r"\min", r"\det",
                r"\gcd", r"\mod", r"\abs"):
        result = result.replace(cmd, cmd[1:])  # strip backslash

    # Greek + symbols
    for token, char in {**_GREEK, **_SYMBOLS}.items():
        result = result.replace(token, char)

    # Strip remaining LaTeX commands and braces
    result = re.sub(r"\\[a-zA-Z]+", "", result)
    result = result.replace("{", "(").replace("}", ")")
    result = result.strip()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recognize_math(png_bytes: bytes) -> dict:
    """Recognize handwritten math from a PNG image.

    Args:
        png_bytes: Raw PNG bytes of the handwriting canvas.

    Returns:
        {
          "latex":      str,   # raw pix2tex output, e.g. r"\\sin(\\frac{\\pi}{4})"
          "unicode":    str,   # human-readable approximation, e.g. "sin((π)/(4))"
          "confidence": float, # placeholder — pix2tex does not expose confidence
        }
        or {"error": str} on failure.
    """
    if not _PIX2TEX_AVAILABLE:
        return {"error": "pix2tex not installed — run: pip install pix2tex"}

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as exc:
        return {"error": f"Failed to decode image: {exc}"}

    try:
        model = _get_model()
        latex = model(img)
        unicode_text = latex_to_unicode(latex)
        log.info("Recognized LaTeX: %s", latex)
        return {
            "latex": latex,
            "unicode": unicode_text,
            "confidence": 1.0,  # pix2tex does not expose a score
        }
    except Exception as exc:
        log.error("pix2tex inference failed: %s", exc)
        return {"error": str(exc)}
