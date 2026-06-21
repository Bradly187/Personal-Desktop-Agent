"""Edit-format ACI — deterministic edit application + lint gate for WRITE_FILE.

The DevAgent's ``WRITE_FILE`` verb mutates files on disk. Historically it wrote
the model's ``body`` verbatim with no validation, so a syntactically broken edit
(a local model silently dropping lines while re-typing a whole file) landed on
disk and was only caught downstream when a later step's test failed.

This module is the seam that fixes that. ``EditApplier.apply`` takes the current
file text, the model's payload, and the active ``edit_format``, and returns the
resulting file text — but **lint-gates it first**: if the result fails a
registered validator (e.g. Python ``ast.parse``), it raises an ``EditError``
instead of returning, so the write never happens (fail-closed) and the DevAgent
loop replans with a diagnostic message.

Design contract (matches ``inference/trajectory.py``): pure functions, fully
deterministic, no I/O of its own (the caller reads/writes files), no LLM call.

Spec: ``specs/edit-format-aci/requirements.md`` (R1 lint gate, R2 diagnostic
feedback, R3 per-model format knob). Structured formats (udiff / hashline) are
introduced by R4 / task 4 — this module currently implements ``whole_file`` and
falls back to it for any unknown format (R3.3).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Edit-format identifiers (ModelProfile.edit_format values). Only WHOLE_FILE is
# implemented today; the others are reserved for task 4 and currently fall back.
WHOLE_FILE = "whole_file"
UDIFF = "udiff"          # reserved — no-line-number unified diff (R4)
HASHLINE = "hashline"    # reserved — line:hash anchored edits (R4)

_KNOWN_FORMATS = frozenset({WHOLE_FILE, UDIFF, HASHLINE})


class EditError(Exception):
    """A WRITE_FILE edit that could not be applied or failed validation.

    Carries structured fields so the diagnostic feedback the model sees on
    replan (R2) is actionable, not a bare exception string. ``str(EditError)``
    is the human-diagnostic ``message`` — the DevAgent retry loop renders it as
    the step result.

    Fields:
        reason     — one of "syntax" | "mismatch" | "scope" | "io"
        message    — human-diagnostic text (what failed + what to do next)
        target     — the text/anchor the edit was trying to match (if any)
        suggestion — the most-similar candidate region, when derivable (R2.1)
    """

    def __init__(
        self,
        reason: str,
        message: str,
        target: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        self.reason = reason
        self.target = target
        self.suggestion = suggestion
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Validators — registered per file extension; each raises EditError on failure.
# --------------------------------------------------------------------------- #

def _validate_python(text: str, path: str) -> None:
    """Reject text that is not parseable Python (R1.2, R2.2).

    Uses ``ast.parse`` (compile to AST, no execution) — catches the dominant
    local-model failure mode (truncated functions, dropped ``def``/``class``
    lines, unbalanced brackets) before the file is written.
    """
    try:
        ast.parse(text)
    except SyntaxError as exc:
        where = f" at line {exc.lineno}" if exc.lineno else ""
        detail = exc.msg or "invalid syntax"
        raise EditError(
            reason="syntax",
            message=(
                f"WRITE_FILE rejected: the resulting file is not valid Python — "
                f"SyntaxError: {detail}{where}. The file on disk was NOT modified. "
                f"Re-emit the COMPLETE corrected file content for {path}."
            ),
            target=f"line {exc.lineno}" if exc.lineno else None,
        ) from exc


# Extension → validator. A path whose extension is absent here is not linted
# (pass-through write — byte-identical to the legacy path). R1.3.
_DEFAULT_VALIDATORS: dict[str, Callable[[str, str], None]] = {
    ".py": _validate_python,
}


class EditApplier:
    """Applies a WRITE_FILE payload to produce the resulting file text + lint.

    Stateless across calls; the validator registry is the only configuration.
    Inject a custom ``validators`` map in tests to register/clear validators.
    """

    def __init__(
        self, validators: Optional[dict[str, Callable[[str, str], None]]] = None
    ) -> None:
        # Copy so a caller's mutation can't bleed into the module default.
        self._validators: dict[str, Callable[[str, str], None]] = (
            dict(validators) if validators is not None else dict(_DEFAULT_VALIDATORS)
        )

    def apply(
        self,
        current_text: str,
        payload: str,
        edit_format: str = WHOLE_FILE,
        path: str = "",
    ) -> str:
        """Return the resulting file text for the edit, or raise ``EditError``.

        ``current_text`` is the file's text on disk ("" for a new file).
        ``payload`` is the model's WRITE_FILE body. The result is lint-gated
        before being returned — a validation failure raises and nothing is
        written by the caller (R1.1, R1.2).
        """
        fmt = (edit_format or WHOLE_FILE).lower()
        if fmt not in _KNOWN_FORMATS:
            log.warning(
                "EditApplier: unknown edit_format %r — falling back to %s",
                edit_format, WHOLE_FILE,
            )
            fmt = WHOLE_FILE

        if fmt == WHOLE_FILE:
            new_text = payload
        else:
            # UDIFF / HASHLINE not yet implemented (task 4) — degrade gracefully
            # to whole_file rather than crash (AGENTS.md degrade-gracefully, R3.3).
            log.warning(
                "EditApplier: edit_format %r not yet implemented — using %s",
                fmt, WHOLE_FILE,
            )
            new_text = payload

        self._lint(path, new_text)
        return new_text

    def _lint(self, path: str, text: str) -> None:
        """Run the registered validator for ``path``'s extension, if any (R1.3)."""
        ext = Path(path).suffix.lower()
        validator = self._validators.get(ext)
        if validator is None:
            return
        validator(text, path)  # raises EditError on failure
