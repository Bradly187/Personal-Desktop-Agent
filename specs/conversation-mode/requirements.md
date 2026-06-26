# Spec: Voice Conversation Mode — wake/sleep-gated talk dialogue

---

## 1. Background — the "Why"

Every voice utterance today is treated as a **command**: a Whisper transcript
flows `FusionEngine → HybridCoordinator._route_impl → DomainClassifier` and either
fires an accessibility verb or a single-shot dev query. `core/conversation_state.
ConversationState` adds narrow anaphora ("do that again") and a one-line
last-action hint, but there is no running *dialogue* — each turn is stateless, and
the "general" talk path is single-shot with no memory of prior turns.

For a single user with rheumatoid arthritis, hands-free back-and-forth
conversation (ask a question, hear an answer, follow up — all by voice) is a
natural accessibility affordance the system lacked. This spec adds an explicit,
**voice-driven conversation mode**: the user enters it with a wake phrase and
leaves it with a sleep phrase; in between, every utterance is a conversational
turn answered by the resident local general model and spoken back via TTS.

**Status:** Implemented (v1) on `master` work — flag `conversation_mode.enabled`,
default OFF. Owner / author session: Claude Code (2026-06-25).

---

## 2. Glossary

- **Conversation mode**: a stateful mode entered by a *wake phrase* and left by a
  *sleep phrase*. While active, the command/dev pipeline is bypassed.
- **Wake / sleep phrase**: a deterministic, anchored spoken trigger ("let's talk"
  / "that's all") matched conservatively so ordinary speech never toggles the mode.
- **Turn**: one user utterance + the assistant's spoken reply, retained in a
  rolling history threaded into the model prompt.

---

## 3. Requirements (EARS)

**R1 — Entry.** WHEN conversation mode is enabled AND inactive AND a voice
utterance matches a wake phrase, the system SHALL enter conversation mode, speak a
brief greeting, and NOT route the utterance as a command.

**R2 — Conversational turn.** WHILE conversation mode is active, WHEN a voice
utterance is received that is NOT a sleep phrase, the system SHALL answer it with
the resident **general** model (gemma4:12b via `ModelRouter.infer(domain=
"general")`), threading the running dialogue history into the prompt, append both
the user turn and the reply to history, and speak the reply. The accessibility /
dev command pipeline SHALL be bypassed for that utterance (v1 = pure talk).

**R3 — Exit.** WHILE active, WHEN a voice utterance matches a sleep phrase, the
system SHALL leave conversation mode, discard the dialogue history, and speak a
brief sign-off.

**R4 — Feedback-loop suppression.** WHEN the system speaks any conversation-mode
audio (greeting, reply, sign-off), it SHALL suppress the microphone for the
estimated playback duration plus an echo tail, so the agent never transcribes its
own TTS voice as the user's next turn.

**R5 — Fail-safe.** IF wake/sleep detection is ambiguous OR conversation handling
raises, the system SHALL leave the current mode unchanged and fall through to the
ordinary command pipeline (AGENTS.md #4 — a fault never strands the user).

**R6 — Default OFF.** The feature SHALL be gated by `conversation_mode.enabled` in
`~/.claude/ipad_bridge/config.json`, default `false`; with the flag unset the
voice path is byte-identical to legacy.

**R7 — VRAM hygiene.** Conversation replies SHALL reuse the already-resident
`general` profile via `ModelRouter` (no new model load, no eviction churn —
AGENTS.md #6).

**R8 — Pain-day / 60 Hz safety.** Phrase detection SHALL be a pure deterministic
string operation (no model, no I/O) cheap enough to run on the routing path
without threatening the 60 Hz tick loop (AGENTS.md #2). Model inference and TTS
run off-thread in the async coordinator, never in the FusionEngine tick.

---

## 4. Non-goals (v1)

- **Acting while in conversation** (executing verbs mid-dialogue). v1 is pure
  talk; a "talk + command escape hatch" is a deferred enhancement.
- **Cloud talk model.** v1 is local-only (privacy, no spend, no added latency).
- **Cross-channel session** (sharing state with the `--chat` text UI).
- **iPad protocol change.** v1 surfaces mode only via spoken greeting/sign-off +
  logs; no new WebSocket message type (avoids AGENTS.md #3 Swift-mirror churn).

---

## 5. Config

```json
{
  "conversation_mode": {
    "enabled": true,
    "wake_phrases": ["let's talk", "start a conversation"],
    "sleep_phrases": ["that's all", "goodbye"],
    "directive": "You are having a spoken conversation ...",
    "max_history_turns": 12
  }
}
```

All keys optional except `enabled`; omitted keys use built-in defaults.

---

## 6. Tests / verification

- `tests/test_conversation_mode.py` (40) — wake/sleep positives & negatives
  (incl. the "goodbye in French" false-positive trap), lifecycle, history
  trimming, context rendering, config parsing.
- Manual: enable the flag, say "let's talk", hold a short dialogue, end with
  "that's all" — verify replies are spoken and the mic does not self-trigger.
