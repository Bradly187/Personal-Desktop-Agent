"""Static token-budget gate for the always-loaded skill metadata (MODEL-FREE).

The whitepaper's fourth eval condition is *token budget*: the metadata that loads on
EVERY turn (the skill router's keyword surface + the planner's "available skills"
block) must stay small, or it degrades unrelated turns via context rot. The paper's
own math is "~N skills x ~50 tokens of always-loaded metadata."

This module measures exactly that always-on surface from the shipped manifests —
per skill: `skill_id` + `display_name` + every intent name + every intent keyword —
and asserts the total stays under a bound. It is deterministic and needs no model,
matching the rest of this repo's model-free eval gates. A bloated manifest (too many
keywords, an over-long display name) shows up immediately in the per-skill breakdown.

This is the pragmatic, CI-friendly stand-in for the paper's co-loaded-model budget
eval: it can't measure attention degradation, but it bounds the cause we control.

    python -m evals.token_budget                 # human breakdown, exit nonzero if over
    python -m evals.token_budget --max-tokens 4000
    python -m evals.token_budget --json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

_MANIFEST_DIR = Path(__file__).parent.parent / "skills" / "manifests"

# Always-loaded metadata budget. The paper frames ~50 tokens/skill as the cost; with
# headroom for this project's growing library we cap the WHOLE surface at 5000 tokens
# (~100 skills' worth). Tightened deliberately — raise only with a reason.
DEFAULT_MAX_TOKENS = 5000


def _est_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token, the common GPT-family
    rule of thumb). No tokenizer dependency — we only need a stable upper-ish bound."""
    return math.ceil(len(text) / 4) if text else 0


def _skill_metadata_text(manifest: dict) -> str:
    """The always-loaded routing surface for one skill: id + display name + every
    intent name and keyword (exactly what gets registered for routing and listed in
    the planner's available-skills block)."""
    parts: list[str] = [
        str(manifest.get("skill_id", "")),
        str(manifest.get("display_name", "")),
    ]
    for iname, intent in (manifest.get("intents") or {}).items():
        parts.append(str(iname))
        parts.extend(str(kw) for kw in (intent.get("keywords") or []))
    return " ".join(p for p in parts if p)


def measure(manifest_dir: "str | Path | None" = None) -> dict:
    """Return {'total_tokens', 'per_skill': {skill_id: tokens}, 'n_skills'}.

    Loads EVERY manifest regardless of `enabled` — the metadata cost is incurred by
    the routing surface whether or not a skill's server is started, and we want the
    bound to cover the full installed library."""
    mdir = Path(manifest_dir) if manifest_dir else _MANIFEST_DIR
    per_skill: dict[str, int] = {}
    if mdir.exists():
        for f in sorted(mdir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = d.get("skill_id") or f.stem
            per_skill[sid] = _est_tokens(_skill_metadata_text(d))
    return {
        "total_tokens": sum(per_skill.values()),
        "per_skill": per_skill,
        "n_skills": len(per_skill),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                    help=f"budget for the always-loaded skill metadata (default {DEFAULT_MAX_TOKENS})")
    ap.add_argument("--manifest-dir", default=None)
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    rep = measure(args.manifest_dir)
    total = rep["total_tokens"]
    over = total > args.max_tokens

    if args.json:
        print(json.dumps({**rep, "max_tokens": args.max_tokens, "over_budget": over},
                         indent=2))
    else:
        print(f"always-loaded skill metadata: ~{total} tokens across "
              f"{rep['n_skills']} skills  (budget {args.max_tokens})")
        for sid, tok in sorted(rep["per_skill"].items(), key=lambda kv: -kv[1]):
            print(f"  {sid:<14} ~{tok:>4} tok")
        verdict = "OVER BUDGET" if over else "OK"
        print(f"\n{verdict}: ~{total} / {args.max_tokens} tokens "
              f"({total / args.max_tokens:.0%})")

    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
