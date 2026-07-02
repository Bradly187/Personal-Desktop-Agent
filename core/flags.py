"""core/flags.py — single registry of every DA_* feature flag, with startup
validation.

Why this exists: flags are read with ``os.environ.get`` at 25+ call sites, so
a typo'd variable name (``DA_CLOUD_PALN=1``) or a malformed value silently does
nothing — the operator believes a behavior is on when it isn't. This module
does NOT change how any call site reads its flag (zero behavioral risk); it is
a declarative mirror that main.py checks once at startup to:

  * WARN on any ``DA_*`` variable set in the environment that no code reads
  * WARN on a set flag whose value won't parse as the declared kind
    (call sites keep their own local fallbacks — the warning is the point)
  * INFO-log every non-default flag so a session's active configuration is
    visible in the log head

Adding a flag? Register it here in the same change that reads it —
``tests/test_flags_registry.py`` sweeps the source tree for ``DA_*`` literals
and fails if a read site and this registry disagree (either direction), and
the CLAUDE.md Feature Flags table row is still required per AGENTS.md Rule 13.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Mapping

log = logging.getLogger(__name__)

# Recognized spellings for boolean flags across the codebase's three idioms
# (`in ("1","true","on","yes")`, `!= "0"`, `not in ("0","false","off","no","")`).
_BOOL_TOKENS = {"", "0", "1", "true", "false", "on", "off", "yes", "no"}


@dataclass(frozen=True)
class FlagSpec:
    name: str
    kind: str          # "bool" | "int" | "float" | "str" | "json"
    default: str       # the call site's default, as the env string would read
    summary: str


_S = FlagSpec

REGISTRY: dict[str, FlagSpec] = {f.name: f for f in (
    # ── dev-agent loop (inference/dev_agent.py) ─────────────────────────────
    _S("DA_PLAN_REPAIR", "bool", "1", "re-prompt planner on unparseable plan"),
    _S("DA_PLAN_REPAIR_MAX", "int", "1", "max plan auto-repair attempts"),
    _S("DA_CRITIC", "bool", "1", "review diffs pre-disk-commit"),
    _S("DA_CRITIC_FLOOR", "float", "0.6", "critic confidence floor to escalate"),
    _S("DA_CRITIC_MAX_REVISIONS", "int", "1", "max replans on critic REVISE"),
    _S("DA_CRITIC_DOMAIN", "str", "plan", "model domain the critic runs on"),
    _S("DA_TESTER", "bool", "1", "auto-pytest after .py writes"),
    _S("DA_TESTER_DOMAIN", "str", "code", "model domain for test generation"),
    _S("DA_REPO_CONTEXT", "bool", "0", "inject stable repo facts ahead of RAG"),
    _S("DA_DELEGATE", "bool", "0", "planner [DELEGATE q] read-only sub-agent"),
    _S("DA_DELEGATE_MAX_DEPTH", "int", "1", "delegation nesting bound"),
    _S("DA_DELEGATE_MAX_STEPS", "int", "4", "steps per delegation"),
    _S("DA_DELEGATE_FINDING_CHARS", "int", "1200", "delegation finding truncation"),
    _S("DA_MATH_CAS_VERIFY", "bool", "1", "sympy CAS check on math answers"),
    _S("DA_SAGA_ANNOUNCE", "bool", "1", "TTS summary after saga rollback"),
    _S("DA_SAGA_GIT_BACKEND", "bool", "0", "git-blob saga snapshots"),
    # ── trajectory / memory (inference/) ────────────────────────────────────
    _S("DA_AUTO_ADJUDICATE", "bool", "0", "auto-dismiss hallucinated escalations"),
    _S("DA_TRAJECTORY_REDUCE", "bool", "1", "compact trajectory tokens"),
    _S("DA_TRAJECTORY_DEDUP", "bool", "1", "drop superseded duplicate reads"),
    _S("DA_RESUME_MEMORY", "bool", "1", "seed crash-resumed plans"),
    _S("DA_SESSION_MEMORY", "bool", "0", "cross-session plan seeding"),
    # ── cloud routing (inference/, core/cloud_backend.py) ───────────────────
    _S("DA_CLOUD_PLAN", "bool", "0", "route plan domain to Bedrock"),
    _S("DA_CLOUD_PLAN_MODEL", "str", "", "override cloud plan model"),
    _S("DA_CLOUD_PLAN_TIMEOUT_S", "int", "60", "cloud plan call timeout"),
    _S("DA_CLOUD_DEV_MODEL", "str", "", "override cloud dev model"),
    _S("DA_BEDROCK_REGION", "str", "", "Bedrock region override"),
    _S("DA_BEDROCK_PROFILE_PREFIX", "str", "us", "Bedrock inference-profile prefix"),
    _S("DA_BEDROCK_PRICES", "json", "", "per-model (in,out) $/Mtok overrides"),
    _S("DA_BEDROCK_PRICE_MULT", "float", "1.0", "cost-ledger price multiplier"),
    # ── inference runtime (inference/local_inference.py, sandbox.py) ────────
    _S("DA_OLLAMA_TIMEOUT_S", "float", "45", "Ollama hang guard"),
    _S("DA_SANDBOX", "bool", "1", "bwrap/firejail RUN_TERMINAL sandbox"),
    # ── routing / classification (core/) ────────────────────────────────────
    _S("DA_DOMAIN_LEARN", "bool", "0", "dynamic domain-keyword overlay"),
    _S("DA_PLAN_WORD_TRIGGER", "bool", "0", "'plan …' word forces plan domain"),
    _S("DA_A2UI_CLICK_TARGETS", "bool", "0", "A2UI click-target surfaces"),
    _S("DA_SNAP_RADIUS_PX", "int", "300", "flick-to-snap search radius"),
    # ── sensors / bridge / desktop (sensors/, core/, main.py) ───────────────
    _S("DA_NEURAL_VAD", "bool", "1", "neural VAD in WhisperStream"),
    _S("DA_BINARY_AUDIO", "bool", "1", "raw binary WS audio frames"),
    _S("DA_CURSOR_GRAVITY", "bool", "1", "magnetic cursor target cache"),
    _S("DA_COMMAND_WARMUP", "bool", "1", "warm command model at boot"),
    # ── observability (monitoring/) ─────────────────────────────────────────
    _S("DA_TRACE", "bool", "1", "per-command trace spans"),
    _S("DA_METRIC_THRESHOLDS", "json", "", "metric-watcher threshold overrides"),
    # ── skill servers (skills/servers/*, separate processes, same env) ──────
    _S("DA_WEATHER_CONFIG", "str", "", "weather skill config path"),
    _S("DA_FILES_ROOTS", "str", "", "files skill allowed roots"),
    _S("DA_PAIN_DB", "str", "", "pain-journal DB path"),
    _S("DA_NOTES_ROOT", "str", "", "notes skill root dir"),
    _S("DA_DIAGRAMS_DIR", "str", "", "diagrams output dir"),
    _S("DA_PAPERS_DIR", "str", "", "arxiv papers dir"),
)}

# Known names with no live read site (docs/schema comments only, or consumed by
# an external launcher). Setting one is NOT flagged as unknown, but they carry
# no registry contract either.
RESERVED: frozenset[str] = frozenset({
    "DA_SELF_EVOLVE",     # storage/db.py schema comment; promotion gate is manual
})


def _value_ok(spec: FlagSpec, value: str) -> bool:
    v = value.strip()
    if spec.kind == "bool":
        return v.lower() in _BOOL_TOKENS
    if spec.kind == "int":
        try:
            int(v)
        except ValueError:
            return False
        return True
    if spec.kind == "float":
        try:
            float(v)
        except ValueError:
            return False
        return True
    if spec.kind == "json":
        if not v:
            return True
        try:
            json.loads(v)
        except ValueError:
            return False
        return True
    return True  # "str": anything goes


def validate_da_flags(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return a list of human-readable problems with the DA_* environment.

    Pure function (no logging, no side effects) so tests can assert on it;
    ``report_da_flags`` is the logging wrapper main.py calls.
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []
    for name in sorted(n for n in env if n.startswith("DA_")):
        spec = REGISTRY.get(name)
        if spec is None:
            if name not in RESERVED:
                problems.append(
                    f"{name} is set but no code reads it (typo?) — "
                    f"known flags: see core/flags.py")
            continue
        if not _value_ok(spec, env[name]):
            problems.append(
                f"{name}={env[name]!r} won't parse as {spec.kind} "
                f"(default {spec.default!r} applies at the call site)")
    return problems


def report_da_flags(environ: Mapping[str, str] | None = None) -> None:
    """Log validation problems (WARNING) and non-default flags (INFO).

    Called once from main.py after logging is configured. Never raises:
    a bad flag environment degrades to warnings, not a refused boot.
    """
    env = os.environ if environ is None else environ
    for problem in validate_da_flags(env):
        log.warning("DA_* flag check: %s", problem)
    active = {
        name: env[name] for name, spec in sorted(REGISTRY.items())
        if name in env and env[name].strip() != spec.default
    }
    if active:
        log.info("DA_* flags active (non-default): %s",
                 ", ".join(f"{k}={v}" for k, v in active.items()))
    else:
        log.info("DA_* flags: all at defaults")
