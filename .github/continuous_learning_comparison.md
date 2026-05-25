# Continuous Learning Comparison — Personal Desktop Agent vs Market

Feature | Personal Desktop Agent (ContinuousTrainer) | River (online ML) | SageMaker Model Monitor | Feast (Feature Store) | Weights & Biases (W&B) | Verta / Enterprise MLOps
---|---:|---:|---:|---:|---:|---:
Few-shot example storage | Yes | No | No | No | Partial (artifact tracking) | No
Hotword promotion / token promotion | Yes | No | No | No | No | No
Gate-1 threshold autotune (safety rollback) | Yes | No | No | No | No | No
Gesture confidence calibration (p10 rules) | Yes | No | No | No | No | No
Pain-day aware adaptation | Yes | No | No | No | No | No
Intra-session calibration / immediate push | Yes | No | No | No | No | No
Persistence backing (DB) | AgentDB (aiosqlite + DuckDB) | None | Integrated (Cloud) | Integrates with stores | Optional (artifact logs) | Model registry
Drift detection & alerts | No | Limited (depends on usage) | Yes | No | Yes (via integrations) | Yes
Experiment tracking & trials | Partial (adaptation logs) | No | Partial | No | Yes | Yes
Retrain orchestration / pipeline | No | No | Partial (via SageMaker pipelines) | No | Partial (with scripts) | Yes
Feature store / consistent online features | No | No | No | Yes | No | Yes
Model versioning & deployment | No | No | No | No | Partial (artifacts) | Yes
Human-in-the-loop labeling / correction queue | Partial (corrections stored) | No | Partial (workflows) | No | Yes | Yes
Online-weight updates / incremental learning | No | Yes (online algos) | No | No | No | No
Autoscaling serving / production deploy | No | No | Yes | No | Partial (via integrations) | Yes
Federated / privacy-preserving training | No | No | No | No | No | Partial
Observability dashboards (metric export) | Partial (logs exist) | No | Yes | No | Yes | Yes
Canary / A/B rollouts & rollback | No | No | Yes | No | Partial | Yes
Audit & append-only adaptation logs | Yes (audit.db) | No | Partial | No | Yes | Yes

Notes: The repository focuses on runtime adaptation (thresholds, few-shot, gesture calibration) and safety (rollback, pain-day). Missing production MLOps features include drift monitoring, retraining orchestration, feature store integration, and experiment tracking. Recommended first steps: integrate W&B or Prometheus + Grafana for monitoring, add lightweight retrain pipeline (GitHub Actions), and centralize features where useful (Feast adapter).
