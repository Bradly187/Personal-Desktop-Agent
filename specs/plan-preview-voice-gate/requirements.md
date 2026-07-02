# Spec: Plan-Preview Voice Gate for Large Plans (CG-7)

## 1. Background
Before executing a new plan, `DevAgent` asks for upfront voice approval using a hardcoded, concatenated string of tool verbs (e.g., *"I'll run 6 steps: write_file, run_command, replace_file_content. Approve all?"*). For complex or large plans, this list of verbs provides zero semantic context about what the plan actually *does*, forcing the user to either blindly approve it or manually read the terminal logs. 

To fix this, we need a "plan preview" voice gate that generates a natural language spoken summary of large plans so the user knows exactly what they are approving.

**Status:** In Progress
**Approved:** Brad, 2026-07-02
**Owner / author session:** Antigravity

---

## 2. Requirements (EARS)
1. THE `DevAgent` SHALL intercept plan execution in `_approve_plan_upfront`.
2. IF the number of steps in the plan exceeds the `DA_PLAN_PREVIEW_THRESHOLD` (default: 3) AND `DA_PLAN_PREVIEW` is enabled, THE agent SHALL query the local plan model to generate a 1-sentence spoken preview of the plan's intent.
3. THE prompt SHALL include the user's goal and a summary of the proposed steps (action and arguments).
4. THE TTS prompt SHALL combine the generated preview and the approval question (e.g., *"I plan to refactor the database schema and migrate 3 tables. Approve all?"*).
5. THE preview generation SHALL have a strict timeout (e.g., 5 seconds) so it doesn't hang the interaction loop.
6. IF the generation times out, throws an error, or the plan is below the threshold, THE system SHALL gracefully fail-safe to the legacy verb-list prompt.

---

## 3. Technical Design
- **Flags:** `DA_PLAN_PREVIEW` (bool, default 0) and `DA_PLAN_PREVIEW_THRESHOLD` (int, default 3) in `core/flags.py`.
- **Logic:** In `DevAgent._approve_plan_upfront`, query the model router directly before formatting the TTS `message`.
- **Fallback:** If `res` is empty, times out, or fails, fall back to the original `verb_summary`.
