# Continuous Learning Roadmap (2-week PoC + next steps)

Goal: Close the highest-impact gaps quickly: monitoring & drift detection, experiment tracking, and retrain orchestration.

Week 1 (PoC focus)

- Task A: Monitoring & Drift Metrics (2 days)
  - Integrate metric exporters: add Prometheus client to log Gate-1 metrics (cloud_rate, failure_rate), adaptation events, and gesture calibration stats.
  - Add a lightweight Grafana dashboard (or W&B charts) to visualize metrics. 
  - Success criteria: metrics emitted and visible; alerts for cloud_rate > 30% configured (test with synthetic data).
  - Files to modify: continuous_trainer.py (emit metrics), hybrid_coordinator.py (routing stats), tests/ (add dry-run test).

- Task B: Experiment Tracking (3 days)
  - Wire Weights & Biases (or MLflow) to log adaptation passes, few-shot insertions, promoted hotwords, and gate threshold changes.
  - Record run_id for each adaptation pass and link to AgentDB snapshots.
  - Success criteria: W&B runs show adaptation metrics, artifacts, and logs for at least 5 adaptation cycles.
  - Files to modify: continuous_trainer.py (_adapt), db.py (optional helper to export snapshots), CI: add secrets for W&B if needed.

- Task C: Retrain Orchestration PoC (3 days)
  - Create a GitHub Actions job that exports recent few-shot examples from AgentDB into a reproducible container environment, runs a training/fine-tune script, and stores artifacts and metrics.
  - Keep it destructive-safe: run in "dry-run" by default; require manual trigger to publish artifacts.
  - Success criteria: an artifact (trained model or small adapter) is produced and metrics recorded; test harness validates expected improvement on a holdout.
  - Files to add/modify: .github/workflows/retrain-poc.yml, tools/retrain/export_examples.py, tools/retrain/train_container/Dockerfile, tests/test_retrain_poc.py

Week 2 (stabilize + integrate)

- Task D: Dashboard + Alerting (2 days)
  - Wire alerts (email/Slack) on Prometheus / W&B thresholds (cloud_rate > 30%, failure_rate increase). Document alert runbook.

- Task E: Safe rollout plan (2 days)
  - Implement canary evaluation in retrain workflow: evaluate candidate on a small subset and support rollback if metrics drop.
  - Add adaptation_log auditing to ensure all changes reversible.

- Task F: Documentation & Tests (2 days)
  - Add docs (.github/continuous_learning_roadmap.md created), unit/integration tests for adaptation logic (dry-run), and a README for the PoC steps.

Owners & assumptions
- Owner: engineering lead (or author). Support: one engineer for infra (GitHub Actions, Prometheus) and one ML engineer for training scripts.
- Assumes access to AgentDB (local or test instance) and permissions to add secrets (W&B API key) to repo.

Deliverables
- Prometheus metrics + Grafana dashboard or W&B dashboard for adaptation metrics.
- W&B experiment logs for adaptation passes.
- GitHub Actions retrain PoC that produces an artifact and test metrics.
- Runbook describing monitoring thresholds and rollback steps.

Next steps after PoC
- Integrate Feast for any structured features used by adaptation logic.
- Add model registry & deployment flow (Verta/Seldon/Triton) if retrain artifacts are to be served.
- Consider incremental learning (River) for small real-time components if latency requires.

