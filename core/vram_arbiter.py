"""VramArbiter — single source of VRAM admission *policy* (orchestration gap B).

`core/vram.py` unified the VRAM *signal* (how much is free). This is the matching
single source for the *policy*: the tolerance + floor constants and the
admit decision that were previously inlined in ModelRouter.select_profile
(`profile.vram_gb <= free_gb + 2.0`) and implied by the ResourceGovernor's
evict-on-flare. Centralising them means the rule can evolve in one place and
can't drift between callers.

Admission control: `can_admit(vram_gb)` answers "will a model of this size fit
right now?" BEFORE dispatch, instead of discovering an OOM at load time. It
fails OPEN when VRAM is unmeasurable (no pynvml / no GPU) — matching the prior
behaviour where an unknown free value (999 GB sentinel) admitted everything.

Scope: this owns admission + headroom policy. The specialist pool's mutual
exclusion (only one 30B resident) and the governor's flare eviction remain their
own mechanisms; they consume the same constants here for consistency. A full
residency ledger (tracking exactly which models Ollama currently holds) is
intentionally NOT modelled — Ollama loads/evicts on its own keep_alive schedule,
so a ledger would drift from reality.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.vram import free_vram_gb, UNKNOWN_FREE_GB

log = logging.getLogger(__name__)


class VramArbiter:
    def __init__(self, tolerance_gb: float = 2.0, min_free_floor_gb: float = 2.0) -> None:
        # tolerance: slack over measured-free to absorb measurement noise and the
        #   fact that a co-resident model may sleep as the new one loads. 2.0
        #   matches the historical ModelRouter constant exactly.
        # floor: VRAM we prefer to keep free for KV cache / transient spikes.
        self._tolerance_gb = tolerance_gb
        self._min_free_floor_gb = min_free_floor_gb

    def free_gb(self) -> float:
        """Current free VRAM (shared probe); UNKNOWN_FREE_GB if unmeasurable."""
        return free_vram_gb()

    def can_admit(self, vram_gb: float, free_gb: Optional[float] = None) -> bool:
        """True if a model needing `vram_gb` fits now. Fails open when free is
        unmeasurable. Pass `free_gb` to avoid a redundant probe (and to honour a
        caller that already sampled it)."""
        if free_gb is None:
            free_gb = self.free_gb()
        if free_gb >= UNKNOWN_FREE_GB:      # signal unavailable → don't block
            return True
        return vram_gb <= free_gb + self._tolerance_gb

    def headroom_gb(self, free_gb: Optional[float] = None) -> float:
        """Free VRAM above the reserve floor (negative = below floor)."""
        if free_gb is None:
            free_gb = self.free_gb()
        if free_gb >= UNKNOWN_FREE_GB:
            return UNKNOWN_FREE_GB
        return free_gb - self._min_free_floor_gb

    def get_status(self) -> dict:
        free = self.free_gb()
        return {
            "tolerance_gb": self._tolerance_gb,
            "min_free_floor_gb": self._min_free_floor_gb,
            "free_gb": None if free >= UNKNOWN_FREE_GB else round(free, 1),
        }
