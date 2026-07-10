"""
routing_constants.py — Single source of truth for command-routing membership sets.

Rules:
  - This is a pure-constants leaf module: NO imports from core, inference, or storage.
  - All modules that need to check bypass/gate membership MUST import from here.
  - Never redefine these sets locally in another module.

Related: specs/bugfix-b1-bypass-sources/, docs/audits/2026-07-09-oop-antipattern-audit.md §B1
"""

# Sources whose commands have a fully-resolved action before reaching the
# coordinator. These skip verb de-glue (EventDispatcher.route_impl) and the
# DevAgent pre-gate. FusionEngine emits source="multimodal" for voice-click
# bypass; "touch" covers direct iPad touch actions.
_BYPASS_SOURCES: frozenset[str] = frozenset({"touch", "multimodal"})

# Sources that skip Gate 1 (the local confidence threshold check) because they
# are already routed through a trusted local-only path.
_SKIP_GATE1_SOURCES: frozenset[str] = frozenset({"voice_local"})
