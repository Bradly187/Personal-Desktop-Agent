# Spec: Voice-Invokable Rewind (CG-2)

---

## 1. Background — the "Why"

Currently, PDA's DevAgent only unwinds saga snapshots on failure (`_halt_and_compensate`). If a successful run commits changes that the user realizes are conceptually wrong, there is no one-shot undo. This is a crucial accessibility-native safety feature: for a voice-first user on a pain day, saying "undo that run" is vastly cheaper than manually identifying and reverting the changes. This spec promotes existing saga snapshots to named, per-run checkpoints that can be explicitly restored via voice command.

**Status:** Done
**Approved:** Brad, 2026-07-02
**Owner / author session:** Antigravity

---

## 2. Glossary

- **RunCheckpoint**: A named snapshot of the workspace and agent state captured at the beginning of a workflow run, persisting beyond the run's success/failure.
- **VoiceRewindHandler**: The pipeline component that listens for the spoken trigger ("undo that run") and coordinates the restoration of the most recent `RunCheckpoint`.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Checkpoint Promotion

**User Story:** As Brad, I want every DevAgent run to leave a persistent snapshot behind so that I can revert to it later.

#### Acceptance Criteria
1. WHEN a DevAgent run begins, THE `DevAgent` SHALL capture a `RunCheckpoint` using the `DA_SAGA_GIT_BACKEND` storage layer.
2. THE `DevAgent` SHALL persist this checkpoint even if the run succeeds (unlike current saga snapshots which discard on success).

### Requirement 2: Voice-Invokable Reversion

**User Story:** As Brad, I want to say "undo that run" to immediately rollback all files and state to before the agent's last action, saving me keyboard effort.

#### Acceptance Criteria
1. WHEN the `CommandExecutor` (or voice parser) receives the phrase "undo that run" (or exact configurable alias), THE `VoiceRewindHandler` SHALL identify the most recent `RunCheckpoint`.
2. THE `VoiceRewindHandler` SHALL restore the workspace to that checkpoint's state.
3. THE `VoiceRewindHandler` SHALL queue a TTS announcement confirming the rollback: "Rolled back to before the last run."
4. IF no checkpoint exists or restoration fails, THEN THE `VoiceRewindHandler` SHALL fail safely and announce the failure via TTS.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** Voice command parser -> `VoiceRewindHandler` -> Saga/Checkpoint backend.
- **New `Command` fields (if any):** `REVERT_RUN` verb or dedicated parser intent.
- **Models / VRAM:** N/A.
- **Persistence:** Requires `DA_SAGA_GIT_BACKEND` to be enabled as the storage layer (currently OFF). No new DB schema needed unless checkpoint metadata is stored in `agent.db` (if so, requires `storage/db.py` migration).

### Configuration (flat YAML)

```yaml
voice_rewind:
  enabled: false          # Start disabled until soak testing passes
  trigger_phrases:
    - "undo that run"
    - "revert last run"
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** 
  - `tests/test_voice_rewind.py`
  - Assert R1.1/R1.2: Checkpoint is created and persists after run success.
  - Assert R2.2: Reverting successfully restores modified files.
  - Assert R2.4: Fails gracefully when no history exists.
- **Eval suite:** N/A for model evals, purely deterministic systems logic.

---

## 6. Tasks

- [x] 1. Enable `DA_SAGA_GIT_BACKEND` on a feature branch and soak test it.
- [x] 2. Update `DevAgent` run lifecycle to persist checkpoints on success (R1).
- [x] 3. Implement `VoiceRewindHandler` and wire up the `REVERT_RUN` intent from the voice parser (R2).
- [x] 4. Add unit tests for successful rewind and missing-checkpoint edges.
- [x] 5. Update CLAUDE.md to document the new voice capability.
