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
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Edit-format identifiers (ModelProfile.edit_format values).
WHOLE_FILE = "whole_file"
UDIFF = "udiff"          # reserved — no-line-number unified diff (R4, not yet impl)
HASHLINE = "hashline"    # line:hash anchored edits (R4, implemented)

_KNOWN_FORMATS = frozenset({WHOLE_FILE, UDIFF, HASHLINE})

# How far up/down from a stale anchor's line number to search for a line whose
# hash still matches (the file shifted since the model read it). Conservative —
# an ambiguous match (both directions hit at the same distance) is rejected.
_FUZZY_WINDOW = 5


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


# --------------------------------------------------------------------------- #
# Hashline format (specs/edit-format-aci R4) — line:hash anchored edits.
#
# The model never reproduces surrounding text: each line is shown to it as
# ``<lineno>:<hash>|<content>`` (render_hashline), and it edits by referencing an
# anchor ``<lineno>:<hash>``. The hash is CRC32-mod-256 of the line's *stripped*
# content as 2 hex chars (handoff §7), so it is whitespace-insensitive and doubles
# as a staleness check: if the line at ``lineno`` no longer hashes to ``hash``, the
# file changed since the read and the edit is rejected rather than misapplied.
# --------------------------------------------------------------------------- #

# Prepended to the plan context when the plan model's edit_format is hashline,
# so it overrides _PLAN_PROMPT's whole-file WRITE_FILE convention (R3.2 prompt
# side). Only injected for hashline models — whole_file models never see it, so
# the default plan prompt is byte-identical.
HASHLINE_PROMPT_INSTRUCTIONS = """\
EDIT FORMAT — HASHLINE (overrides the default WRITE_FILE convention):
- READ_FILE returns each line as `<lineno>:<hash>|<content>`. The hash is a short
  fingerprint of the line; use it to anchor edits — never retype surrounding code.
- To change a file, emit a WRITE_FILE step whose body is one or more edit ops.
  Each op is a header line plus (for REPLACE / INSERT) its new content:
    @@ REPLACE <line>:<hash>        replace that one line with the lines below
    @@ DELETE <line>:<hash>         delete that line (no content)
    @@ INSERT_AFTER <line>:<hash>   insert the lines below after that line
    @@ INSERT_BEFORE <line>:<hash>  insert the lines below before that line
- Copy the `<line>:<hash>` anchor EXACTLY from the most recent READ_FILE output.
  If an anchor is stale (the file changed), the edit is rejected — READ_FILE again.
- One op per line; do not target the same line twice in one WRITE_FILE."""


def hash_line(content: str) -> str:
    """2-hex-char CRC32-mod-256 of the line's whitespace-stripped content."""
    return f"{zlib.crc32(content.strip().encode('utf-8')) & 0xFF:02x}"


def render_hashline(text: str) -> str:
    """Render file text as ``lineno:hash|content`` lines for the model to anchor on."""
    lines = text.split("\n")
    return "\n".join(f"{i}:{hash_line(ln)}|{ln}" for i, ln in enumerate(lines, 1))


# An op header: ``@@ REPLACE 12:a3`` / ``@@ DELETE 9:b0`` / ``@@ INSERT_AFTER 4:1f``.
_HASHLINE_OP_RE = re.compile(
    r"^@@\s+(REPLACE|DELETE|INSERT_AFTER|INSERT_BEFORE)\s+(\d+):([0-9a-fA-F]{2})\s*$"
)
_CONSUMING_OPS = frozenset({"REPLACE", "DELETE"})  # ops that remove the anchored line


@dataclass
class _HashOp:
    op: str
    lineno: int          # 1-based line number the model anchored on
    hash: str            # expected 2-hex hash of that line
    content: list[str] = field(default_factory=list)  # for REPLACE / INSERT_*


def _parse_hashline_ops(payload: str) -> list[_HashOp]:
    """Parse ``@@ <OP> <line>:<hash>`` blocks; content runs until the next header.

    Lenient (Aider principle): text before the first header is ignored, and a
    single trailing blank line per content block is dropped (it is the
    conventional separator before the next ``@@``).
    """
    ops: list[_HashOp] = []
    cur: Optional[_HashOp] = None
    for raw in payload.split("\n"):
        m = _HASHLINE_OP_RE.match(raw)
        if m:
            if cur is not None:
                ops.append(cur)
            cur = _HashOp(op=m.group(1).upper(), lineno=int(m.group(2)),
                          hash=m.group(3).lower())
        elif cur is not None:
            cur.content.append(raw)
    if cur is not None:
        ops.append(cur)
    for op in ops:
        while op.content and op.content[-1].strip() == "":
            op.content.pop()
    return ops


def _resolve_anchor(lines: list[str], op: _HashOp, path: str) -> int:
    """Resolve an op's ``lineno:hash`` anchor to a 0-based index, or raise.

    Layered matcher (R4.2): exact line:hash (hash is already whitespace-
    normalized) → fuzzy search ±_FUZZY_WINDOW for a uniquely-matching nearby
    line. An ambiguous fuzzy hit (both directions match at the same distance)
    fails closed as stale.
    """
    n = len(lines)
    i0 = op.lineno - 1  # 0-based

    # Exact.
    if 0 <= i0 < n and hash_line(lines[i0]) == op.hash:
        return i0

    # Fuzzy: widen outward; nearest unambiguous match wins.
    for d in range(1, _FUZZY_WINDOW + 1):
        left, right = i0 - d, i0 + d
        lm = 0 <= left < n and hash_line(lines[left]) == op.hash
        rm = 0 <= right < n and hash_line(lines[right]) == op.hash
        if lm and rm:
            break  # equidistant collision → ambiguous → stale
        if lm:
            return left
        if rm:
            return right

    actual = lines[i0] if 0 <= i0 < n else None
    actual_desc = repr(actual) if actual is not None else "out of range"
    actual_hash = hash_line(actual) if actual is not None else "n/a"
    raise EditError(
        reason="mismatch",
        message=(
            f"WRITE_FILE (hashline) anchor {op.lineno}:{op.hash} is stale — "
            f"line {op.lineno} is now {actual_desc} (hash {actual_hash}). "
            f"The file changed since you read it; nothing was written. "
            f"Re-READ_FILE {path} and resend the edit against the current "
            f"line:hash anchors."
        ),
        target=f"{op.lineno}:{op.hash}",
        suggestion=actual,
    )


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
        elif fmt == HASHLINE:
            new_text = self._apply_hashline(current_text, payload, path)
        else:
            # UDIFF not yet implemented (task 4) — degrade gracefully to
            # whole_file rather than crash (AGENTS.md degrade-gracefully, R3.3).
            log.warning(
                "EditApplier: edit_format %r not yet implemented — using %s",
                fmt, WHOLE_FILE,
            )
            new_text = payload

        self._lint(path, new_text)
        return new_text

    def _apply_hashline(self, current_text: str, payload: str, path: str) -> str:
        """Apply ``@@ <OP> <line>:<hash>`` edit ops to ``current_text`` (R4).

        Atomic (R4.3): every anchor is resolved and overlap-checked BEFORE any
        edit is applied; edits are then applied bottom-up by descending line so
        an earlier edit can't shift a later anchor. Any stale anchor aborts the
        whole batch with a diagnostic (R2.1) — nothing is written.
        """
        ops = _parse_hashline_ops(payload)
        if not ops:
            raise EditError(
                reason="mismatch",
                message=(
                    "WRITE_FILE (hashline) contained no parseable edit ops. Each "
                    "edit must begin with a header line: '@@ REPLACE <line>:<hash>', "
                    "'@@ DELETE <line>:<hash>', '@@ INSERT_AFTER <line>:<hash>', or "
                    "'@@ INSERT_BEFORE <line>:<hash>', with replacement/inserted "
                    "content on the following lines. Resend in that form."
                ),
            )

        lines = current_text.split("\n")

        # Resolve all anchors first (atomic) and reject two edits on one line.
        resolved: list[tuple[int, _HashOp]] = []
        seen: set[int] = set()
        for op in ops:
            idx = _resolve_anchor(lines, op, path)
            if idx in seen:
                raise EditError(
                    reason="overlap",
                    message=(
                        f"WRITE_FILE (hashline) has two edits targeting line "
                        f"{op.lineno}. Send at most one edit per line; nothing was "
                        f"written."
                    ),
                    target=f"{op.lineno}:{op.hash}",
                )
            seen.add(idx)
            resolved.append((idx, op))

        # Apply bottom-up so earlier (higher-index) edits don't shift the indices
        # of not-yet-applied (lower-index) ones.
        for idx, op in sorted(resolved, key=lambda t: t[0], reverse=True):
            if op.op == "REPLACE":
                lines[idx:idx + 1] = op.content
            elif op.op == "DELETE":
                del lines[idx]
            elif op.op == "INSERT_AFTER":
                lines[idx + 1:idx + 1] = op.content
            elif op.op == "INSERT_BEFORE":
                lines[idx:idx] = op.content

        return "\n".join(lines)

    def _lint(self, path: str, text: str) -> None:
        """Run the registered validator for ``path``'s extension, if any (R1.3)."""
        ext = Path(path).suffix.lower()
        validator = self._validators.get(ext)
        if validator is None:
            return
        validator(text, path)  # raises EditError on failure
