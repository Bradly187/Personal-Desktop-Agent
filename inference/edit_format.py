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
import difflib
import logging
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Edit-format identifiers (ModelProfile.edit_format values).
WHOLE_FILE = "whole_file"
UDIFF = "udiff"          # no-line-number unified diff (R4, implemented)
HASHLINE = "hashline"    # line:hash anchored edits (R4, implemented)
SEARCH_REPLACE = "search_replace"  # aider-style SEARCH/REPLACE blocks (EDIT_FILE verb)

_KNOWN_FORMATS = frozenset({WHOLE_FILE, UDIFF, HASHLINE, SEARCH_REPLACE})

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
- READ_FILE returns each line as `<lineno>:<hash>|<content>`. That `|<content>`
  is DISPLAY ONLY so you can read the file — it is NOT the edit syntax.
- To change a file, emit a WRITE_FILE step whose body is one or more edit ops.
  Each op is a HEADER LINE BY ITSELF, then (for REPLACE / INSERT) the new content
  on the FOLLOWING lines. The header ends right after `<line>:<hash>` — never put
  code or a `|` after the hash on the header line:
    @@ REPLACE <line>:<hash>        replace that one line with the lines below
    @@ DELETE <line>:<hash>         delete that line (no content lines)
    @@ INSERT_AFTER <line>:<hash>   insert the lines below after that line
    @@ INSERT_BEFORE <line>:<hash>  insert the lines below before that line
- Copy the `<line>:<hash>` anchor EXACTLY from the most recent READ_FILE output,
  taking the hash that belongs to the line you actually want to change.
  If an anchor is stale (the file changed), the edit is rejected — READ_FILE again.
- One op per anchored line; do not target the same line twice in one WRITE_FILE.

Worked example — READ_FILE showed:
    14:7c|    # BUG: off by one
    15:a9|    return n - 1
To fix line 15, the WRITE_FILE body is EXACTLY:
    @@ REPLACE 15:a9
        return n
(The header is its own line; the replacement code is on the next line. Do NOT
write `@@ REPLACE 15:a9|    return n`.)"""


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


# --------------------------------------------------------------------------- #
# Search/Replace format (EDIT_FILE verb) — aider-style anchored blocks.
#
# Unlike WRITE_FILE (which re-emits the WHOLE file), EDIT_FILE makes a *surgical*
# change: the model emits one or more blocks, each pairing an exact slice of the
# current file (SEARCH) with its replacement (REPLACE). A SEARCH that does not
# match the current text EXACTLY ONCE fails closed (R5) — the file is never
# touched on an ambiguous or stale edit, mirroring the hashline contract. This is
# the format Claude Code's Edit primitive uses; it avoids the dominant whole-file
# failure mode (a local model dropping unrelated lines while retyping the file).
# --------------------------------------------------------------------------- #

# Block markers. Lenient on marker length (5–9 chars) and trailing whitespace so
# a model that emits 6 or 8 angle-brackets still parses (Aider leniency principle).
_SR_SEARCH_RE = re.compile(r"^<{5,9}\s*SEARCH\s*$")
_SR_DIVIDER_RE = re.compile(r"^={5,9}\s*$")
_SR_REPLACE_RE = re.compile(r"^>{5,9}\s*REPLACE\s*$")

# Prepended to the plan context when EDIT_FILE is available, documenting the
# block syntax. Kept separate from _PLAN_PROMPT so the WRITE_FILE convention is
# untouched for models/paths that don't use EDIT_FILE.
SEARCH_REPLACE_PROMPT_INSTRUCTIONS = """\
EDIT_FILE — surgical edit of an EXISTING file (prefer over WRITE_FILE for changes):
- The body is one or more SEARCH/REPLACE blocks. Each block is EXACTLY:
    <<<<<<< SEARCH
    <lines copied verbatim from the current file>
    =======
    <the replacement lines>
    >>>>>>> REPLACE
- The SEARCH text must match the current file content EXACTLY (whitespace and
  all) and be UNIQUE. Copy it from the most recent READ_FILE output. If it does
  not match exactly once, the edit is rejected and nothing is written — READ_FILE
  again and resend.
- Include enough surrounding context in SEARCH to make the match unique. To
  delete code, leave the REPLACE section empty. Use one block per distinct edit.
- Use WRITE_FILE (whole file) only to CREATE a new file or fully rewrite one."""


@dataclass
class _SRBlock:
    search: str   # exact text to find in the current file ("" allowed for new file)
    replace: str  # text to substitute


def _parse_search_replace_blocks(payload: str) -> list[_SRBlock]:
    """Parse ``<<<<<<< SEARCH / ======= / >>>>>>> REPLACE`` blocks.

    Deterministic state machine. Text outside well-formed blocks is ignored
    (Aider leniency). A malformed block (missing divider/closer) yields no block
    for that region; an empty result is handled by the caller as a hard error so
    a garbled payload never silently no-ops.
    """
    blocks: list[_SRBlock] = []
    state = "outside"          # outside → search → replace
    search: list[str] = []
    replace: list[str] = []
    for raw in payload.split("\n"):
        if state == "outside":
            if _SR_SEARCH_RE.match(raw):
                state, search, replace = "search", [], []
        elif state == "search":
            if _SR_DIVIDER_RE.match(raw):
                state = "replace"
            else:
                search.append(raw)
        elif state == "replace":
            if _SR_REPLACE_RE.match(raw):
                blocks.append(_SRBlock("\n".join(search), "\n".join(replace)))
                state = "outside"
            else:
                replace.append(raw)
    return blocks


# --------------------------------------------------------------------------- #
# Udiff format (specs/edit-format-aci R4) — no-line-number unified diff.
#
# The model emits a unified diff whose ``@@`` hunk-header line numbers (if any)
# are IGNORED — each hunk is interpreted as search/replace: its context (` `) +
# removed (`-`) lines form the "before" block to locate in the file, and its
# context + added (`+`) lines form the replacement. Matching is layered
# (exact → whitespace-normalized → fuzzy, R4.2); a hunk that can't be located
# uniquely fails closed (EditError) — nothing is written. Hunks resolve against
# the original file (atomic), reject overlaps, and apply bottom-up (R4.3).
# --------------------------------------------------------------------------- #

# Fuzzy floor for locating a hunk's "before" block when exact and
# whitespace-normalized both miss. Below this (or without a clear single best)
# the hunk fails rather than patch a wrong region (R4.2).
_UDIFF_FUZZY_THRESHOLD = 0.85

_UDIFF_HUNK_HDR = re.compile(r"^@@.*@@")

# Prepended to the plan context when the plan model's edit_format is udiff, so it
# overrides _PLAN_PROMPT's whole-file WRITE_FILE convention (R3.2 prompt side).
# Only injected for udiff models — whole_file/hashline models never see it.
UDIFF_PROMPT_INSTRUCTIONS = """\
EDIT FORMAT — UDIFF (overrides the default WRITE_FILE convention):
- To change a file, emit a WRITE_FILE step whose body is a unified diff. Do NOT
  re-emit the whole file.
- Each change is a hunk beginning with a `@@ ... @@` header line (any line
  numbers in it are ignored). Inside a hunk, prefix each line with one char:
    ` ` (space) for an UNCHANGED context line (copied verbatim from the file),
    `-` for a line to REMOVE, `+` for a line to ADD.
- Include a few unchanged context lines around each change so the hunk locates a
  UNIQUE spot. If the context/removed lines don't match the current file, the
  edit is rejected and nothing is written — READ_FILE again and resend.

Worked example — to fix an off-by-one on one line:
    @@ @@
     def dec(n):
         # BUG: off by one
    -    return n - 1
    +    return n
(The space-prefixed lines are unchanged context; only the `-`/`+` line changes.)"""


@dataclass
class _UdiffHunk:
    before: list[str]   # context + removed lines, in order (the "find" block)
    after: list[str]    # context + added lines, in order (the replacement)


def _parse_udiff_hunks(payload: str) -> list["_UdiffHunk"]:
    """Parse no-line-number unified-diff hunks into (before, after) blocks.

    Deterministic, lenient (aider style): ``@@`` headers delimit hunks but their
    line numbers are ignored; ``---``/``+++`` file headers are skipped; an
    unprefixed line inside a hunk is treated as context. A payload with diff
    lines but no ``@@`` header is still parsed as a single hunk.
    """
    hunks: list[_UdiffHunk] = []
    before: list[str] = []
    after: list[str] = []
    in_hunk = False

    def flush() -> None:
        nonlocal before, after
        if before or after:
            hunks.append(_UdiffHunk(before, after))
        before, after = [], []

    # splitlines() (not split("\n")) so the payload's trailing newline does not
    # leave a phantom "" that would be absorbed as a blank context line; interior
    # blank lines are preserved.
    for raw in payload.splitlines():
        if _UDIFF_HUNK_HDR.match(raw):
            flush()
            in_hunk = True
            continue
        if raw.startswith("--- ") or raw.startswith("+++ "):
            continue  # file header — must be checked before the '-'/'+' cases
        prefix = raw[:1]
        if prefix == "+":
            after.append(raw[1:])
            in_hunk = True
        elif prefix == "-":
            before.append(raw[1:])
            in_hunk = True
        elif prefix == " ":
            before.append(raw[1:])
            after.append(raw[1:])
            in_hunk = True
        elif in_hunk:
            # Unprefixed / blank line inside a hunk → context on both sides.
            before.append(raw)
            after.append(raw)
        # else: preamble noise before any hunk → ignored
    flush()
    return hunks


def _find_unique_block(
    hay: list[str], needle: list[str], path: str, n: int
) -> tuple[int, int]:
    """Locate ``needle`` as a contiguous run of lines in ``hay`` (R4.2 layered:
    exact → whitespace-normalized → fuzzy). Return (start, end) for exactly one
    match; raise ``EditError`` on zero, multiple, or a below-threshold fuzzy."""
    L = len(needle)
    H = len(hay)
    if L > H:
        raise EditError(
            reason="mismatch",
            message=(f"WRITE_FILE (udiff) hunk {n}: the context/removed block is "
                     f"longer than {path}. Nothing was written — re-READ_FILE and "
                     f"resend."),
            target="\n".join(needle),
        )

    def _hits(pred) -> list[int]:
        return [i for i in range(H - L + 1) if pred(hay[i:i + L])]

    # 1. exact
    hits = _hits(lambda w: w == needle)
    # 2. whitespace-normalized
    if not hits:
        nn = [s.strip() for s in needle]
        hits = _hits(lambda w: [s.strip() for s in w] == nn)
    if len(hits) == 1:
        return hits[0], hits[0] + L
    if len(hits) > 1:
        raise EditError(
            reason="mismatch",
            message=(f"WRITE_FILE (udiff) hunk {n}: its context matches "
                     f"{len(hits)} places in {path} — add more surrounding context "
                     f"so it identifies exactly one region. Nothing was written."),
            target="\n".join(needle),
        )

    # 3. fuzzy — require a clear single best at/above threshold (R4.2)
    target = "\n".join(needle)
    best_ratio, best_i, second = 0.0, -1, 0.0
    for i in range(H - L + 1):
        r = difflib.SequenceMatcher(None, "\n".join(hay[i:i + L]), target).ratio()
        if r > best_ratio:
            second, best_ratio, best_i = best_ratio, r, i
        elif r > second:
            second = r
    if best_ratio >= _UDIFF_FUZZY_THRESHOLD and (best_ratio - second) >= 0.05:
        return best_i, best_i + L
    raise EditError(
        reason="mismatch",
        message=(f"WRITE_FILE (udiff) hunk {n}: could not locate its "
                 f"context/removed lines in {path} (best fuzzy match "
                 f"{best_ratio:.0%} < {_UDIFF_FUZZY_THRESHOLD:.0%}). Re-READ_FILE "
                 f"and resend the hunk against the current content."),
        target=target,
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
        elif fmt == SEARCH_REPLACE:
            new_text = self._apply_search_replace(current_text, payload, path)
        elif fmt == UDIFF:
            new_text = self._apply_udiff(current_text, payload, path)
        else:  # pragma: no cover — _KNOWN_FORMATS guard above is exhaustive
            log.warning(
                "EditApplier: edit_format %r has no applier — using %s",
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

    def _apply_search_replace(
        self, current_text: str, payload: str, path: str
    ) -> str:
        """Apply aider-style SEARCH/REPLACE blocks to ``current_text``.

        Fail-closed contract (R5): each SEARCH must match the *running* text
        EXACTLY ONCE. Zero matches (stale) or multiple matches (ambiguous) abort
        the whole batch with a diagnostic — nothing is written. Blocks apply in
        order against the running text so a later block sees earlier edits, which
        also makes "edit two identical-looking regions" expressible by ordering.
        An empty SEARCH is only valid against an empty file (file creation).
        """
        blocks = _parse_search_replace_blocks(payload)
        if not blocks:
            raise EditError(
                reason="mismatch",
                message=(
                    "EDIT_FILE contained no parseable SEARCH/REPLACE blocks. Each "
                    "edit must be:\n<<<<<<< SEARCH\n<current lines>\n=======\n"
                    "<new lines>\n>>>>>>> REPLACE\nNothing was written — resend in "
                    "that form."
                ),
            )

        text = current_text
        for n, block in enumerate(blocks, 1):
            if block.search == "":
                # Empty SEARCH = create/seed an empty file only. Refuse to blindly
                # prepend into a non-empty file (that is a whole-file WRITE_FILE).
                if text != "":
                    raise EditError(
                        reason="mismatch",
                        message=(
                            f"EDIT_FILE block {n} has an empty SEARCH but {path} is "
                            f"not empty. Use WRITE_FILE to rewrite a whole file, or "
                            f"give a non-empty SEARCH anchor. Nothing was written."
                        ),
                    )
                text = block.replace
                continue

            count = text.count(block.search)
            if count == 0:
                raise EditError(
                    reason="mismatch",
                    message=(
                        f"EDIT_FILE block {n}: the SEARCH text was not found in "
                        f"{path}. It must match the current file content exactly. "
                        f"The file was NOT modified. Re-READ_FILE {path} and resend "
                        f"the edit against the current content."
                    ),
                    target=block.search,
                )
            if count > 1:
                raise EditError(
                    reason="mismatch",
                    message=(
                        f"EDIT_FILE block {n}: the SEARCH text matches {count} "
                        f"locations in {path} — it must be unique. Add surrounding "
                        f"context lines to the SEARCH so it identifies exactly one "
                        f"region. Nothing was written."
                    ),
                    target=block.search,
                )
            text = text.replace(block.search, block.replace, 1)

        return text

    def _apply_udiff(self, current_text: str, payload: str, path: str) -> str:
        """Apply a no-line-number unified diff to ``current_text`` (R4).

        Each hunk's context+removed lines are located in the file (layered match,
        R4.2) and replaced by its context+added lines. Atomic (R4.3): every hunk
        is resolved against the ORIGINAL file first and overlap-checked before any
        edit applies; edits then apply bottom-up so an earlier edit can't shift a
        later hunk's position. Any unlocatable hunk aborts the batch (R2.1) —
        nothing is written.
        """
        hunks = _parse_udiff_hunks(payload)
        # Drop no-op hunks (only context, no +/- change) — e.g. a whitespace-only
        # payload — so a body with no actual edit fails rather than silently no-ops.
        hunks = [h for h in hunks if h.before != h.after]
        if not hunks:
            raise EditError(
                reason="mismatch",
                message=(
                    "WRITE_FILE (udiff) contained no parseable hunks. The body must "
                    "be a unified diff: a `@@ @@` header then lines prefixed ' ' "
                    "(context), '-' (remove), or '+' (add). Nothing was written — "
                    "resend in that form."
                ),
            )

        lines = current_text.split("\n")
        resolved: list[tuple[int, int, list[str]]] = []   # (start, end, after)
        for n, h in enumerate(hunks, 1):
            if not h.before:
                # Pure-insertion hunk (all '+', no anchor). Only valid as the sole
                # hunk creating an empty file — otherwise there is nothing to anchor
                # the insertion to (use a context line, or WRITE_FILE the whole file).
                if current_text == "" and len(hunks) == 1:
                    resolved.append((0, len(lines), h.after))
                    continue
                raise EditError(
                    reason="mismatch",
                    message=(
                        f"WRITE_FILE (udiff) hunk {n} has only added lines and no "
                        f"context to anchor on. Include a few surrounding context "
                        f"lines, or use WRITE_FILE to write {path} whole. Nothing "
                        f"was written."
                    ),
                )
            start, end = _find_unique_block(lines, h.before, path, n)
            resolved.append((start, end, h.after))

        # Reject overlapping target ranges (R4.3).
        for (s1, e1), (s2, e2) in zip(
            sorted((s, e) for s, e, _ in resolved),
            sorted((s, e) for s, e, _ in resolved)[1:],
        ):
            if s2 < e1:
                raise EditError(
                    reason="overlap",
                    message=(
                        "WRITE_FILE (udiff) has two hunks editing overlapping "
                        "regions. Combine them into one hunk; nothing was written."
                    ),
                )

        # Apply bottom-up so earlier edits don't shift later (lower) positions.
        for start, end, after in sorted(resolved, key=lambda t: t[0], reverse=True):
            lines[start:end] = after
        return "\n".join(lines)

    def _lint(self, path: str, text: str) -> None:
        """Run the registered validator for ``path``'s extension, if any (R1.3)."""
        ext = Path(path).suffix.lower()
        validator = self._validators.get(ext)
        if validator is None:
            return
        validator(text, path)  # raises EditError on failure
