"""Tests for VramArbiter (orchestration gap B) — single source of VRAM admission policy.

Verifies the admit rule (matches the historical `<= free + tolerance`), fail-open
when VRAM is unmeasurable, headroom, and that ModelRouter routes its selection
through the arbiter (behaviour preserved).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vram_arbiter import VramArbiter
from core.vram import UNKNOWN_FREE_GB


# ---------------------------------------------------------------------------
# can_admit
# ---------------------------------------------------------------------------

def test_admits_when_fits_within_tolerance():
    a = VramArbiter(tolerance_gb=2.0)
    assert a.can_admit(18.0, free_gb=20.0) is True
    assert a.can_admit(21.9, free_gb=20.0) is True     # within +2.0 tolerance
    assert a.can_admit(22.1, free_gb=20.0) is False


def test_boundary_matches_historical_rule():
    a = VramArbiter(tolerance_gb=2.0)
    # Historical: profile.vram_gb <= free + 2.0  →  4.6 <= 2.6 + 2.0 == 4.6 True
    assert a.can_admit(4.6, free_gb=2.6) is True
    assert a.can_admit(4.7, free_gb=2.6) is False


def test_fails_open_when_vram_unmeasurable():
    a = VramArbiter()
    assert a.can_admit(30.0, free_gb=UNKNOWN_FREE_GB) is True   # don't block when unknown


def test_can_admit_probes_when_free_omitted(monkeypatch):
    import core.vram_arbiter as mod
    monkeypatch.setattr(mod, "free_vram_gb", lambda: 10.0)
    a = VramArbiter(tolerance_gb=2.0)
    assert a.can_admit(11.0) is True
    assert a.can_admit(13.0) is False


def test_headroom():
    a = VramArbiter(min_free_floor_gb=2.0)
    assert a.headroom_gb(free_gb=10.0) == 8.0
    assert a.headroom_gb(free_gb=1.0) == -1.0           # below floor
    assert a.headroom_gb(free_gb=UNKNOWN_FREE_GB) == UNKNOWN_FREE_GB


def test_status_reports_none_free_when_unknown(monkeypatch):
    import core.vram_arbiter as mod
    monkeypatch.setattr(mod, "free_vram_gb", lambda: UNKNOWN_FREE_GB)
    st = VramArbiter().get_status()
    assert st["free_gb"] is None
    assert st["tolerance_gb"] == 2.0


# ---------------------------------------------------------------------------
# ModelRouter routes selection through the arbiter
# ---------------------------------------------------------------------------

def _patch_vram(free_gb: float):
    mock_nvml = MagicMock()
    mock_nvml.nvmlDeviceGetMemoryInfo.return_value = MagicMock(
        free=int(free_gb * 1024 ** 3), used=0, total=int(32 * 1024 ** 3)
    )
    mock_nvml.nvmlDeviceGetHandleByIndex.return_value = object()
    return patch.dict("sys.modules", {"pynvml": mock_nvml})


def test_router_selection_uses_arbiter_ample_vram():
    from inference.model_router import ModelRouter
    with _patch_vram(20.0):
        r = ModelRouter()
        p = r.select_profile("vision")
        assert p.name == "qwen3-vl:30b"           # 19 GB fits in 20+2


def test_router_selection_uses_arbiter_low_vram():
    from inference.model_router import ModelRouter
    with _patch_vram(5.0):
        r = ModelRouter()
        p = r.select_profile("vision")
        # 19 GB and 16 GB don't fit in 5+2; falls back down the chain to 8b.
        assert p.name == "llama3.1:8b"


def test_router_status_includes_arbiter():
    from inference.model_router import ModelRouter
    with _patch_vram(20.0):
        st = ModelRouter().get_status()
        assert "vram_arbiter" in st
        assert st["vram_arbiter"]["tolerance_gb"] == 2.0
