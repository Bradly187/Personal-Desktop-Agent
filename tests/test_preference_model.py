"""Property-based tests for PreferenceModel.

Feature: behavioral-twin-state
Properties covered: 4, 5, 6, 7
PBT library: Hypothesis (pip install hypothesis)
"""

import datetime
import json
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from behavioral_twin_state import ActionStats, PreferenceModel


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Valid action verbs — uppercase identifiers matching the project vocabulary
action_verb_st = st.text(
    alphabet=st.characters(whitelist_categories=("Lu",)),
    min_size=1,
    max_size=20,
)

# Confidence values: [0.0, 1.0]
confidence_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Latency values: non-negative ms
latency_st = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)

# Source names
source_st = st.sampled_from(["voice", "gesture", "touch", "gaze", "tilt", "head"])

# Unix timestamps covering a full day range (any 24-hour period)
# Use a fixed base date to keep tests deterministic and avoid DST edge cases
_BASE_TS = datetime.datetime(2024, 6, 15, 0, 0, 0).timestamp()  # midnight UTC+local
timestamp_st = st.floats(
    min_value=_BASE_TS,
    max_value=_BASE_TS + 86400.0,  # one full day
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Property 4: PreferenceModel statistics are correct
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    action_verb=action_verb_st,
    observations=st.lists(
        st.tuples(confidence_st, latency_st),
        min_size=1,
        max_size=50,
    ),
    source=source_st,
    ts=timestamp_st,
)
def test_property_4_preference_model_statistics(action_verb, observations, source, ts):
    # Feature: behavioral-twin-state, Property 4: PreferenceModel statistics are correct
    # Validates: Requirements 2.1, 2.2

    model = PreferenceModel()
    k = len(observations)

    for confidence, latency_ms in observations:
        model.update(
            action_verb=action_verb,
            confidence=confidence,
            latency_ms=latency_ms,
            source=source,
            ts=ts,
            success=True,
        )

    stats = model.action_stats[action_verb]

    # frequency == k
    assert stats.frequency == k, (
        f"Expected frequency={k}, got {stats.frequency}"
    )

    # mean_confidence == sum(confidences) / k  (within floating-point tolerance)
    expected_mean_conf = sum(c for c, _ in observations) / k
    assert math.isclose(stats.mean_confidence, expected_mean_conf, rel_tol=1e-9, abs_tol=1e-12), (
        f"mean_confidence mismatch: expected {expected_mean_conf}, got {stats.mean_confidence}"
    )

    # mean_latency_ms == sum(latencies) / k  (within floating-point tolerance)
    expected_mean_lat = sum(l for _, l in observations) / k
    assert math.isclose(stats.mean_latency_ms, expected_mean_lat, rel_tol=1e-9, abs_tol=1e-12), (
        f"mean_latency_ms mismatch: expected {expected_mean_lat}, got {stats.mean_latency_ms}"
    )


# ---------------------------------------------------------------------------
# Property 5: time_bucket is deterministic and exhaustive
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(ts=timestamp_st)
def test_property_5_time_bucket_deterministic(ts):
    # Feature: behavioral-twin-state, Property 5: time_bucket is deterministic and exhaustive
    # Validates: Requirements 2.3
    # Part A — deterministic: same ts always returns same bucket

    bucket1 = PreferenceModel.time_bucket(ts)
    bucket2 = PreferenceModel.time_bucket(ts)

    assert bucket1 == bucket2, (
        f"time_bucket({ts}) returned different values on two calls: {bucket1} vs {bucket2}"
    )


@settings(max_examples=100)
@given(ts=timestamp_st)
def test_property_5_time_bucket_in_valid_range(ts):
    # Feature: behavioral-twin-state, Property 5: time_bucket is deterministic and exhaustive
    # Validates: Requirements 2.3
    # Part B — result is always in {0, 1, 2, 3}

    bucket = PreferenceModel.time_bucket(ts)

    assert bucket in {0, 1, 2, 3}, (
        f"time_bucket({ts}) returned {bucket}, expected one of {{0, 1, 2, 3}}"
    )


def test_property_5_time_bucket_exhaustive():
    # Feature: behavioral-twin-state, Property 5: time_bucket is deterministic and exhaustive
    # Validates: Requirements 2.3
    # Part C — all 4 buckets are reachable; every hour 0–23 maps to exactly one bucket

    # Build one representative timestamp per hour of the day
    base = datetime.datetime(2024, 6, 15, 0, 0, 0)
    hour_to_bucket: dict[int, int] = {}

    for hour in range(24):
        ts = (base + datetime.timedelta(hours=hour)).timestamp()
        bucket = PreferenceModel.time_bucket(ts)
        hour_to_bucket[hour] = bucket

    # Every hour maps to exactly one bucket (function is deterministic — already guaranteed,
    # but we verify the mapping is consistent with floor(hour / 6))
    for hour, bucket in hour_to_bucket.items():
        expected_bucket = hour // 6
        assert bucket == expected_bucket, (
            f"Hour {hour}: expected bucket {expected_bucket}, got {bucket}"
        )

    # All 4 buckets are reachable
    reachable_buckets = set(hour_to_bucket.values())
    assert reachable_buckets == {0, 1, 2, 3}, (
        f"Not all buckets reachable. Got: {reachable_buckets}"
    )


# ---------------------------------------------------------------------------
# Property 6: source_weights equal success ratio
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    source_observations=st.dictionaries(
        keys=source_st,
        values=st.tuples(
            st.integers(min_value=0, max_value=100),   # success_count
            st.integers(min_value=1, max_value=100),   # total_count (>= 1 to avoid div-by-zero)
        ),
        min_size=1,
        max_size=6,
    ),
    ts=timestamp_st,
)
def test_property_6_source_weights_equal_success_ratio(source_observations, ts):
    # Feature: behavioral-twin-state, Property 6: source_weights equal success ratio
    # Validates: Requirements 2.4

    model = PreferenceModel()

    # Feed observations: for each source, record success_count successes and
    # (total_count - success_count) failures
    for source, (success_count, total_count) in source_observations.items():
        # Clamp success_count to total_count (can't have more successes than total)
        actual_success = min(success_count, total_count)
        failure_count = total_count - actual_success

        for _ in range(actual_success):
            model.update(
                action_verb="CLICK",
                confidence=0.9,
                latency_ms=100.0,
                source=source,
                ts=ts,
                success=True,
            )
        for _ in range(failure_count):
            model.update(
                action_verb="CLICK",
                confidence=0.5,
                latency_ms=200.0,
                source=source,
                ts=ts,
                success=False,
            )

    weights = model.source_weights()

    for source, (success_count, total_count) in source_observations.items():
        actual_success = min(success_count, total_count)
        expected_weight = actual_success / total_count

        assert source in weights, f"Source '{source}' missing from source_weights()"

        w = weights[source]

        # Weight equals success ratio
        assert math.isclose(w, expected_weight, rel_tol=1e-9, abs_tol=1e-12), (
            f"source_weights()['{source}'] = {w}, expected {expected_weight} "
            f"({actual_success}/{total_count})"
        )

        # All weights are in [0.0, 1.0]
        assert 0.0 <= w <= 1.0, (
            f"source_weights()['{source}'] = {w} is outside [0.0, 1.0]"
        )


# ---------------------------------------------------------------------------
# Property 7: PreferenceModel survives restart round-trip
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    observations=st.lists(
        st.tuples(
            action_verb_st,
            confidence_st,
            latency_st,
            source_st,
            timestamp_st,
            st.booleans(),  # success
        ),
        min_size=1,
        max_size=30,
    )
)
def test_property_7_preference_model_round_trip(observations):
    # Feature: behavioral-twin-state, Property 7: PreferenceModel survives restart round-trip
    # Validates: Requirements 2.7

    # Build original model
    original = PreferenceModel()
    for action_verb, confidence, latency_ms, source, ts, success in observations:
        original.update(
            action_verb=action_verb,
            confidence=confidence,
            latency_ms=latency_ms,
            source=source,
            ts=ts,
            success=success,
        )

    # Serialize → deserialize
    json_str = original.to_json()
    restored = PreferenceModel.from_json(json_str)

    # --- action_stats equivalence ---
    assert set(original.action_stats.keys()) == set(restored.action_stats.keys()), (
        "action_stats keys differ after round-trip"
    )
    for verb in original.action_stats:
        orig_s = original.action_stats[verb]
        rest_s = restored.action_stats[verb]

        assert orig_s.frequency == rest_s.frequency, (
            f"action_stats['{verb}'].frequency: {orig_s.frequency} vs {rest_s.frequency}"
        )
        assert math.isclose(orig_s.mean_confidence, rest_s.mean_confidence, rel_tol=1e-9, abs_tol=1e-15), (
            f"action_stats['{verb}'].mean_confidence: {orig_s.mean_confidence} vs {rest_s.mean_confidence}"
        )
        assert math.isclose(orig_s.mean_latency_ms, rest_s.mean_latency_ms, rel_tol=1e-9, abs_tol=1e-15), (
            f"action_stats['{verb}'].mean_latency_ms: {orig_s.mean_latency_ms} vs {rest_s.mean_latency_ms}"
        )

    # --- time_buckets equivalence ---
    assert original.time_buckets == restored.time_buckets, (
        f"time_buckets differ after round-trip:\n"
        f"  original: {original.time_buckets}\n"
        f"  restored: {restored.time_buckets}"
    )

    # --- source_counts equivalence ---
    assert original.source_counts == restored.source_counts, (
        f"source_counts differ after round-trip:\n"
        f"  original: {original.source_counts}\n"
        f"  restored: {restored.source_counts}"
    )
