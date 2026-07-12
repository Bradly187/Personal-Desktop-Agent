"""core/flags.py registry — validation behavior + registry⇄code conformance.

The sweep test is the enforcement mechanism: it fails when a DA_* name appears
in production source but not in the registry (new flag added without
registering), and when a registry entry no longer appears anywhere in source
(dead row after a flag is removed).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.flags import REGISTRY, RESERVED, validate_da_flags

_ROOT = Path(__file__).parent.parent
_SWEPT_DIRS = ("core", "inference", "sensors", "monitoring", "adaptive",
               "desktop", "skills", "storage", "tts", "mcp_server",
               "calibration", "evals")
# Lookbehind: don't match the tail of another identifier (_CUDA_DLL_CANDIDATES).
_DA_LITERAL = re.compile(r"(?<![A-Za-z0-9_])DA_[A-Z][A-Z0-9_]*")


def _source_flag_names() -> set[str]:
    names: set[str] = set()
    files = [_ROOT / "main.py"]
    for d in _SWEPT_DIRS:
        files.extend((_ROOT / d).rglob("*.py"))
    for f in files:
        if f.name == "flags.py":       # the registry itself (has typo examples)
            continue
        names.update(_DA_LITERAL.findall(f.read_text(encoding="utf-8",
                                                     errors="replace")))
    return names


def test_every_flag_in_source_is_registered():
    unregistered = _source_flag_names() - set(REGISTRY) - RESERVED
    assert not unregistered, (
        f"DA_* names in source but not in core/flags.py REGISTRY: "
        f"{sorted(unregistered)} — register them (and add the CLAUDE.md "
        f"Feature Flags row per AGENTS.md Rule 13)")


def test_every_registered_flag_appears_in_source():
    dead = set(REGISTRY) - _source_flag_names()
    assert not dead, (
        f"Registry rows with no source occurrence (flag removed?): {sorted(dead)}")


def test_unknown_flag_is_flagged():
    problems = validate_da_flags({"DA_CLOUD_PALN": "1"})   # typo'd DA_CLOUD_PLAN
    assert len(problems) == 1 and "no code reads it" in problems[0]


def test_reserved_flag_is_not_flagged():
    assert validate_da_flags({"DA_SELF_EVOLVE": "1"}) == []


def test_bad_values_are_flagged_good_values_pass():
    assert validate_da_flags({"DA_CLOUD_PLAN_TIMEOUT_S": "sixty"})  # int
    assert validate_da_flags({"DA_CRITIC_FLOOR": "high"})           # float
    assert validate_da_flags({"DA_CRITIC": "maybe"})                # bool
    assert validate_da_flags({"DA_METRIC_THRESHOLDS": "{not json"})
    assert validate_da_flags({
        "DA_CLOUD_PLAN_TIMEOUT_S": "60",
        "DA_CRITIC_FLOOR": "0.75",
        "DA_CRITIC": "true",
        "DA_TRACE": "0",
        "DA_METRIC_THRESHOLDS": '[{"metric": "x", "op": ">", "value": 1}]',
        "DA_CRITIC_DOMAIN": "anything-goes",
    }) == []


def test_defaults_match_key_call_sites():
    """Spot-pin the defaults most likely to drift (doc table said ON while the
    code said OFF for two of these once already).
    DA_REPO_CONTEXT was intentionally flipped to ON on
    2026-07-09 (Brad approved B3 Path B). Update these pins if you consciously
    change a default; never update them to silence a surprise."""
    assert REGISTRY["DA_REPO_CONTEXT"].default == "1"  # ON — Brad approved 2026-07-09
    assert REGISTRY["DA_CRITIC"].default == "1"
    assert REGISTRY["DA_TESTER"].default == "1"
    assert REGISTRY["DA_CLOUD_PLAN"].default == "0"
    assert REGISTRY["DA_SNAP_RADIUS_PX"].default == "300"
