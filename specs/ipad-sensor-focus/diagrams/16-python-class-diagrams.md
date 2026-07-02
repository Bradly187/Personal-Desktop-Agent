# Python Class Diagrams — by Concern

Eight focused class diagrams covering the PC-side (Python) architecture, decomposed
by layer. Supersedes the monolithic Python section formerly in
[02-class-diagram.md](02-class-diagram.md) (which retains the iPad/Swift side).

Reflects the post-PR-#153 `HybridCoordinator` decomposition. All class names were
verified against the codebase on 2026-07-02 (26 spot-checked, including
`VoicePromptComposer`, `MCPTrustClassifier`, `FlickEngine`, `VLLMSpecialistPool`,
`GyroBiasCalibrator`); the Gate 0–4 note matches `core/gate_evaluator.py`, the
VisionGrounder fallback matches `desktop/vision_grounder.py`, and
`CoordinatorConfig.anthropic_model` matches `core/hybrid_coordinator.py`.

| # | Diagram | Key modules |
|---|---------|-------------|
| 1 | Top-Level Pipeline | `main.py`, `core/ipad_bridge.py`, `core/fusion_engine.py`, `core/supervisor.py`, `core/resource_governor.py` |
| 2 | HybridCoordinator Decomposition | `core/hybrid_coordinator.py`, `core/gate_evaluator.py`, `core/inference_runner.py`, `core/action_executor.py`, `core/workflow_handler.py`, `core/voice_system_control.py` |
| 3 | Inference & Dev-Agent Layer | `inference/model_router.py`, `inference/local_inference.py`, `inference/dev_agent.py`, `core/goal_session.py` |
| 4 | Sensor Layer | `sensors/`, `calibration/`, `core/whisper_stream.py` |
| 5 | Storage Layer | `storage/db.py`, `storage/analytics_db.py`, `storage/memory_manager.py`, `storage/personal_kb.py` |
| 6 | Adaptive / Learning Layer | `adaptive/behavioral_twin_state.py`, `adaptive/continuous_trainer.py`, `adaptive/mcp_trust_classifier.py` |
| 7 | Desktop Automation Layer | `desktop/` (`command_executor`, `ui_automation`, `vision_grounder`, `target_cache`, `flick_engine`) |
| 8 | Monitoring & Observability | `monitoring/` (`metrics`, `trace`, `metric_watcher`, `dashboard`), `storage/session_analyzer.py` |

> Per AGENTS.md #1, `storage/db.py` remains the source of truth for the DB schema;
> these diagrams show class collaboration, not schema.

---

## 1 · Top-Level Pipeline (Entry → Orchestration)

```mermaid
classDiagram
    direction LR

    class IPadBridge {
        -FusionEngine _fusion
        -GestureProcessor _gesture
        -WhisperStream _whisper
        -LiDARReceiver _lidar
        -HybridCoordinator _coordinator
        -AgentDB _agent_db
        +start()
        +stop()
    }

    class FusionEngine {
        -FusionConfig config
        -HybridCoordinator _coordinator
        +tick_loop()
        +enqueue(Command)
    }

    class FusionConfig {
        +int tick_rate_hz
        +float gravity_radius
    }

    class Command {
        +str verb
        +str target
        +str text
        +str trace_id
        +str domain
        +float confidence
        +str source
        +str outcome
    }

    class HybridCoordinator {
        -CoordinatorConfig _cfg
        -GateEvaluator _gates
        -InferenceRunner _inference
        -ActionExecutor _action_executor
        -WorkflowHandler _workflow
        -VoiceSystemControl _voice_control
        -CommandExecutor _executor
        -EventBus _event_bus
        -RateLimiter _rate_limiter
        -Metrics _metrics
        +route(Command) Command
    }

    class CoordinatorConfig {
        +str anthropic_model
        +float local_timeout_s
        +int max_replans
    }

    class AccessibilityScheduler {
        +enqueue(coro, Priority)
        +fan_out(tasks)
        +pause_dev()
        +resume_dev()
    }

    class Supervisor {
        -list~SupervisedSpec~ _specs
        +add(SupervisedSpec)
        +start()
        +stop()
    }

    class ResourceGovernor {
        -MemoryManager _memory
        -ModelRouter _router
        -AccessibilityScheduler _scheduler
        +start()
        +_on_flare_start()
        +_on_flare_end()
    }

    class CircuitBreaker {
        -int _failure_count
        -float _cooldown_s
        -str _state
        +call(coro)
        +reset()
    }

    class EventBus {
        +subscribe(event_type, handler)
        +publish(event_type, payload)
    }

    class RateLimiter {
        +check(key) bool
        +consume(key)
    }

    IPadBridge --> FusionEngine : drives 60Hz loop
    IPadBridge --> HybridCoordinator : dispatches to
    FusionEngine *-- FusionConfig
    FusionEngine --> HybridCoordinator : emits Command
    FusionEngine ..> Command : creates

    HybridCoordinator *-- CoordinatorConfig
    HybridCoordinator --> AccessibilityScheduler : schedules work
    HybridCoordinator --> EventBus : publishes events
    HybridCoordinator --> RateLimiter : rate checks

    ResourceGovernor --> ModelRouter : evicts models
    ResourceGovernor --> AccessibilityScheduler : pause/resume dev
    ResourceGovernor --> EventBus : subscribes to flare

    Supervisor --> AccessibilityScheduler : liveness watch
    Supervisor --> FusionEngine : liveness watch
```

---

## 2 · HybridCoordinator Decomposition

```mermaid
classDiagram
    direction TB

    class HybridCoordinator {
        -GateEvaluator _gates
        -InferenceRunner _inference
        -ActionExecutor _action_executor
        -WorkflowHandler _workflow
        -VoiceSystemControl _voice_control
        -CommandExecutor _executor
        -DevAgent _dev_agent
        -BehavioralTwinState _twin
        -ConversationState _conversation
        +route(Command) Command
    }

    class GateEvaluator {
        -CoordinatorConfig _cfg
        -AuditLog _audit
        +evaluate(Command) GateDecision
        +update_latency_ema(float)
    }

    note for GateEvaluator "Gate 0: Privacy\nGate 1: Confidence\nGate 2: Complexity\nGate 3: VRAM\nGate 4: Latency EMA"

    class InferenceRunner {
        -LocalInference _local
        -_CloudInference _cloud
        -ContinuousTrainer _trainer
        -ContentFilter _content_filter
        -RateLimiter _rate_limiter
        -AgentDB _agent_db
        +run_local(Command) str
        +run_cloud(Command) str
    }

    class ActionExecutor {
        -CommandExecutor _executor
        -VisionGrounder _grounder
        -ConversationState _conversation
        -Metrics _metrics
        -WhisperStream _whisper
        -ClickableTargetCache _target_cache
        +execute(Command) Command
    }

    class WorkflowHandler {
        -WorkflowRunner _workflow_runner
        -DevAgent _dev_agent
        -BehavioralTwinState _twin
        -ConversationMode _conv_mode
        -MacroStore _macro_store
        -AgentDB _agent_db
        +handle(Command) Command
    }

    class VoiceSystemControl {
        -WhisperStream _whisper
        -AgentDB _agent_db
        -BehavioralTwinState _twin
        -AcousticProfiler _profiler
        -VoiceCalibrator _calibrator
        -AuditLog _audit
        +handle_voice_control(str) bool
    }

    class CommandExecutor {
        -AgentDB _agent_db
        -AuditLog _audit_log
        -list _writable_roots
        +execute(Command) Command
        +_resolve_coords(target) tuple
    }

    class ConversationState {
        -list~Turn~ _turns
        +add(Turn)
        +get_hint() str
    }

    class ConversationMode {
        +is_active() bool
        +handle_turn(text) str
    }

    HybridCoordinator *-- GateEvaluator : _gates
    HybridCoordinator *-- InferenceRunner : _inference
    HybridCoordinator *-- ActionExecutor : _action_executor
    HybridCoordinator *-- WorkflowHandler : _workflow
    HybridCoordinator *-- VoiceSystemControl : _voice_control
    HybridCoordinator *-- CommandExecutor : _executor
    HybridCoordinator --> ConversationState : shared state

    WorkflowHandler --> ConversationMode : checks mode
    ActionExecutor --> ConversationState : reads hint
    ActionExecutor --> CommandExecutor : dispatches verbs
```

---

## 3 · Inference & Dev-Agent Layer

```mermaid
classDiagram
    direction TB

    class ModelRouter {
        -DomainClassifier _classifier
        -VLLMSpecialistPool _pool
        -list~ModelProfile~ _profiles
        +route(Command) RouterResult
        +evict_all()
    }

    class ModelProfile {
        +str name
        +float vram_gb
        +list~str~ domains
    }

    class DomainClassifier {
        +classify(text) DomainScore
    }

    class DomainScore {
        +str domain
        +float score
    }

    class LocalInference {
        <<abstract>>
        +infer(prompt, model) str
        +stream(prompt, model) AsyncIterator
        +embed(text) list~float~
    }

    class OllamaInference {
        -CircuitBreaker _breaker
        +infer(prompt, model) str
    }

    class VLLMInference {
        +infer(prompt, model) str
    }

    class VLLMServerInference {
        +infer(prompt, model) str
    }

    class LlamaCppInference {
        +infer(prompt, model) str
    }

    class DevAgent {
        -ModelRouter _router
        -MemoryManager _memory
        -AccessibilityScheduler _scheduler
        -AgentDB _agent_db
        -Critic _critic
        -Tester _tester
        -EditApplier _edit_applier
        +run(Command) AgentResult
        +_run_dag_waves(plan)
    }

    class AgentStep {
        +str verb
        +str args
        +str status
    }

    class AgentResult {
        +bool success
        +str output
        +list~AgentStep~ steps
    }

    class Critic {
        +review(diff) CriticVerdict
    }

    class Tester {
        +run_tests(path) TestOutcome
    }

    class EditApplier {
        +apply(edit) str
    }

    class GoalSession {
        -GoalSessionStore _store
        -AgentDB _agent_db
        +authorize(goal) bool
        +_path_in_scope(path) bool
    }

    class GoalSessionStore {
        +load() dict
        +save(dict)
    }

    class VoicePromptComposer {
        +compose(text, context) str
    }

    LocalInference <|-- OllamaInference
    LocalInference <|-- VLLMInference
    LocalInference <|-- VLLMServerInference
    LocalInference <|-- LlamaCppInference

    ModelRouter --> DomainClassifier : classifies input
    ModelRouter --> ModelProfile : selects from roster
    ModelRouter ..> DomainScore : uses
    DomainClassifier ..> DomainScore : returns

    DevAgent --> ModelRouter : selects specialist
    DevAgent --> Critic : reviews diffs (DA_CRITIC)
    DevAgent --> Tester : auto-pytest (DA_TESTER)
    DevAgent --> EditApplier : applies writes
    DevAgent ..> AgentStep : plans
    DevAgent ..> AgentResult : returns

    GoalSession *-- GoalSessionStore
    DevAgent --> GoalSession : path scoping
    DevAgent --> VoicePromptComposer : prompt assembly
```

---

## 4 · Sensor Layer

```mermaid
classDiagram
    direction LR

    class WhisperStream {
        -_StreamingVAD _vad
        -AcousticProfiler _profiler
        -AgentDB _agent_db
        +start()
        +stop()
        +_handle_approval_gate(text)
    }

    class _StreamingVAD {
        +process(audio_chunk) bool
    }

    class GestureProcessor {
        -OneEuroFilter _filter
        -AgentDB _db
        +process_frame(frame) Gesture
        +update_velocity_floor(gesture, vel)
    }

    class LiDARReceiver {
        +decode_frame(data) DepthMap
        +get_confidence_map() ndarray
    }

    class OneEuroFilter {
        -float _min_cutoff
        -float _beta
        +filter(value, timestamp) float
    }

    class SensorViewer {
        +show_frame(frame)
        +show_depth(depth)
        +show_landmarks(landmarks)
    }

    class HandPointer {
        -HandPointerConfig _config
        -ThumbClick _thumb
        -OneEuroFilter _filter
        +process(landmarks) CursorDelta
    }

    class HandPointerConfig {
        +float smoothing
        +float scale_x
        +float scale_y
    }

    class ThumbClick {
        +detect(landmarks) bool
    }

    class AcousticProfiler {
        -AgentDB _db
        +profile(audio) VoiceMetrics
        +detect_drift() DriftResult
    }

    class VoiceMetrics {
        +float logprob
        +float vad_threshold
        +str condition
    }

    class DriftResult {
        +bool drifted
        +float delta
    }

    class GyroBiasCalibrator {
        -BiasState _state
        +update(sample) float
        +is_calibrated() bool
    }

    class VoiceCalibrator {
        -AgentDB _db
        +run_calibration() CalibrationReport
    }

    WhisperStream *-- _StreamingVAD
    WhisperStream --> AcousticProfiler : calibrates VAD
    WhisperStream ..> VoiceMetrics : uses thresholds

    GestureProcessor --> OneEuroFilter : smooths landmarks
    HandPointer --> OneEuroFilter : smooths cursor
    HandPointer *-- HandPointerConfig
    HandPointer *-- ThumbClick

    AcousticProfiler ..> VoiceMetrics : returns
    AcousticProfiler ..> DriftResult : detects

    VoiceCalibrator --> AcousticProfiler : updates profile
```

---

## 5 · Storage Layer

```mermaid
classDiagram
    direction TB

    class AgentDB {
        +int user_version
        +log_command(Command)
        +log_inference(record)
        +enqueue_goal(goal)
        +claim_goal() dict
        +requeue_stale()
        +mark_interrupted_runs()
        +log_gesture_sample(sample)
        +store_routing_threshold(domain, val)
    }

    class AnalyticsDB {
        +insert_benchmark_run(record)
        +insert_benchmark_result(record)
        +query_percentiles(domain) dict
    }

    class MemoryManager {
        -AgentDB _db
        -SemanticMemory _semantic
        +store(key, value)
        +retrieve(key) str
        +search_similar(query) list
        +log_action(Command)
    }

    class SemanticMemory {
        -chromadb.Collection _collection
        +add(text, metadata)
        +search(query, k) list
        +jaccard_fallback(query) list
    }

    class AuditLog {
        +log_tool_call(record)
        +log_security_event(record)
        +log_session_event(record)
    }

    class SessionAnalyzer {
        -AnalyticsDB _db
        +analyze_session(session_id)
        +route_distribution() dict
        +latency_percentiles() dict
    }

    class PersonalKB {
        -SemanticMemory _mem
        -AgentDB _db
        +index_document(path)
        +search(query) list~DocChunk~
    }

    class DocChunk {
        +str text
        +str source
        +int page
    }

    MemoryManager --> AgentDB : SQLite backend
    MemoryManager --> SemanticMemory : ChromaDB backend
    AuditLog --> AgentDB : append-only writes
    AgentDB --> AnalyticsDB : separate DuckDB instance
    SessionAnalyzer --> AnalyticsDB : queries
    PersonalKB --> SemanticMemory : indexes into
    PersonalKB *-- DocChunk
```

---

## 6 · Adaptive / Learning Layer

```mermaid
classDiagram
    direction TB

    class BehavioralTwinState {
        -AgentDB _db
        -PreferenceModel _prefs
        -TwinSnapshot _snapshot
        +apply_pain_day(signal)
        +update_from_command(Command)
        +get_gesture_floor(gesture) float
        +get_voice_threshold(condition) float
        +save()
        +start()
    }

    class TwinSnapshot {
        +float pain_score
        +dict gesture_floors
        +dict voice_thresholds
        +str condition
    }

    class PreferenceModel {
        +float pain_score
        +dict action_stats
        +update(action, outcome)
        +get_preferred(candidates) str
    }

    class ActionStats {
        +int success_count
        +int failure_count
        +float avg_latency
    }

    class ContinuousTrainer {
        -AgentDB _db
        -BehavioralTwinState _twin
        +on_command_done(Command)
        +adapt_thresholds()
        +update_gesture_floor(gesture, velocity)
        +rank_few_shot(examples) list
    }

    class ContentFilter {
        -list~_Pattern~ _patterns
        +filter(text) Finding
        +scrub(text) str
    }

    class Finding {
        +str category
        +str matched_text
        +float severity
    }

    class MCPTrustClassifier {
        -list~_ThreatPattern~ _patterns
        +classify(tool_output) TrustVerdict
    }

    class TrustVerdict {
        +RiskLevel level
        +str reason
        +bool block
    }

    class MacroStore {
        -AgentDB _db
        +save_macro(Macro)
        +get_macro(name) Macro
        +list_macros() list~Macro~
        +delete_macro(name)
    }

    class Macro {
        +str name
        +str trigger_phrase
        +list~Command~ steps
    }

    BehavioralTwinState *-- TwinSnapshot
    BehavioralTwinState *-- PreferenceModel
    PreferenceModel *-- ActionStats
    BehavioralTwinState --> AgentDB : persists snapshots

    ContinuousTrainer --> BehavioralTwinState : updates twin state
    ContinuousTrainer --> AgentDB : drains gesture samples

    ContentFilter ..> Finding : returns
    MCPTrustClassifier ..> TrustVerdict : returns

    MacroStore *-- Macro
    MacroStore --> AgentDB : persists macros
```

---

## 7 · Desktop Automation Layer

```mermaid
classDiagram
    direction TB

    class CommandExecutor {
        -AgentDB _agent_db
        -AuditLog _audit_log
        -list _writable_roots
        +execute(Command) Command
        +_resolve_coords(target) tuple
    }

    note for CommandExecutor "Coord resolution chain:\n1. UIAutomation BFS\n2. VisionGrounder\n3. OCR (pytesseract)\n4. Cursor + CLARIFY"

    class UIAutomationProvider {
        +find_element(name) UIElement
        +bfs_tree() list~UIElement~
        +fuzzy_match(name) UIElement
    }

    class UIElement {
        +str name
        +str control_type
        +tuple rect
        +bool is_enabled
    }

    class VisionGrounder {
        +ground(target, screenshot) GroundingResult
    }

    note for VisionGrounder "Primary: qwen3-vl:30b\nFallback: claude-sonnet-4-6"

    class GroundingResult {
        +tuple coords
        +float confidence
        +str source
    }

    class ClickableTargetCache {
        -UIAutomationProvider _uia
        +get_targets() list~Target~
        +update()
        +nearest(point) Target
    }

    class Target {
        +str name
        +tuple rect
        +str control_type
    }

    class ActionVerifier {
        +verify(before, after) VerifyResult
    }

    note for ActionVerifier "Pillow perceptual diff\n2% pixel threshold\n400ms delay"

    class VerifyResult {
        +bool changed
        +float diff_pct
    }

    class FlickEngine {
        -FlickState _state
        +process_frame(frame) FlickResult
    }

    class FlickResult {
        +str snap_direction
        +bool triggered
    }

    class SnapZone {
        +str direction
        +tuple rect
        +bool contains(point) bool
    }

    CommandExecutor --> UIAutomationProvider : coord resolution (1st)
    CommandExecutor --> VisionGrounder : coord resolution (2nd)
    CommandExecutor --> AuditLog : logs all executions

    UIAutomationProvider ..> UIElement : returns
    VisionGrounder ..> GroundingResult : returns
    ActionVerifier ..> VerifyResult : returns

    ClickableTargetCache --> UIAutomationProvider : polls BFS tree
    ClickableTargetCache *-- Target

    FlickEngine ..> FlickResult : returns
    FlickEngine --> SnapZone : checks zones
```

---

## 8 · Monitoring & Observability Layer

```mermaid
classDiagram
    direction LR

    class Metrics {
        -Histogram _latency
        -RollingWindow _throughput
        -dict _counters
        +record_latency(domain, ms)
        +increment(counter)
        +snapshot() dict
        +serve_endpoint()
    }

    class Histogram {
        -list _buckets
        +observe(value)
        +percentile(p) float
    }

    class RollingWindow {
        -deque _samples
        -float _window_s
        +add(value)
        +mean() float
        +p95() float
    }

    class TraceRecorder {
        -AgentDB _db
        +start_span(trace_id, name)
        +end_span(trace_id, name)
        +get_trace(trace_id) list
    }

    note for TraceRecorder "Opt-in via DA_TRACE=1\nQueryable: GET /trace/{id}"

    class MetricWatcher {
        -Metrics _metrics
        -EventBus _event_bus
        -list~_ThresholdState~ _thresholds
        +watch()
        +add_threshold(metric, value)
    }

    class Dashboard {
        -SnapshotFetcher _fetcher
        -CursesRenderer _renderer
        +run()
    }

    class SnapshotFetcher {
        -Metrics _metrics
        -AgentDB _db
        +fetch() DashboardSnapshot
    }

    class CursesRenderer {
        +render(DashboardSnapshot)
    }

    class SessionAnalyzer {
        -AnalyticsDB _db
        +analyze_session(id)
        +route_distribution() dict
        +latency_percentiles() dict
    }

    Metrics *-- Histogram
    Metrics *-- RollingWindow

    Dashboard *-- SnapshotFetcher
    Dashboard *-- CursesRenderer
    SnapshotFetcher --> Metrics : reads snapshot

    MetricWatcher --> Metrics : observes
    MetricWatcher --> EventBus : fires threshold alerts

    TraceRecorder --> AgentDB : persists spans
    SessionAnalyzer --> AnalyticsDB : queries DuckDB
```
