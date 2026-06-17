---
name: running-the-eval-harness
description: >
  Run, lock, and extend the behavioral eval harness in evals/. Use after changing
  routing, prompts, a manifest, or a skill, when you need to check a baseline, harvest
  gold cases, or add a suite. Covers the model-free gates (router, skill_trigger,
  token budget) and the model-backed ones (command, slots, trajectory, judge) plus the
  baseline-lock regression gate. Do NOT use for the pytest plumbing suite (that mocks
  the model); this is for the model's DECISIONS.
version: 1.0.0
license: MIT
allowed-tools: Read Bash Edit
---

# Running the eval harness

`evals/` is the standing, versioned benchmark for the model's *decisions* (the pytest
suite tests the plumbing around the model; this tests the model itself). Every change
that can move routing/output quality should re-run the relevant gate.

## When to use
- You edited a prompt, the `DomainClassifier`, a manifest, or a skill server.
- You need to lock or re-check a baseline, or harvest gold cases from `agent.db`.
- You're adding a new suite/predictor.

## When NOT to use
- Pure plumbing change with no decision surface → the pytest suite covers it.
- A change the preview/browser can exercise instead → verify there.

## Workflow
1. **Read `evals/README.md`** for the full layout (suites/baselines/modes) and the
   five-step flywheel. The CLI is `python -m evals.run`.
2. **Run the model-free gates first** (instant, no GPU):
   - `python -m evals.run --suite router_domains  --predictor router --check`
   - `python -m evals.run --suite skill_triggers   --predictor skill_trigger --check`
   - `python -m evals.token_budget`  (always-loaded skill-metadata budget)
3. **Run model-backed gates** when the model is up (they fail SAFE — exit 2/skip — when
   Ollama is down, so they never wedge): `command_verbs`, `pain_journal_slots`
   (`--predictor slots`), `dev_trajectory` (`--mode trajectory`), `explain_quality`
   (`--mode judge`). See the exact invocations in `scripts/run_evals.ps1`.
4. **Lock a baseline** only with a deliberate `--update-baseline` (writes
   `evals/baselines/<suite>.json` with a tolerance band). `--check` exits nonzero on
   regression; that is the gate the pre-push hook runs.
5. **Add a suite:** drop `evals/suites/<name>.jsonl` (one JSON case per line; `#`
   comments allowed), then run + `--update-baseline`. For a model-free predictor,
   mirror `router_predictor`/`skill_trigger_predictor` in `evals/runner.py` and wire
   the choice into `evals/run.py`.
6. **Harvest gold cases** from production corrections: add `--db agent.db` to a single
   suite (skipped for the model-free predictors).
7. **One command for everything:** `pwsh -File scripts/run_evals.ps1`
   (`-SkipModel` for the model-free tier only).

## Anti-patterns
- Don't update a baseline to make a real regression "pass" — diagnose the failure.
- Don't over-fit a prompt to a single suite; the suites are co-checked so one gate's
  gain shouldn't silently regress another (that's what the regression gate is for).
- Don't treat an exit-2 (backend down) as a pass *or* a failure — it's a skip.
