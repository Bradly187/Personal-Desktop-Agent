# Behavioral evals

The pytest suite verifies the **plumbing** around the model (it mocks inference —
~93% of test files use `AsyncMock`/`MagicMock`). This package verifies the model's
**decisions**: given an utterance (and optional context), does it produce the right
verb and slots? It is the missing left half of the quality flywheel — a standing,
versioned benchmark that *communicates intent* and *gates change* — seeded from the
production corpus we already collect in `agent.db`.

## Layout

| File | Role |
|------|------|
| `corpus.py` | `EvalCase`, JSONL load/save, `harvest_from_agent_db` (gold cases from `commands.corrected_to`) |
| `scoring.py` | parse an action string, `score_case`, `aggregate` → `Report`, `check_regression` |
| `trajectory.py` | `TrajectoryCase`, `extract_plan_verbs` (mirrors `dev_agent`), `score_trajectory` → `TrajReport` |
| `judge.py` | `JudgeCase`, rubric prompt, `parse_verdict`, `score_judge` → `JudgeReport` (LM-as-judge) |
| `runner.py` | `run_suite` / `run_trajectory_suite` / `run_judge_suite`; real-model predictor factories |
| `run.py` | CLI — run any of the three eval kinds vs the live local model, update/check the baseline |
| `suites/*.jsonl` | versioned labelled cases |
| `baselines/*.json` | locked accuracy baselines (regression gate) |

## Three eval kinds (`--mode`)

The white-paper distinguishes **output** evaluation (is the final artifact right?)
from **trajectory** evaluation (did the agent take the right *sequence* of steps?),
and notes that non-deterministic surfaces need an **LM judge** against a rubric.
This package covers all three; all share the same regression gate (on `exact_acc`).

| Mode | Question | Suite(s) | Gated metric |
|------|----------|----------|-------------|
| `single` (default) | utterance → right verb + slots? | `command_verbs`, `*_slots` | exact accuracy |
| `single --predictor router` | utterance → right domain? (**model-free**) | `router_domains` | exact accuracy |
| `single --predictor skill_trigger` | utterance → right **skill** fires (or none)? (**model-free**) | `skill_triggers` | exact accuracy |
| `trajectory` | goal → right plan (verbs, order, no forbidden actions)? | `dev_trajectory` | exact (fully-correct) rate |
| `judge` | free-form answer → meets the rubric (correct, grounded, no hallucination)? | `explain_quality` | pass rate |

```bash
python -m evals.run --suite router_domains  --predictor router          # no model needed
python -m evals.run --suite skill_triggers   --predictor skill_trigger   # no model needed
python -m evals.run --suite dev_trajectory  --mode trajectory --model qwen3-coder:30b
python -m evals.run --suite explain_quality --mode judge --judge-model gemma3:27b
python -m evals.token_budget                                            # always-loaded metadata budget
```

The four-condition skill-eval coverage from the *Agent Skills* whitepaper:
**trigger** = `skill_triggers` (positive AND negative cases, ≥90%, model-free over the
real `SkillRegistry.match_intent`); **execution** = `command_verbs`/`*_slots`/`dev_*`;
**regression** = the shared baseline-lock gate; **token budget** = `evals.token_budget`
(static check that the always-loaded skill metadata stays small — context-rot defense).

The `router` predictor scores the deterministic `DomainClassifier` (no model, ~0 ms);
the trajectory eval scores the **live production `_PLAN_PROMPT`** (imported from
`inference.model_router`), so tightening that prompt moves the trajectory score — a
true closed loop, not a proxy.

The `trajectory` suite's read-only cases are **safety evals**: a "just explain /
find" goal that emits `WRITE_FILE` / `RUN_TERMINAL` / `GIT_COMMIT` fails even if the
plan parses. The `judge` suite includes a **hallucination probe** (asks about a
country that doesn't exist) — the kind of failure exact-match can't catch.

`corpus`/`scoring`/`runner` are pure stdlib, so the harness logic is unit-tested
without a GPU (`tests/test_evals.py`) and can never wedge CI. Only `run.py` touches
the model, and it fails safe (exit 2, no baseline written) if the backend is down.

## The flywheel, concretely

1. **Evaluate** — `python -m evals.run --suite command_verbs`
2. **Diagnose** — the report clusters failures by verb; `--json` for tooling
3. **Optimize** — edit the system prompt / few-shot, re-run
4. **Verify** — `python -m evals.run --suite command_verbs --check` (nonzero exit on regression)
5. **Monitor → feed back** — `--db agent.db` harvests real corrections as new gold cases

## First-time use (needs the local model up)

```bash
# record the current model's accuracy as the baseline
python -m evals.run --suite command_verbs --update-baseline

# later, gate a prompt/model change against it
python -m evals.run --suite command_verbs --check
```

Baselines ship **unset** (`exact_acc: null`) — record them once on a machine with
Ollama running. The slot suite uses a focused-prompt proxy for the planner's
extraction (`--predictor slots`); see `runner.slot_predictor` for the caveat.

## Gating (CI / pre-push)

`scripts/run_evals.ps1` is the gate that runs **next to pytest**. It runs the
model-free tier always (the harness logic tests + the router gate) and the
model-backed gates only when Ollama is up — `run.py` exits 2 (not 1) on an
unreachable backend, so those are *skipped*, never a false failure. A genuine
baseline regression exits 1 and blocks the push.

```bash
git config core.hooksPath .githooks   # enable the pre-push hook once
pwsh -File scripts/run_evals.ps1            # run all gates by hand
pwsh -File scripts/run_evals.ps1 -SkipModel # model-free tier only
```

The eval-harness logic (incl. the model-free router accuracy bar) also runs in the
normal `pytest tests/` collection (`tests/test_evals*.py`), so eval regressions are
caught even without the hook.
