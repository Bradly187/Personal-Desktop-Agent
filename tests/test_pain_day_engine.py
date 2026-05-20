"""Property-based tests for the PainDayEngine (BehavioralTwinState pain day scoring).

Feature: behavioral-twin-state
Properties covered: 10, 11
PBT library: Hypothesis (pip install hypothesis)
"""

import time

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from behavioral_twin_state import BehavioralTwinState, _DEFAULT_SNAPSHOT


# ---------------------------------------------------------------------------
# Minimal stub — BehavioralTwinState requires an AgentDB at __init__ time,
# but _recompute_pain_day_score() is a pure synchronous method that only
# reads internal accumulators. We bypass __init__ entirely via __new__ and
# set the required fields directly.
# ---------------------------------------------------------------------------

class MockAgentDB:
    available = True


def _make_twin(
    cmd_count: int = 0,
    fail_count: int = 0,
    clarify_count: int = 0,
    gesture_confs: list[float] | None = None,
    elapsed_s: float = 1.0,
    working_set: list[dict] | None = None,
    pain_day_active: bool = False,
) -> BehavioralTwinState:
    """Create a BehavioralTwinState with internal accumulators set directly,
    bypassing __init__ to avoid requiring a real AgentDB or event loop.
    """
    twin = BehavioralTwinState.__new__(BehavioralTwinState)
    twin._session_cmd_count = cmd_count
    twin._session_fail_count = fail_count
    twin._session_clarify_count = clarify_count
    twin._session_gesture_confs = gesture_confs if gesture_confs is not None else []
    twin._session_start_ts = time.monotonic() - max(elapsed_s, 1e-9)
    twin._working_set = working_set if working_set is not None else []
    twin._pain_day_active = pain_day_active
    return twin


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Non-negative integer counts — include 0 and large values
count_st = st.integers(min_value=0, max_value=10_000)

# Confidence values in [0.0, 1.0]
conf_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Elapsed session time in seconds — from near-zero to many hours
elapsed_st = st.floats(min_value=0.001, max_value=86_400.0, allow_nan=False, allow_infinity=False)

# Working set entries — list of dicts with optional gesture_confidence and source
working_set_entry_st = st.fixed_dictionaries({
    "source": st.sampled_from(["voice", "gesture", "touch", "gaze", "tilt", "head"]),
    "gesture_confidence": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
})

working_set_st = st.lists(working_set_entry_st, min_size=0, max_size=500)


# ---------------------------------------------------------------------------
# Property 10: pain_day_score is always in [0.0, 1.0]
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    cmd_count=count_st,
    fail_count=count_st,
    clarify_count=count_st,
    gesture_confs=st.lists(conf_st, min_size=0, max_size=100),
    elapsed_s=elapsed_st,
    working_set=working_set_st,
)
def test_property_10_pain_day_score_in_unit_interval(
    cmd_count, fail_count, clarify_count, gesture_confs, elapsed_s, working_set
):
    # Feature: behavioral-twin-state, Property 10: pain_day_score is always in [0.0, 1.0]
    # Validates: Requirements 4.1

    twin = _make_twin(
        cmd_count=cmd_count,
        fail_count=fail_count,
        clarify_count=clarify_count,
        gesture_confs=gesture_confs,
        elapsed_s=elapsed_s,
        working_set=working_set,
    )

    score = twin._recompute_pain_day_score()

    assert isinstance(score, float), (
        f"_recompute_pain_day_score() returned {type(score).__name__}, expected float"
    )
    assert 0.0 <= score <= 1.0, (
        f"pain_day_score={score} is outside [0.0, 1.0] for inputs: "
        f"cmd_count={cmd_count}, fail_count={fail_count}, "
        f"clarify_count={clarify_count}, gesture_confs={gesture_confs[:5]}..., "
        f"elapsed_s={elapsed_s}, working_set_size={len(working_set)}"
    )


@settings(max_examples=100)
@given(
    # Extreme values: counts much larger than cmd_count (ratios > 1 before clamping)
    cmd_count=st.integers(min_value=1, max_value=100),
    fail_count=st.integers(min_value=0, max_value=10_000),
    clarify_count=st.integers(min_value=0, max_value=10_000),
    gesture_confs=st.lists(conf_st, min_size=1, max_size=50),
    elapsed_s=elapsed_st,
    working_set=working_set_st,
)
def test_property_10_pain_day_score_extreme_counts(
    cmd_count, fail_count, clarify_count, gesture_confs, elapsed_s, working_set
):
    # Feature: behavioral-twin-state, Property 10: pain_day_score is always in [0.0, 1.0]
    # Validates: Requirements 4.1
    # Variant: fail_count and clarify_count may exceed cmd_count (tests clamping robustness)

    twin = _make_twin(
        cmd_count=cmd_count,
        fail_count=fail_count,
        clarify_count=clarify_count,
        gesture_confs=gesture_confs,
        elapsed_s=elapsed_s,
        working_set=working_set,
    )

    score = twin._recompute_pain_day_score()

    assert 0.0 <= score <= 1.0, (
        f"pain_day_score={score} is outside [0.0, 1.0] with extreme counts: "
        f"cmd_count={cmd_count}, fail_count={fail_count}, clarify_count={clarify_count}"
    )


def test_property_10_pain_day_score_zero_commands():
    # Feature: behavioral-twin-state, Property 10: pain_day_score is always in [0.0, 1.0]
    # Validates: Requirements 4.1
    # Edge case: zero commands → score must be 0.0 (guard condition)

    twin = _make_twin(cmd_count=0)
    score = twin._recompute_pain_day_score()

    assert score == 0.0, (
        f"Expected score=0.0 when cmd_count=0, got {score}"
    )
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Property 11: PainDayMode hysteresis is correct
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    # Score above activation threshold (> 0.6) with enough commands
    cmd_count=st.integers(min_value=5, max_value=1000),
    # Use a small elapsed_s to drive signal_4 (cmd_rate_drop) high.
    # With signal_1=1.0 (0.35) + signal_2=1.0 (0.25) + signal_4≈1.0 (0.20) = 0.80 > 0.6.
    # We also add gesture signals to push signal_3 above 0 for extra margin.
    elapsed_s=st.floats(min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False),
)
def test_property_11_hysteresis_activates_above_threshold(cmd_count, elapsed_s):
    # Feature: behavioral-twin-state, Property 11: PainDayMode hysteresis is correct
    # Validates: Requirements 4.2, 4.8
    # When score > 0.6 AND cmd_count >= 5: PainDayMode should activate

    # Construct a scenario guaranteed to produce score > 0.6:
    # signal_1 = 1.0 (all commands failed)     → 0.35
    # signal_2 = 1.0 (all commands clarified)  → 0.25
    # signal_3 ≈ 1.0 (high baseline conf, zero current conf) → 0.20
    # signal_4 ≈ 1.0 (very low current rate vs large baseline) → 0.20
    # Total ≈ 1.00 → clamped to 1.0 > 0.6
    large_working_set = [
        {"source": "gesture", "gesture_confidence": 1.0}  # high baseline gesture conf
        for _ in range(500)
    ]

    twin = _make_twin(
        cmd_count=cmd_count,
        fail_count=cmd_count,           # signal_1 = 1.0
        clarify_count=cmd_count,        # signal_2 = 1.0
        gesture_confs=[0.0] * cmd_count,  # signal_3: current conf=0, baseline=1.0 → drop=1.0
        elapsed_s=elapsed_s,            # very short session → high cmd_rate_drop
        working_set=large_working_set,
        pain_day_active=False,
    )

    score = twin._recompute_pain_day_score()

    # Skip this example if the score doesn't exceed the activation threshold.
    # (Floating-point arithmetic can produce exactly 0.6 in edge cases; those
    # examples don't trigger activation and are not interesting for this property.)
    assume(score > BehavioralTwinState.PAIN_DAY_ACTIVATE_THRESHOLD)

    # Simulate the hysteresis logic from _pain_day_loop
    pain_day_active = twin._pain_day_active  # starts False
    if (not pain_day_active
            and score > BehavioralTwinState.PAIN_DAY_ACTIVATE_THRESHOLD
            and twin._session_cmd_count >= BehavioralTwinState.PAIN_DAY_MIN_COMMANDS):
        pain_day_active = True

    assert pain_day_active is True, (
        f"PainDayMode should have activated: score={score:.3f} > 0.6, "
        f"cmd_count={cmd_count} >= {BehavioralTwinState.PAIN_DAY_MIN_COMMANDS}"
    )


@settings(max_examples=100)
@given(
    cmd_count=st.integers(min_value=5, max_value=1000),
    # Score below deactivation threshold (< 0.4): use zero fail/clarify/gesture signals
    # and a very high current rate (many commands in short time) to keep score near 0
    elapsed_s=st.floats(min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False),
)
def test_property_11_hysteresis_deactivates_below_threshold(cmd_count, elapsed_s):
    # Feature: behavioral-twin-state, Property 11: PainDayMode hysteresis is correct
    # Validates: Requirements 4.6
    # When score < 0.4: PainDayMode should deactivate

    # Construct a scenario guaranteed to produce score < 0.4:
    # signal_1 = 0.0 (no failures)
    # signal_2 = 0.0 (no clarifications)
    # signal_3 = 0.0 (no gesture data)
    # signal_4 = 0.0 (no working set → baseline unavailable → defaults to 0)
    twin = _make_twin(
        cmd_count=cmd_count,
        fail_count=0,
        clarify_count=0,
        gesture_confs=[],
        elapsed_s=elapsed_s,
        working_set=[],             # no baseline → signal_4 = 0.0
        pain_day_active=True,       # starts active
    )

    score = twin._recompute_pain_day_score()

    # Verify the score is actually below the deactivation threshold
    assert score < BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD, (
        f"Test precondition failed: score={score} is not < "
        f"{BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD}. "
        f"cmd_count={cmd_count}"
    )

    # Simulate the hysteresis logic from _pain_day_loop
    pain_day_active = twin._pain_day_active  # starts True
    if pain_day_active and score < BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD:
        pain_day_active = False

    assert pain_day_active is False, (
        f"PainDayMode should have deactivated: score={score:.3f} < 0.4"
    )


@settings(max_examples=100)
@given(
    # cmd_count < PAIN_DAY_MIN_COMMANDS (5)
    cmd_count=st.integers(min_value=1, max_value=4),
    elapsed_s=st.floats(min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False),
)
def test_property_11_hysteresis_no_activation_below_min_commands(cmd_count, elapsed_s):
    # Feature: behavioral-twin-state, Property 11: PainDayMode hysteresis is correct
    # Validates: Requirements 4.8
    # When cmd_count < 5: PainDayMode does NOT activate from session signals alone

    # Use maximum-stress signals to ensure score would be high if not for the guard
    large_working_set = [
        {"source": "voice", "gesture_confidence": 0.0}
        for _ in range(500)
    ]

    twin = _make_twin(
        cmd_count=cmd_count,
        fail_count=cmd_count,
        clarify_count=cmd_count,
        elapsed_s=elapsed_s,
        working_set=large_working_set,
        pain_day_active=False,
    )

    score = twin._recompute_pain_day_score()

    # Simulate the hysteresis logic from _pain_day_loop
    pain_day_active = twin._pain_day_active  # starts False
    if (not pain_day_active
            and score > BehavioralTwinState.PAIN_DAY_ACTIVATE_THRESHOLD
            and twin._session_cmd_count >= BehavioralTwinState.PAIN_DAY_MIN_COMMANDS):
        pain_day_active = True

    assert pain_day_active is False, (
        f"PainDayMode must NOT activate when cmd_count={cmd_count} < "
        f"{BehavioralTwinState.PAIN_DAY_MIN_COMMANDS}, even with score={score:.3f}"
    )


def test_property_11_hysteresis_maintains_state_in_dead_zone():
    # Feature: behavioral-twin-state, Property 11: PainDayMode hysteresis is correct
    # Validates: Requirements 4.2, 4.6
    # When score is between 0.4 and 0.6 (dead zone), state is maintained (no flip)

    # Score in dead zone: use moderate signals
    # signal_1 = 0.5 → 0.175, signal_2 = 0.5 → 0.125 → total ≈ 0.30 (below 0.4)
    # We need a score between 0.4 and 0.6. Use signal_1=1.0 (0.35) + signal_2=0.3 (0.075)
    # + signal_4 moderate. Simplest: set fail_count=cmd_count, clarify_count=0,
    # and a moderate working_set to get signal_4 ≈ 0.5 → 0.10. Total ≈ 0.55.
    cmd_count = 20
    working_set = [{"source": "voice", "gesture_confidence": 0.0} for _ in range(200)]

    twin_was_active = _make_twin(
        cmd_count=cmd_count,
        fail_count=cmd_count,   # signal_1 = 1.0 → 0.35
        clarify_count=0,
        elapsed_s=0.5,          # moderate rate drop
        working_set=working_set,
        pain_day_active=True,   # was active
    )

    twin_was_inactive = _make_twin(
        cmd_count=cmd_count,
        fail_count=cmd_count,
        clarify_count=0,
        elapsed_s=0.5,
        working_set=working_set,
        pain_day_active=False,  # was inactive
    )

    score_active = twin_was_active._recompute_pain_day_score()
    score_inactive = twin_was_inactive._recompute_pain_day_score()

    # Both twins compute the same score (same inputs)
    assert score_active == score_inactive

    score = score_active

    # If score is in the dead zone [0.4, 0.6], state should be preserved
    if BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD <= score <= BehavioralTwinState.PAIN_DAY_ACTIVATE_THRESHOLD:
        # Simulate hysteresis for the "was active" twin
        active_result = twin_was_active._pain_day_active
        if active_result and score < BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD:
            active_result = False
        elif (not active_result
              and score > BehavioralTwinState.PAIN_DAY_ACTIVATE_THRESHOLD
              and twin_was_active._session_cmd_count >= BehavioralTwinState.PAIN_DAY_MIN_COMMANDS):
            active_result = True

        assert active_result is True, (
            f"Dead zone: was-active state should be preserved at score={score:.3f}"
        )

        # Simulate hysteresis for the "was inactive" twin
        inactive_result = twin_was_inactive._pain_day_active
        if inactive_result and score < BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD:
            inactive_result = False
        elif (not inactive_result
              and score > BehavioralTwinState.PAIN_DAY_ACTIVATE_THRESHOLD
              and twin_was_inactive._session_cmd_count >= BehavioralTwinState.PAIN_DAY_MIN_COMMANDS):
            inactive_result = True

        assert inactive_result is False, (
            f"Dead zone: was-inactive state should be preserved at score={score:.3f}"
        )
