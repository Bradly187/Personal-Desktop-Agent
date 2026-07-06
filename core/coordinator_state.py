from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

class CoordinatorState:
    """Encapsulates mutable state for the HybridCoordinator orchestrator."""
    def __init__(self):
        self.pending_clarification: Optional[str] = None
        self.active_clarify_surface_id: Optional[str] = None
        self.recent_open_targets: list[str] = []
        self.last_executed_action: str = ""
        self.last_command_id: int = -1
        self.session_intent: Optional[str] = None
        self.drift_streak: int = 0
        self.drift_warned: bool = False
        self.recent_dev_commands: list[str] = []
        self.dev_agent_active: bool = False

    def get_pending_clarification(self) -> Optional[str]:
        return self.pending_clarification
    
    def set_pending_clarification(self, val: Optional[str]) -> None:
        self.pending_clarification = val

    def get_active_clarify_surface_id(self) -> Optional[str]:
        return self.active_clarify_surface_id
        
    def set_active_clarify_surface_id(self, val: Optional[str]) -> None:
        self.active_clarify_surface_id = val

    def get_recent_open_targets(self) -> list[str]:
        return self.recent_open_targets

    def set_recent_open_targets(self, targets: list[str]) -> None:
        self.recent_open_targets = targets
