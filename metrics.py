"""Prometheus metrics wrapper with safe no-op fallback when prometheus_client
is not available. Expose start_metrics_server() to start an HTTP /metrics
endpoint in the background.
"""

try:
    from prometheus_client import Counter, Gauge, start_http_server
    METRICS_AVAILABLE = True
except Exception:
    METRICS_AVAILABLE = False

    class _NoopMetric:
        def labels(self, *a, **k):
            return self

        def inc(self, *a, **k):
            return None

        def set(self, *a, **k):
            return None

    def start_http_server(port: int = 8000):
        # No-op if prometheus_client is not installed
        return None

    Counter = lambda *a, **k: _NoopMetric()
    Gauge = lambda *a, **k: _NoopMetric()


# --- Counters / Gauges ---
adaptation_pass_counter = Counter(
    'continuous_trainer_adaptation_pass_total',
    'Number of adaptation passes performed by ContinuousTrainer'
)

# Gate-1 metrics
gate1_cloud_rate = Gauge(
    'continuous_trainer_gate1_cloud_rate',
    'Cloud escalation rate observed during the last adaptation pass (0-1)'
)

gate1_failure_rate = Gauge(
    'continuous_trainer_gate1_failure_rate',
    'Failure rate (CLARIFY/error fraction) observed during the last adaptation pass (0-1)'
)

gate1_whisper_logprob = Gauge(
    'continuous_trainer_gate1_whisper_logprob',
    'Current whisper_logprob_min threshold used by Gate 1 (logprob)'
)

# HybridCoordinator routing metrics
route_counter = Counter(
    'hybrid_route_total',
    'Count of routed commands by final route (local/cloud/bypass/discard)',
    ['route']
)

route_latency_ms = Gauge(
    'hybrid_route_latency_ms',
    'Latency in ms for routed commands (per-route)',
    ['route']
)

# Gesture calibration
gesture_confidence_floor = Gauge(
    'gesture_confidence_floor',
    'Per-gesture confidence floor applied by ContinuousTrainer',
    ['gesture']
)

gesture_velocity_floor = Gauge(
    'gesture_velocity_floor',
    'Per-gesture velocity floor applied by ContinuousTrainer',
    ['gesture']
)

gesture_velocity_samples_total = Counter(
    'gesture_velocity_samples_total',
    'Number of gesture velocity samples recorded',
    ['gesture']
)


def start_metrics_server(port: int = 8000) -> None:
    try:
        if METRICS_AVAILABLE:
            start_http_server(port)
    except Exception:
        # swallow errors — metrics are optional
        pass
