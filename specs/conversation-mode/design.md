# Design: Voice Conversation Mode

Spec: see `requirements.md`. Status: implemented v1.

---

## Components

### `core/conversation_mode.py` (new) — pure state + detection
`ConversationMode` dataclass (no I/O, synchronous, unit-tested):
- `enabled`, `wake_phrases`, `sleep_phrases`, `directive`, `max_history_turns`,
  `active`, `_history: list[ConversationTurn]`.
- `detect_wake(text)` / `detect_sleep(text)` — normalize the utterance
  (lowercase → strip punctuation → collapse whitespace → drop a small leading
  filler set {hey, ok, okay, agent, computer, please, …} and trailing politeness
  {please, now, then}) and test **set membership** against the phrase set. Anchored
  equality (not substring) is the deliberate fail-safe choice: "how do you say
  **goodbye** in French" must not end the conversation.
- `enter()` / `exit()` — toggle `active`, clear history.
- `record(role, content)` / `history` / `build_context()` — rolling transcript,
  trimmed to `max_history_turns`; `build_context()` returns the directive plus a
  plain-text transcript of *prior* turns (the current turn is passed separately as
  `user_text`).
- `from_config(dict)` / `conversation_mode_config()` — read the
  `conversation_mode` block from `~/.claude/ipad_bridge/config.json`, mirroring
  `macro_store.self_skilling_config()`. Malformed values fall back to defaults.

### `core/hybrid_coordinator.py` — orchestration
- `__init__`: `self._conv_mode = ConversationMode.from_config(conversation_mode_config())`.
- `_route_impl`: a guard block **after** the macro check and **before** the dev
  pre-gate. For `enabled` + `source in {voice, voice_local}`, call
  `_maybe_handle_conversation(cmd)`; a non-`None` result short-circuits routing.
- `_maybe_handle_conversation(cmd)`: state machine —
  - inactive + wake → `enter()`, greet, return `CONVERSATION_START`.
  - inactive + non-wake → `None` (ordinary routing).
  - active + sleep → `exit()`, sign-off, return `CONVERSATION_END`.
  - active + other → `_converse(text)`, record both turns, speak, return
    `CONVERSATION_REPLY`.
  - any exception → `None` (fail-safe, R5).
- `_converse(text)`: `self._dev_agent._router.infer(domain="general",
  user_text=text, context=conv.build_context())`; returns `result.text` or a
  spoken apology. Reuses the resident general model (R7).
- `_speak_and_suppress(text)`: suppress `self._whisper` for `max(1.5, words*0.35)
  + 1.0` s **before** `_tts_speak` (which blocks until playback completes), then
  re-arm a `0.8` s echo tail **after** — guarantees the window covers synthesis +
  playback even if the estimate runs short (R4). Mirrors the voice-calibrator and
  approval-gate guards (`sensors/whisper_stream.WhisperStream.suppress`).

---

## Why these choices

- **Anchored equality detection, not substring/LLM** — predictable, zero-latency,
  fail-safe; consistent with `ConversationState`'s anchored anaphora regexes.
- **Bypass before the dev pre-gate** — once in conversation mode the user's words
  are dialogue, not commands; intercepting early avoids the DomainClassifier
  misrouting them and avoids waking a 30B specialist.
- **Reuse `ModelRouter` general profile** — gemma4:12b co-resides with command +
  Whisper and is never evicted, so conversation adds no VRAM pressure (#6).
- **Pre- + post-speak suppression** — the single highest-risk detail; a feedback
  loop would make the agent talk to itself. The double guard is robust to a short
  word-count estimate.
- **No iPad protocol change** — spoken greeting/sign-off is the primary signal for
  a voice-first user; deferring a `conversation_state` PC→iPad message avoids
  Swift mirror churn (#3) until a UI affordance is actually wanted.

---

## Future (deferred)
- Talk + command escape hatch (a small hard-coded verb set still executes).
- iPad visual mode indicator (new `status`/message-type, mirror in Swift).
- Idle-timeout auto-exit (pain-day-aware threshold via `BehavioralTwinState`).
- Cross-channel session shared with the `--chat` text UI.
