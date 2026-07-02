# Spec: Post-Run Walkthrough Artifact + TTS Summary (CG-5)

## 1. Background — the "Why"
Currently, when `DevAgent` successfully completes a plan, it speaks a very brief, hardcoded message like "Done." or reads the first 80 characters of the final response. This gives the user very little visibility into what was actually changed across the codebase during the run. To improve observability without requiring the user to dig through database logs, `DevAgent` should generate a readable markdown "walkthrough" artifact summarizing the run, and distill that into a natural, spoken TTS summary.

**Status:** In Progress
**Approved:** Brad, 2026-07-02
**Owner / author session:** Antigravity

---

## 2. Requirements (EARS acceptance criteria)

### Requirement 1: Artifact Generation
**User Story:** As Brad, I want a readable summary of what the agent just did so I can quickly verify its work.
1. THE `DevAgent` SHALL, upon successful plan completion, query the local plan model (via `ModelRouter`) to generate a markdown walkthrough summarizing the run.
2. THE prompt SHALL include the original goal and a high-level summary of the executed trajectory (actions taken, files modified).
3. THE system SHALL write the resulting markdown to a `walkthrough.md` file in the agent's current working directory (workspace root).

### Requirement 2: TTS Summary
**User Story:** As Brad, I want to hear a concise verbal summary of the completed work rather than just "Done."
1. THE model prompt SHALL also request a 1-sentence plain-text spoken summary of the work.
2. THE `DevAgent` SHALL parse this spoken summary and pass it to the TTS engine via `_speak_plan_completion`.
3. IF the generation fails or times out, THE system SHALL fail-safe to the legacy completion message ("Plan complete.").

### Requirement 3: Safety & Configuration
1. THE feature SHALL be gated by a new configuration flag `DA_POST_RUN_WALKTHROUGH`, defaulting to `0` (OFF) until tested.
2. THE artifact generation SHALL occur asynchronously or have a strict timeout (e.g. 15 seconds) so it does not block the agent's reset cycle indefinitely.

---

## 3. Proposed Changes
- Create this spec file as the source of truth.
- Register `DA_POST_RUN_WALKTHROUGH` (bool, default 0).
- Update `_speak_plan_completion` to generate the walkthrough, parse the spoken tag, and write to `walkthrough.md`.
- Ensure it fails-safe to the original `msg` on any exceptions or missing tags.
- Add unit tests for the walkthrough extraction logic and fail-safes.
