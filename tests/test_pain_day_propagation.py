"""End-to-end propagation tests for the pain-day / flare subsystem.

Covers gap G7 from the gap analysis: that a pain-day transition (auto *or*
manual) actually relaxes the voice recognizer (both VAD and logprob floor),
that the manual and auto paths converge to the same recognizer state, and that
the flare_profile degrade flags gate which consumers relax.

These are the integration checks that would have caught G1 (auto path never
touched the recognizer) and G2 (logprob floor computed but never applied).
"""

import time

import pytest

from adaptive.behavioral_twin_state import (
    BehavioralTwinState, PreferenceModel, _DEFAULT_SNAPSHOT,
)
from sensors.whisper_stream import WhisperStream
from sensors.gesture_processor import GestureProcessor


# ---------------------------------------------------------------------------
# Lightweight stubs — mirror the real AcousticProfiler threshold contract
# without loading audio hardware or models.
# ---------------------------------------------------------------------------

class FakeProfiler:
    """Matches AcousticProfiler.get_vad_threshold / get_logprob_floor."""

    def __init__(self, vad: float = 0.020, floor: float = -0.80,
                 flare_scale: float = 0.5):
        self._vad = vad
        self._floor = floor
        self._scale = flare_scale

    def get_vad_threshold(self, pain_day: bool = False) -> float:
        return self._vad * self._scale if pain_day else self._vad

    def get_logprob_floor(self, pain_day: bool = False) -> float:
        return self._floor - 0.20 if pain_day else self._floor


class RecordingGovernor:
    def __init__(self):
        self.scores: list[float] = []

    def notify_pain_day_change(self, score: float) -> None:
        self.scores.append(score)


class MockAgentDB:
    available = False  # skip the async DB persist path (no event loop in test)
    def __init__(self):
        self.commands = self
        self.sessions = self
        self.profile = self
        self.telemetry = self
        self.routing = self
        self.misc = self
        self.memory = self
        self.events = self


def _make_whisper(profiler=None) -> WhisperStream:
    ws = WhisperStream.__new__(WhisperStream)
    ws._pain_day_active = False
    ws._profiler = profiler
    if profiler is not None:
        ws._silence_thresh = profiler.get_vad_threshold()
        ws._logprob_floor_override = profiler.get_logprob_floor()
    else:
        ws._silence_thresh = 0.020
        ws._logprob_floor_override = None
    return ws


def _make_gesture() -> GestureProcessor:
    g = GestureProcessor.__new__(GestureProcessor)
    g._pain_day_active = False
    return g


def _make_twin(
    *,
    voice_degrades: bool = True,
    gesture_degrades: bool = True,
    tilt_degrades: bool = True,
    sound_degrades: bool = True,
    gesture=None,
    governor=None,
) -> BehavioralTwinState:
    """BehavioralTwinState with just enough state for _build_snapshot,
    set_manual_pain_day, and _on_pain_day_transition — bypassing __init__.
    """
    twin = BehavioralTwinState.__new__(BehavioralTwinState)
    twin._preference_model = PreferenceModel()
    twin._session_history = {"accessibility": [], "dev_agent": []}
    twin._session_cmd_count = 0
    twin._pain_day_score = 0.0
    twin._pain_day_active = False
    twin._manual_pain_day = False
    twin._is_ready = True
    twin._agent_db = MockAgentDB()
    twin._bg_tasks = set()
    twin._resource_governor = governor
    twin._gesture = gesture
    twin._flare_voice_degrades = voice_degrades
    twin._flare_gesture_degrades = gesture_degrades
    twin._flare_tilt_degrades = tilt_degrades
    twin._flare_sound_degrades = sound_degrades
    return twin


# ---------------------------------------------------------------------------
# G1 + G2: apply_pain_day moves BOTH thresholds
# ---------------------------------------------------------------------------

def test_whisper_apply_pain_day_moves_vad_and_logprob():
    prof = FakeProfiler(vad=0.020, floor=-0.80, flare_scale=0.5)
    ws = _make_whisper(prof)
    base_vad, base_floor = ws._silence_thresh, ws._logprob_floor_override

    ws.apply_pain_day(True)

    assert ws._silence_thresh == pytest.approx(0.010)        # 0.020 * 0.5
    assert ws._logprob_floor_override == pytest.approx(-1.00)  # -0.80 - 0.20
    assert ws._silence_thresh < base_vad
    assert ws._logprob_floor_override < base_floor


def test_whisper_apply_pain_day_restores_baseline():
    prof = FakeProfiler()
    ws = _make_whisper(prof)
    ws.apply_pain_day(True)
    ws.apply_pain_day(False)
    assert ws._silence_thresh == pytest.approx(prof.get_vad_threshold())
    assert ws._logprob_floor_override == pytest.approx(prof.get_logprob_floor())


def test_whisper_apply_pain_day_idempotent_no_profiler():
    ws = _make_whisper(profiler=None)  # graceful: no profiler wired
    ws.apply_pain_day(True)            # must not raise
    assert ws._pain_day_active is True
    assert ws._logprob_floor_override is None  # untouched without a profiler


# ---------------------------------------------------------------------------
# Manual + auto convergence: both paths drive the same route() expression to
# the same recognizer state.
# ---------------------------------------------------------------------------

def _route_voice_push(ws: WhisperStream, snapshot) -> None:
    """Mirror the HybridCoordinator.route() voice push exactly."""
    ws.apply_pain_day(snapshot.pain_day_active and snapshot.flare_voice_degrades)


def test_manual_toggle_rebuilds_snapshot_for_route():
    # Latent-bug fix: set_manual_pain_day must refresh the cached snapshot so
    # route() sees the new state on the next command, not 60s later.
    twin = _make_twin()
    assert twin._current_snapshot if hasattr(twin, "_current_snapshot") else True
    twin.set_manual_pain_day(True)
    assert twin._current_snapshot.pain_day_active is True
    assert twin._current_snapshot.flare_voice_degrades is True


def test_manual_and_auto_paths_converge():
    prof = FakeProfiler()
    # Manual path: snapshot rebuilt by set_manual_pain_day
    twin_m = _make_twin()
    twin_m.set_manual_pain_day(True)
    ws_manual = _make_whisper(prof)
    _route_voice_push(ws_manual, twin_m._current_snapshot)

    # Auto path: same flag arrives via _build_snapshot from the 60s loop
    twin_a = _make_twin()
    twin_a._pain_day_active = True
    snap_auto = twin_a._build_snapshot()
    ws_auto = _make_whisper(prof)
    _route_voice_push(ws_auto, snap_auto)

    assert ws_manual._silence_thresh == ws_auto._silence_thresh
    assert ws_manual._logprob_floor_override == ws_auto._logprob_floor_override
    assert ws_auto._silence_thresh == pytest.approx(prof.get_vad_threshold(pain_day=True))


# ---------------------------------------------------------------------------
# G5: flare_profile degrade flags gate each consumer
# ---------------------------------------------------------------------------

def test_voice_flag_off_keeps_recognizer_at_baseline():
    prof = FakeProfiler()
    twin = _make_twin(voice_degrades=False)
    twin._pain_day_active = True
    snap = twin._build_snapshot()
    assert snap.flare_voice_degrades is False

    ws = _make_whisper(prof)
    _route_voice_push(ws, snap)  # active AND not-voice_degrades → False
    assert ws._silence_thresh == pytest.approx(prof.get_vad_threshold(pain_day=False))


def test_tilt_flag_gates_fusion_expression():
    twin = _make_twin(tilt_degrades=False)
    twin._pain_day_active = True
    snap = twin._build_snapshot()
    # This is exactly what route() passes to fusion.apply_pain_day(...)
    assert (snap.pain_day_active and snap.flare_tilt_degrades) is False


def test_transition_gates_gesture_but_not_governor():
    gesture = _make_gesture()
    governor = RecordingGovernor()
    twin = _make_twin(gesture_degrades=False, gesture=gesture, governor=governor)

    twin._on_pain_day_transition(True)

    # Governor always notified (VRAM/flare response is symptom-agnostic)...
    assert governor.scores == [1.0]
    # ...but gesture floor stays off because gesture_degrades is False.
    assert gesture._pain_day_active is False


def test_transition_applies_gesture_when_flag_set():
    gesture = _make_gesture()
    governor = RecordingGovernor()
    twin = _make_twin(gesture_degrades=True, gesture=gesture, governor=governor)

    twin._on_pain_day_transition(True)
    assert gesture._pain_day_active is True
    assert governor.scores == [1.0]

    twin._on_pain_day_transition(False)
    assert gesture._pain_day_active is False
    assert governor.scores == [1.0, 0.0]


# ---------------------------------------------------------------------------
# Default snapshot is safe (flags default True → no behavior change pre-config)
# ---------------------------------------------------------------------------

def test_default_snapshot_flags_default_true():
    assert _DEFAULT_SNAPSHOT.flare_voice_degrades is True
    assert _DEFAULT_SNAPSHOT.flare_gesture_degrades is True
    assert _DEFAULT_SNAPSHOT.flare_tilt_degrades is True
    assert _DEFAULT_SNAPSHOT.flare_sound_degrades is True


# ---------------------------------------------------------------------------
# sound_degrades is independent of tilt (the decoupled flag)
# ---------------------------------------------------------------------------

def test_sound_and_tilt_gate_independently():
    # Tilt fine, mouth-sounds weaken: sound relaxes, tilt does not.
    twin = _make_twin(tilt_degrades=False, sound_degrades=True)
    twin._pain_day_active = True
    snap = twin._build_snapshot()
    # These mirror exactly what route() passes to fusion.apply_pain_day(...)
    assert (snap.pain_day_active and snap.flare_tilt_degrades) is False
    assert (snap.pain_day_active and snap.flare_sound_degrades) is True


def test_sound_flag_off_keeps_sound_baseline():
    twin = _make_twin(sound_degrades=False)
    twin._pain_day_active = True
    snap = twin._build_snapshot()
    assert (snap.pain_day_active and snap.flare_sound_degrades) is False


def test_set_flare_profile_updates_live_and_rebuilds_snapshot():
    twin = _make_twin()  # all True
    twin._pain_day_active = True
    twin._current_snapshot = twin._build_snapshot()
    assert twin._current_snapshot.flare_sound_degrades is True

    twin.set_flare_profile({"sound_degrades": False, "tilt_degrades": False})

    assert twin._flare_sound_degrades is False
    assert twin._flare_tilt_degrades is False
    assert twin._flare_voice_degrades is True  # untouched key preserved
    # Snapshot rebuilt so route() honours the change on the next command.
    assert twin._current_snapshot.flare_sound_degrades is False
    assert twin._current_snapshot.flare_tilt_degrades is False


# ---------------------------------------------------------------------------
# DB round-trip: upsert_flare_profile <-> get_flare_profile (sound_degrades)
# ---------------------------------------------------------------------------

def test_flare_profile_db_roundtrip(tmp_path):
    import asyncio
    from storage.db import AgentDB, _AIOSQLITE_AVAILABLE

    if not _AIOSQLITE_AVAILABLE:
        pytest.skip("aiosqlite not installed")

    async def run():
        db = AgentDB()
        await db.open(str(tmp_path / "flare.db"))
        await db.profile.upsert_flare_profile({
            "voice_degrades": True, "gesture_degrades": True,
            "tilt_degrades": False, "sound_degrades": False,
            "flare_vad_scale": 0.4,
        })
        prof = await db.profile.get_flare_profile()
        # Partial update must preserve untouched columns.
        await db.profile.upsert_flare_profile({"sound_degrades": True})
        prof2 = await db.profile.get_flare_profile()
        await db.close()
        return prof, prof2

    prof, prof2 = asyncio.run(run())
    assert prof["sound_degrades"] is False
    assert prof["tilt_degrades"] is False
    assert prof["gesture_degrades"] is True
    assert prof["flare_vad_scale"] == pytest.approx(0.4)
    # Second upsert flipped only sound; tilt stays False.
    assert prof2["sound_degrades"] is True
    assert prof2["tilt_degrades"] is False
