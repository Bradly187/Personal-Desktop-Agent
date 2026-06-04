# Voice Approval-Gate Hardening — 2026-06-04

Branch: `fix/approval-gate-confirmation-token`

## The bug

`approval_hook.py` is a Claude Code `PreToolUse` hook: when a gated tool
(Bash / PowerShell / Agent / computer — per `approval_config.json`) needs
approval it writes `~/.claude/approval/pending`, speaks "Approve <action>?" via
Danielle TTS through the PC speakers, and waits for the iPad mic to answer.

While `pending` existed, `WhisperStream` wrote **whatever it transcribed next**
to `~/.claude/approval/response`, and `approval_hook.py` interpreted that text as
yes/no. So any of the following could silently approve or deny a destructive
tool call:

- ambient audio (a podcast / video / call playing in the room),
- the TTS echo of Danielle's own question (which contains the word "approve"),
- a stray word.

Real 2026-06-04 logs showed `approval response → "share our oil"`,
`"prognostications"`, `"broken record"`, `"Let's put C3 up on the screen"` — all
written as approval answers. On top of that, the timeout path **defaulted to
approve** (`timeout_action: "approve"`, and silence auto-approved), so a
no-response window also granted consent.

## The fix

**1. Explicit confirmation token required.** New `core/approval_keywords.py` is
the single source of truth for the approve/deny vocabulary, with
`classify_confirmation(text) -> "approve" | "deny" | None`. A transcript with no
clear confirmation word returns `None`. `WhisperStream._handle_approval_gate()`
writes the response **only** on `"approve"`/`"deny"`; `None` is discarded and the
gate keeps waiting (no more auto-answering with garbage). Deny wins ties so
ambiguity fails safe toward blocking; utterances longer than `MAX_ANSWER_WORDS`
(6) are treated as ambient, not answers.

**2. Fail safe to deny.** `approval_config.json` `timeout_action` is now
`"reject"`; `approval_hook._parse_response()` delegates to
`classify_confirmation` and defaults ambiguity/silence/timeout to deny; the
`_load_config` fallback and `main()` default also flipped to `"reject"`.
`_request_ipad_approval()` still returns `None` on timeout → caller blocks.

**3. TTS echo suppression.** `WhisperStream._check_approval_echo_guard()` runs at
the top of `_maybe_transcribe()`; when `pending` first appears it calls the
existing `suppress(1.0s)` (which flushes the buffer + drops in-flight audio) so
Danielle's spoken question — captured by the iPad mic during playback — is never
transcribed and mistaken for the user's answer. Fires once per gate.

**4. Ambient rejection (defense in depth).** The `MAX_ANSWER_WORDS` length cap in
`classify_confirmation` rejects long background-media sentences that merely
happen to contain a keyword.

Both halves of the gate now share `core/approval_keywords.py`, so the
"what counts as yes/no" vocabulary can never drift between the transcriber and
the hook.

## Tests

`tests/test_approval_gate.py` — 44 tests: classifier (approve / deny / ambient /
deny-wins-ties / too-long); `_handle_approval_gate` (gate closed no-op, ambient
not written + keep waiting, yes→approve, no→deny, ambient-then-real-answer);
`_check_approval_echo_guard` (suppress on open, once-per-gate, reset on close);
`approval_hook` (shared classifier, ambiguity→deny, iPad timeout→None, config
`timeout_action == "reject"`).

Full suite: **625 passed** (581 baseline + 44 new), run with
`python -m pytest -q --ignore=tests/test_remote_whisper_smoke.py`.

## Files touched

- `core/approval_keywords.py` — **new**; shared confirmation vocabulary + classifier.
- `sensors/whisper_stream.py` — `_handle_approval_gate()`, `_check_approval_echo_guard()`, `_approval_pending_active` state, `_APPROVAL_ECHO_GUARD_S`.
- `approval_hook.py` — import shared classifier, `_parse_response` fail-safe, `timeout_action` default `reject`.
- `approval_config.json` — `timeout_action: "reject"`.
- `tests/test_approval_gate.py` — **new**; 44 tests.
- `CLAUDE.md` — Known Gotchas entry.
