"""Generate synthetic fine-tuning examples to balance under-represented domains.

Real agent use is almost entirely the `command` domain (CLICK/SCROLL/TYPE).
Code, math, vision, and plan examples will be sparse after 2-4 weeks of passive
collection. This script uses claude-haiku-4-5 to generate paraphrases and novel
examples from hand-crafted seed pairs, writing output in the same Alpaca-format
JSONL schema as extract_finetuning_dataset.py.

Usage:
    python scripts/generate_synthetic_data.py
    python scripts/generate_synthetic_data.py --domain code --count 100
    python scripts/generate_synthetic_data.py --domain all  --count 50

Output:
    finetuning_synthetic.jsonl  (append mode — safe to run multiple times)

Pass --include-synthetic to extract_finetuning_dataset.py to merge this file
into the final training set.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_DEFAULT_OUT = Path(__file__).parent.parent / "finetuning_synthetic.jsonl"

_SYSTEM_PROMPT = (
    "You are a desktop control assistant. Convert the user's natural-language "
    "request into exactly ONE action from the following vocabulary.\n\n"
    "CLICK <target>  SCROLL <direction> [<amount>]  TYPE <text>  OPEN <app-or-file>\n"
    "CLOSE [<target>]  HOTKEY <key1> [<key2>...]  DICTATE <text>\n"
    "CLARIFY <question>  SCREENSHOT\n\n"
    "Reply with ONLY the action string. Do not explain or comment."
)

# ---------------------------------------------------------------------------
# Seed pairs per domain — (user utterance, correct action)
# Extend these as you learn which phrasings the model struggles with.
# ---------------------------------------------------------------------------

_SEEDS: dict[str, list[tuple[str, str]]] = {
    "command": [
        ("click the save button",               "CLICK save button"),
        ("scroll down",                          "SCROLL down"),
        ("scroll up three times",               "SCROLL up 3"),
        ("open chrome",                          "OPEN Chrome"),
        ("close this window",                   "CLOSE"),
        ("take a screenshot",                   "SCREENSHOT"),
        ("press control s",                     "HOTKEY ctrl s"),
        ("type hello world",                    "TYPE hello world"),
        ("paste the text",                      "HOTKEY ctrl v"),
        ("undo that",                           "HOTKEY ctrl z"),
        ("open the terminal",                   "OPEN Terminal"),
        ("click ok",                            "CLICK ok"),
        ("maximize the window",                 "HOTKEY super up"),
        ("go to the next tab",                  "HOTKEY ctrl tab"),
        ("close the tab",                       "HOTKEY ctrl w"),
        ("zoom in",                             "HOTKEY ctrl plus"),
        ("open file explorer",                  "OPEN File Explorer"),
        ("copy that",                           "HOTKEY ctrl c"),
        ("select all",                          "HOTKEY ctrl a"),
        ("open settings",                       "OPEN Settings"),
    ],
    "code": [
        ("open vs code",                         "OPEN VS Code"),
        ("click the run button in the editor",   "CLICK run button"),
        ("open the terminal in vscode",          "HOTKEY ctrl backtick"),
        ("go to definition",                     "HOTKEY f12"),
        ("rename this variable",                 "HOTKEY f2"),
        ("format the document",                  "HOTKEY shift alt f"),
        ("open the command palette",             "HOTKEY ctrl shift p"),
        ("split the editor",                     "HOTKEY ctrl backslash"),
        ("close the sidebar",                    "HOTKEY ctrl b"),
        ("search in files",                      "HOTKEY ctrl shift f"),
    ],
    "voice": [
        ("hey agent click the submit button",    "CLICK submit button"),
        ("agent scroll down please",             "SCROLL down"),
        ("open my browser",                      "OPEN Chrome"),
        ("agent take a screenshot",              "SCREENSHOT"),
        ("type my name Brad",                    "TYPE Brad"),
        ("hey close this",                       "CLOSE"),
        ("agent press enter",                    "HOTKEY enter"),
        ("search for something",                 "CLARIFY What would you like to search for?"),
        ("do the thing",                         "CLARIFY What would you like me to do?"),
        ("go there",                             "CLARIFY Where would you like me to navigate?"),
    ],
    "accessibility": [
        ("click the big red button",             "CLICK big red button"),
        ("slowly scroll down",                   "SCROLL down 1"),
        ("open something to read",               "CLARIFY What would you like to open?"),
        ("make the text bigger",                 "HOTKEY ctrl plus"),
        ("turn on high contrast",                "OPEN Settings"),
        ("read the screen",                      "READ_SCREEN"),
        ("dictate this paragraph",               "DICTATE "),
        ("click anywhere on the screen",         "CLARIFY Where would you like me to click?"),
    ],
}

_GENERATION_PROMPT = """\
You are generating training data for a fine-tuned desktop control assistant.

Given a seed (user utterance → correct action), generate {count} new examples
that test the SAME action but with different phrasings, vocabulary, or context.

Seed: "{utterance}" → "{action}"

Rules:
- Each new example must map to the SAME action string as the seed (or a very close variant).
- Vary phrasing, formality, word order, synonyms.
- Include realistic voice-input patterns (contractions, trailing "please", false starts).
- Do not introduce new action types not represented in the seed.
- Output ONLY a JSON array of objects with keys "input" and "output".
- Example output format:
  [
    {{"input": "hit the save icon", "output": "CLICK save button"}},
    {{"input": "can you save this for me", "output": "CLICK save button"}}
  ]

Generate {count} examples now:"""


def _call_claude(prompt: str, model: str = "claude-haiku-4-5") -> str:
    try:
        import anthropic  # noqa: F401  (imported by make_client)
    except ImportError:
        print("ERROR: anthropic not installed — run: pip install anthropic", file=sys.stderr)
        sys.exit(1)
    # Claude is accessed exclusively through Amazon Bedrock — build the client and
    # map the model id via the shared backend seam (AWS_BEARER_TOKEN_BEDROCK).
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.cloud_backend import resolve_backend
    try:
        backend = resolve_backend()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    client = backend.make_client()
    response = client.messages.create(
        model=backend.map_model(model),
        max_tokens=2048,
        temperature=0.8,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _generate_for_seed(
    utterance: str,
    action: str,
    count: int,
    domain: str,
) -> list[dict]:
    prompt = _GENERATION_PROMPT.format(utterance=utterance, action=action, count=count)
    raw = _call_claude(prompt)
    # Extract the JSON array from the response
    try:
        # Try direct parse
        pairs = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract the array from surrounding prose
        import re
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not m:
            print(f"  WARNING: could not parse response for seed '{utterance}'", file=sys.stderr)
            return []
        try:
            pairs = json.loads(m.group(0))
        except json.JSONDecodeError:
            print(f"  WARNING: JSON parse failed for seed '{utterance}'", file=sys.stderr)
            return []

    examples = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        inp = str(p.get("input", "")).strip()
        out = str(p.get("output", "")).strip()
        if not inp or not out:
            continue
        examples.append({
            "instruction": _SYSTEM_PROMPT,
            "input": inp,
            "output": out,
            "metadata": {
                "source":             "synthetic",
                "whisper_logprob":    None,
                "gesture_confidence": None,
                "success":            True,
                "model":              "claude-haiku-4-5",
                "domain":             domain,
                "latency_ms":         None,
                "tokens_in":          None,
                "tokens_out":         None,
                "synthetic":          True,
            },
        })
    return examples


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--domain", default="all",
                   help="Domain to generate for: command|code|voice|accessibility|all")
    p.add_argument("--count", type=int, default=5,
                   help="Paraphrases to generate per seed pair (default: 5)")
    p.add_argument("--out", default=str(_DEFAULT_OUT),
                   help="Output JSONL path (append mode)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be generated without calling the API")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    out_path = Path(args.out)

    domains = list(_SEEDS.keys()) if args.domain == "all" else [args.domain]
    for d in domains:
        if d not in _SEEDS:
            print(f"ERROR: unknown domain '{d}'. Choose from: {list(_SEEDS.keys())}", file=sys.stderr)
            sys.exit(1)

    total_seeds = sum(len(_SEEDS[d]) for d in domains)
    print(f"Generating {args.count} paraphrases × {total_seeds} seeds "
          f"across domains: {domains}")
    print(f"Output → {out_path} (append mode)")
    if args.dry_run:
        print("DRY RUN — no API calls made")
        for d in domains:
            for utt, act in _SEEDS[d]:
                print(f"  [{d}] '{utt}' → '{act}'  (+{args.count} paraphrases)")
        return

    total_written = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for domain in domains:
            seeds = _SEEDS[domain]
            print(f"\n[{domain}] {len(seeds)} seeds …")
            for utterance, action in seeds:
                print(f"  '{utterance}' → '{action}' …", end=" ", flush=True)
                examples = _generate_for_seed(utterance, action, args.count, domain)
                for ex in examples:
                    f.write(json.dumps(ex, ensure_ascii=False) + "\n")
                total_written += len(examples)
                print(f"{len(examples)} examples")
                time.sleep(0.2)  # stay within rate limit

    print(f"\nWrote {total_written} synthetic examples → {out_path}")


if __name__ == "__main__":
    main()
