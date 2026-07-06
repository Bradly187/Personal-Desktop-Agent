"""Regression gate for the Kokoro local TTS backend (default tts_backend).

These tests exist because of a silent-degradation bug found 2026-06-24:
``kokoro-onnx`` is pinned (uncommented) in ``requirements.txt`` and selected as
the default ``tts_backend`` in ``approval_config.json``, but it was missing from
the ``.venv`` the launcher (``start_agent.bat``) actually runs. At runtime
``KokoroClient.speak_sync`` caught the import error and returned ``False`` — so
the agent's spoken output went silently mute, masked because the separate
``approval_hook.py`` consent prompts still spoke via Polly.

The legacy ``tts/test_kokoro.py`` plays audio, has no assertions, and lives
outside ``testpaths=tests`` so pytest never collected it — it could not catch
this class of regression. These tests do, and use plain ``assert`` (no audio
playback) so they are CI-safe.

Layering:
  * dispatch + config tests always run (pure wiring, no model needed);
  * the importability gate HARD-FAILS when the configured default backend is
    not installed — this is the test that would have caught the original bug;
  * the synthesis test is conditional on the package + model weights being
    present (the runtime / dev box), and validates the audio path functionally.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import tts.polly_stream as polly_stream
from tts.polly_stream import _read_tts_config, get_client


@pytest.fixture(autouse=True)
def _reset_kokoro_singleton():
    """Clear the module-level client cache so each test resolves fresh."""
    polly_stream._kokoro_client = None
    yield
    polly_stream._kokoro_client = None


# ---------------------------------------------------------------------------
# Config plumbing — guards the _read_tts_config repo-root path that, when
# broken, silently made tts_backend never honored (always fell back to Polly).
# ---------------------------------------------------------------------------

def test_config_reads_from_repo_root():
    """_read_tts_config() finds approval_config.json at the repo root."""
    cfg = _read_tts_config()
    assert cfg, "approval_config.json read as empty — _read_tts_config path is wrong"
    assert "tts_backend" in cfg, "tts_backend key missing from approval_config.json"


def test_default_backend_is_kokoro():
    """The shipped default backend is Kokoro (drives the gate below)."""
    assert _read_tts_config().get("tts_backend") == "kokoro"


# ---------------------------------------------------------------------------
# Dispatch — get_client() routes the configured backend to KokoroClient with
# the configured voice. No model load (KokoroClient.__init__ is lazy).
# ---------------------------------------------------------------------------

def test_get_client_dispatches_to_kokoro():
    client = get_client(backend="kokoro")
    actual = getattr(client, "_client", client)
    assert actual.__class__.__name__ == "KokoroClient"


def test_kokoro_client_uses_configured_voice():
    cfg = _read_tts_config()
    client = get_client(backend="kokoro")
    actual = getattr(client, "_client", client)
    assert actual.voice == cfg.get("kokoro_voice", "af_bella")


def test_get_client_honors_config_default():
    """With backend=None, get_client() resolves via approval_config.json."""
    client = get_client()
    # config default is kokoro; assert we did NOT silently fall back to Polly.
    actual = getattr(client, "_client", client)
    assert actual.__class__.__name__ == "KokoroClient"


# ---------------------------------------------------------------------------
# THE gap-catcher: the configured default backend must be importable. This is
# the assertion that would have failed on the broken runtime venv.
# ---------------------------------------------------------------------------

def test_configured_default_backend_is_importable():
    backend = _read_tts_config().get("tts_backend", "polly")
    if backend != "kokoro":
        pytest.skip(f"default tts_backend is {backend!r}, not kokoro")
    assert importlib.util.find_spec("kokoro_onnx") is not None, (
        "tts_backend=kokoro but kokoro_onnx is not installed in this interpreter. "
        "The default voice output will be silently mute. "
        "Fix: pip install -r requirements.txt"
    )


# ---------------------------------------------------------------------------
# Functional synthesis — conditional on the capable env (package + weights).
# Validates that create() actually produces non-empty 24 kHz audio.
# ---------------------------------------------------------------------------

def _weights_present() -> bool:
    base = _REPO_ROOT / "tts"
    return (base / "kokoro-v1.0.onnx").exists() and (base / "voices.bin").exists()


def test_kokoro_synthesizes_nonempty_audio():
    pytest.importorskip("kokoro_onnx", reason="kokoro-onnx not installed")
    np = pytest.importorskip("numpy")
    if not _weights_present():
        pytest.skip("Kokoro model weights not downloaded (run tts/download_kokoro.py)")

    from tts.kokoro_tts import _get_kokoro

    kokoro = _get_kokoro()
    samples, sample_rate = kokoro.create(
        "Regression gate synthesis check.", voice="af_bella", speed=1.0, lang="en-us"
    )
    assert sample_rate == 24000, f"expected 24 kHz, got {sample_rate}"
    assert samples is not None and len(samples) > 0, "synthesis returned empty audio"
    assert float(np.max(np.abs(samples))) > 0.0, "synthesis returned silence"
