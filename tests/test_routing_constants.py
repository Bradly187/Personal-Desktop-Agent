"""
tests/test_routing_constants.py — B1 regression guard

Ensures _BYPASS_SOURCES is defined exactly once, in the leaf module, with the
correct values, and that both consumers import from it rather than redefining it.
"""


from core.routing_constants import _BYPASS_SOURCES, _SKIP_GATE1_SOURCES


# ---------------------------------------------------------------------------
# R1.4 — correct membership
# ---------------------------------------------------------------------------

def test_multimodal_in_bypass_sources():
    """FusionEngine emits source='multimodal'; it MUST be in _BYPASS_SOURCES."""
    assert "multimodal" in _BYPASS_SOURCES


def test_touch_in_bypass_sources():
    """Direct iPad touch actions use source='touch'."""
    assert "touch" in _BYPASS_SOURCES


def test_multi_not_in_bypass_sources():
    """'multi' was the old stale value in event_dispatcher.py — must not appear."""
    assert "multi" not in _BYPASS_SOURCES


def test_bypass_sources_is_frozenset():
    """frozenset prevents accidental mutation by importing callers."""
    assert isinstance(_BYPASS_SOURCES, frozenset)


def test_skip_gate1_sources_contains_voice_local():
    assert "voice_local" in _SKIP_GATE1_SOURCES


# ---------------------------------------------------------------------------
# R1.1 / R1.3 — single definition; both consumers import from the leaf
# ---------------------------------------------------------------------------

def test_event_dispatcher_imports_bypass_from_leaf():
    """event_dispatcher must NOT define its own _BYPASS_SOURCES."""
    import core.event_dispatcher as ed
    # The module-level name must resolve to the same object as the leaf's
    assert ed._BYPASS_SOURCES is _BYPASS_SOURCES


def test_hybrid_coordinator_imports_bypass_from_leaf():
    """hybrid_coordinator must NOT define its own _BYPASS_SOURCES."""
    import core.hybrid_coordinator as hc
    assert hc._BYPASS_SOURCES is _BYPASS_SOURCES


def test_hybrid_coordinator_imports_skip_gate1_from_leaf():
    import core.hybrid_coordinator as hc
    assert hc._SKIP_GATE1_SOURCES is _SKIP_GATE1_SOURCES


def test_no_local_bypass_definition_in_event_dispatcher(tmp_path):
    """Grep-level guard: the literal string '\"multi\"' must not appear as a
    _BYPASS_SOURCES value in event_dispatcher.py."""
    src = (
        __file__
        .replace("tests\\test_routing_constants.py", "core\\event_dispatcher.py")
        .replace("tests/test_routing_constants.py", "core/event_dispatcher.py")
    )
    import pathlib
    text = pathlib.Path(src).read_text(encoding="utf-8")
    # The old stale tuple ("touch", "multi") must be gone
    assert '"multi"' not in text or "_BYPASS_SOURCES" not in text.split('"multi"')[0].split("\n")[-1], \
        "event_dispatcher.py still contains a local _BYPASS_SOURCES with 'multi'"
