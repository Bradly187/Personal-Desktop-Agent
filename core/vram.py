"""Single source for the GPU VRAM signal (orchestration gap #3).

Several components independently queried pynvml for free/used VRAM with
near-identical code (ModelRouter._free_vram_gb, benchmark_models._vram_used_gb,
ResourceGovernor status). This module is the ONE probe they share so the signal
cannot drift between callers.

Scope note: this owns the VRAM *signal*, not VRAM *policy*. Admission, eviction
(ResourceGovernor keep_alive), specialist mutual-exclusion (VLLMSpecialistPool),
and model selection (ModelRouter) still each apply their own tuned policy. Folding
those into a single arbiter object is a deliberate, larger follow-up — kept out
of scope here to avoid destabilising the carefully-measured VRAM math in the pool
and router.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Fail-open sentinel: when VRAM can't be measured (no pynvml / no GPU), callers
# treat capacity as unlimited rather than blocking inference. Matches the prior
# ModelRouter._free_vram_gb behaviour so this consolidation is a no-op there.
UNKNOWN_FREE_GB: float = 999.0


def _mem_info():
    import pynvml as nvml
    nvml.nvmlInit()
    try:
        handle = nvml.nvmlDeviceGetHandleByIndex(0)
        return nvml.nvmlDeviceGetMemoryInfo(handle)
    finally:
        nvml.nvmlShutdown()


def free_vram_gb() -> float:
    """Free VRAM on GPU 0 in GiB; UNKNOWN_FREE_GB (fail-open) if unmeasurable."""
    try:
        return _mem_info().free / (1024 ** 3)
    except Exception as exc:
        log.debug("vram.free_vram_gb: pynvml unavailable (%s) — fail-open", exc)
        return UNKNOWN_FREE_GB


def used_vram_gb() -> Optional[float]:
    """Used VRAM on GPU 0 in GiB, or None if unmeasurable."""
    try:
        return _mem_info().used / (1024 ** 3)
    except Exception:
        return None
