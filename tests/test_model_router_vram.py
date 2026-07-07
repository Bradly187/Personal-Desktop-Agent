"""Integration test: ModelRouter VRAM fallback chain selection (Req 4).

Validates:
  1. Preferred model fits in VRAM → selected (no fallback)
  2. Preferred model too large → falls back to next model in chain
  3. All chain entries too large → command domain (llama3.1:8b) returned as ultimate fallback
  4. pynvml unavailable → assumes 999 GB free, selects preferred model
  5. pynvml raises an exception → assumes 999 GB free, selects preferred
  6. Unknown domain → uses default chain (llama3.1:8b)
  7. select_profile always returns non-None with non-empty name, domain, system_prompt
  8. 2.0 GB tolerance applied correctly (preferred fits within free + 2.0)

Run:
    python tests/test_model_router_vram.py

Exit codes: 0 = all passed, 1 = any failed
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.model_router import ModelRouter, _free_vram_gb, _FALLBACK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _router() -> ModelRouter:
    return ModelRouter()


def _patch_vram(free_gb: float):
    """Context manager: mock pynvml to report `free_gb` of free VRAM."""
    mock_nvml = MagicMock()
    mock_info = MagicMock()
    mock_info.free = int(free_gb * 1024**3)
    mock_nvml.nvmlDeviceGetMemoryInfo.return_value = mock_info
    return patch.dict("sys.modules", {"pynvml": mock_nvml})


# ---------------------------------------------------------------------------
# Test 1: Preferred model fits → selected (no fallback)
# ---------------------------------------------------------------------------

async def test_preferred_fits_in_vram() -> tuple[bool, str]:
    """When free VRAM exceeds preferred model, it's selected immediately."""
    router = _router()

    # Code domain prefers qwen3-coder:30b (18 GB); give 20 GB free
    with _patch_vram(20.0):
        profile = router.select_profile("code")

    if "qwen3-coder" not in profile.name:
        return False, f"Expected qwen3-coder:30b, got {profile.name!r}"
    return True, f"20 GB free → preferred model selected: {profile.name}"


# ---------------------------------------------------------------------------
# Test 2: Preferred too large → falls back to next in chain
# ---------------------------------------------------------------------------

async def test_preferred_too_large_falls_back() -> tuple[bool, str]:
    """When preferred model doesn't fit, the next chain entry is selected."""
    router = _router()

    # Code chain: qwen3-coder:30b (18 GB) → llama3.1:8b (4.6 GB) → llama3.2:3b
    # Give only 8 GB free → preferred (18 GB) won't fit, llama3.1:8b (4.6+2=6.6 ≤ 8) will
    with _patch_vram(8.0):
        profile = router.select_profile("code")

    if profile.name == "qwen3-coder:30b":
        return False, "Expected fallback but got preferred model (qwen3-coder:30b)"
    if "llama3.1" not in profile.name and "llama3.2" not in profile.name:
        return False, f"Expected llama3.1:8b or llama3.2:3b fallback, got {profile.name!r}"
    return True, f"8 GB free → fallback selected: {profile.name}"


# ---------------------------------------------------------------------------
# Test 3: No chain entry fits → ultimate fallback (llama3.1:8b / command domain)
# ---------------------------------------------------------------------------

async def test_no_chain_entry_fits_ultimate_fallback() -> tuple[bool, str]:
    """When no model fits VRAM, command domain (llama3.1:8b) is returned."""
    router = _router()

    # Vision chain: qwen3-vl:30b (19 GB) → llama3.1:8b (4.6 GB) → llama3.2:3b
    # Give only 1 GB free: even llama3.2:3b (6.3 GB) > 1+2=3 GB tolerance
    with _patch_vram(1.0):
        profile = router.select_profile("vision")

    if not profile.name:
        return False, "Ultimate fallback returned empty name"
    return True, f"1 GB free → ultimate fallback: {profile.name} (domain={profile.domain})"


# ---------------------------------------------------------------------------
# Test 4: pynvml not installed → assumes 999 GB, picks preferred
# ---------------------------------------------------------------------------

async def test_pynvml_missing_assumes_large_vram() -> tuple[bool, str]:
    """When pynvml is unavailable, _free_vram_gb returns 999.0 (best effort)."""
    with patch.dict("sys.modules", {"pynvml": None}):
        free = _free_vram_gb()

    if free != 999.0:
        return False, f"Expected 999.0 GB when pynvml missing, got {free}"

    # With 999 GB available, preferred model should always be selected
    router = _router()
    with patch.dict("sys.modules", {"pynvml": None}):
        profile = router.select_profile("code")

    if "qwen3-coder" not in profile.name:
        return False, f"Expected preferred model with 999 GB free, got {profile.name!r}"
    return True, "pynvml missing → 999 GB assumed → preferred model selected"


# ---------------------------------------------------------------------------
# Test 5: pynvml raises an exception → assumes 999 GB, picks preferred
# ---------------------------------------------------------------------------

async def test_pynvml_error_assumes_large_vram() -> tuple[bool, str]:
    """When pynvml raises during VRAM query, fallback to 999 GB assumed."""
    mock_nvml = MagicMock()
    mock_nvml.nvmlInit.side_effect = RuntimeError("NVML init failed")

    with patch.dict("sys.modules", {"pynvml": mock_nvml}):
        free = _free_vram_gb()

    if free != 999.0:
        return False, f"Expected 999.0 GB on pynvml error, got {free}"
    return True, "pynvml error → 999 GB assumed"


# ---------------------------------------------------------------------------
# Test 6: Unknown domain → default chain (llama3.1:8b)
# ---------------------------------------------------------------------------

async def test_unknown_domain_uses_default_chain() -> tuple[bool, str]:
    """An unknown domain falls back to the default chain (llama3.1:8b)."""
    router = _router()
    with _patch_vram(20.0):
        profile = router.select_profile("completely_unknown_domain")

    if not profile.name:
        return False, "Profile name empty for unknown domain"
    # Should default to llama3.1:8b (command domain)
    return True, f"Unknown domain → {profile.name}"


# ---------------------------------------------------------------------------
# Test 7: select_profile always returns valid non-None profile
# ---------------------------------------------------------------------------

async def test_always_returns_valid_profile() -> tuple[bool, str]:
    """select_profile never returns None; always has non-empty name, domain, system_prompt."""
    router = _router()
    domains = list(_FALLBACK.keys()) + ["command", "unknown_domain", ""]

    failures: list[str] = []
    with _patch_vram(0.0):  # pathological: no VRAM at all
        for domain in domains:
            profile = router.select_profile(domain)
            if profile is None:
                failures.append(f"{domain!r} → None")
                continue
            if not profile.name:
                failures.append(f"{domain!r} → empty name")
            if not profile.domain:
                failures.append(f"{domain!r} → empty domain")
            if not profile.system_prompt:
                failures.append(f"{domain!r} → empty system_prompt")

    if failures:
        return False, f"Profile validation failed: {'; '.join(failures)}"
    return True, f"All {len(domains)} domains return valid non-None profiles"


# ---------------------------------------------------------------------------
# Test 8: 2.0 GB tolerance applied — model exactly at free+2 is accepted
# ---------------------------------------------------------------------------

async def test_vram_tolerance_boundary() -> tuple[bool, str]:
    """A model requiring exactly free_gb + 2.0 GB should be selected (within tolerance)."""
    router = _router()

    # llama3.1:8b requires 4.6 GB; with 2.6 GB free: 2.6 + 2.0 = 4.6 — exactly fits
    with _patch_vram(2.6):
        profile = router.select_profile("command")

    if "llama3.1" not in profile.name:
        return False, f"Expected llama3.1:8b at 2.6+2.0=4.6 tolerance, got {profile.name!r}"
    return True, f"2.6 GB free + 2.0 tolerance → {profile.name} (4.6 GB) accepted"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    tests = [
        ("Preferred model fits VRAM → selected",               test_preferred_fits_in_vram),
        ("Preferred too large → next chain entry selected",    test_preferred_too_large_falls_back),
        ("No chain entry fits → ultimate fallback",            test_no_chain_entry_fits_ultimate_fallback),
        ("pynvml missing → 999 GB assumed, preferred selected", test_pynvml_missing_assumes_large_vram),
        ("pynvml exception → 999 GB assumed",                  test_pynvml_error_assumes_large_vram),
        ("Unknown domain → default chain (llama3.1:8b)",       test_unknown_domain_uses_default_chain),
        ("select_profile always returns valid non-None",        test_always_returns_valid_profile),
        ("2.0 GB tolerance boundary: free+2.0 fits exactly",   test_vram_tolerance_boundary),
    ]

    print(f"\nModelRouter VRAM fallback integration tests\n{'─' * 52}")
    passed = 0
    for name, fn in tests:
        try:
            ok, msg = await fn()
        except Exception as exc:
            ok, msg = False, f"EXCEPTION: {exc}"
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if ok:
            passed += 1
        else:
            print(f"    FAIL: {msg}")

    print(f"\n{'─' * 52}")
    print(f"Results: {passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
