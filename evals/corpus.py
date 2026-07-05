"""Eval corpus: the labelled cases and how to load/save/harvest them.

A case is a frozen statement of intent — "this utterance (in this context) should
map to this verb + these slots." Cases come from two places:
  - curated suites (evals/suites/*.jsonl), hand-written incl. edge cases;
  - harvested gold cases from agent.db, where commands.corrected_to records the
    action the USER said was correct after a misroute — the strongest label we have.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_SUITES_DIR = Path(__file__).parent / "suites"


@dataclass
class EvalCase:
    """One labelled (utterance [+context]) -> (verb, slots) expectation."""

    id: str
    suite: str
    utterance: str
    expected_verb: str
    expected_slots: dict[str, Any] = field(default_factory=dict)
    context: list[str] = field(default_factory=list)   # session_context lines
    domain: str = "command"
    source: str = "curated"                            # curated | gold | silver
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "EvalCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_suite(name_or_path: str | Path) -> list[EvalCase]:
    """Load a suite by bare name (evals/suites/<name>.jsonl) or explicit path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SUITES_DIR / f"{path.name}.jsonl"
    cases: list[EvalCase] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(EvalCase.from_dict(json.loads(line)))
    return cases


def save_suite(cases: list[EvalCase], path: str | Path) -> None:
    out = "\n".join(json.dumps(asdict(c), ensure_ascii=False) for c in cases)
    Path(path).write_text(out + "\n", encoding="utf-8")


def harvest_from_agent_db(
    db_path: str | Path,
    *,
    suite: str = "harvested",
    limit: int = 500,
    include_silver: bool = False,
) -> list[EvalCase]:
    """Build eval cases from production history.

    GOLD  — commands with a non-null ``corrected_to``: the user explicitly said
            the produced action was wrong and supplied the right one.
    SILVER— (optional) successful command-domain executions, a weaker positive
            signal (it ran ok, not necessarily that intent matched).

    Read-only; returns [] if the db or table is absent (never raises on a missing
    journal, so harvesting is safe to call opportunistically).
    """
    path = Path(db_path)
    if not path.exists():
        return []
    # Parse lazily to avoid an import cycle (scoring imports nothing heavy).
    from evals.scoring import parse_action_string

    cases: list[EvalCase] = []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT id, text, corrected_to FROM commands "
            "WHERE corrected_to IS NOT NULL AND TRIM(corrected_to) != '' "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for cid, text, corrected in rows:
            verb, slots = parse_action_string(corrected)
            if not verb:
                continue
            cases.append(EvalCase(
                id=f"gold-{cid}", suite=suite, utterance=text or "",
                expected_verb=verb, expected_slots=slots,
                source="gold", tags=["agent_db", "correction"],
            ))
        if include_silver:
            srows = conn.execute(
                "SELECT id, text, action FROM commands "
                "WHERE success = 1 AND corrected_to IS NULL AND action IS NOT NULL "
                "AND source IN ('voice','voice_local') ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            for cid, text, action in srows:
                verb, slots = parse_action_string(action)
                if not verb:
                    continue
                cases.append(EvalCase(
                    id=f"silver-{cid}", suite=suite, utterance=text or "",
                    expected_verb=verb, expected_slots=slots,
                    source="silver", tags=["agent_db", "success"],
                ))
    except sqlite3.Error:
        return cases
    finally:
        conn.close()
    return cases
