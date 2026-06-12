"""core/schedule_parser.py — deterministic NL grammar for voice scheduling (N+2).

Turns a spoken phrase into a time-trigger (scheduled goal), an event-rule, or a
management command — no LLM, a small predictable grammar:

  time triggers:
    "every morning|day|night|evening [at H[:MM]] <goal>"  → daily recurrence
    "every <N> minutes|hours <goal>"                       → interval recurrence
    "in <N> minutes|hours <goal>"                          → one-shot
    "remind me at H[:MM] to <goal>"                        → one-shot today/tomorrow
  event rules:
    "when [an] email from <X> arrives, tell|notify|alert me"
  management:
    "what are my reminders" / "list my reminders"
    "cancel the morning brief" / "cancel all reminders"

`parse(text, now)` returns a tagged dict (or None); `is_schedule_phrase(text)` is
the cheap pre-check the coordinator's system-control gate uses.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from core.proactive_scheduler import next_occurrence

_DAILY_DEFAULTS = {"morning": "08:00", "day": "08:00", "night": "21:00", "evening": "18:00"}


def _parse_hm(s: str) -> tuple[int, int]:
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.strip().lower())
    if not m:
        return 8, 0
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    return hh % 24, mm % 60


def _one_shot_at(now: float, hh: int, mm: int) -> float:
    cand = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand = cand + timedelta(days=1)
    return cand.timestamp()


def _clean_goal(goal: str) -> str:
    return goal.strip().rstrip(".!?").strip()


def is_schedule_phrase(text: str) -> bool:
    """Cheap pre-check for the coordinator's system-control gate — conservative
    (specific multi-word prefixes) so it never grabs an ordinary ML/QC query."""
    tl = text.strip().lower()
    if re.search(r"\b(what are|list|show) my reminders\b", tl):
        return True
    if tl.startswith("cancel ") and ("reminder" in tl or "brief" in tl or "schedule" in tl):
        return True
    return bool(re.match(
        r"(every (morning|day|night|evening|\d+)|in \d+ (minute|hour)|"
        r"remind me at|when (an? )?email from)", tl))


def parse(text: str, now: float) -> Optional[dict]:
    t = text.strip().rstrip(".!?").strip()
    tl = t.lower()

    # ── Management ──────────────────────────────────────────────────────────
    if re.search(r"\b(what are|list|show) my reminders\b", tl):
        return {"kind": "list"}
    if tl.startswith("cancel ") and ("reminder" in tl or "brief" in tl or "schedule" in tl):
        q = re.sub(r"^cancel (the |my |all )?", "", tl)
        q = re.sub(r"\b(reminder|reminders|schedule)\b", "", q).strip()
        return {"kind": "cancel", "query": q}

    # ── Event rule: "when [an] email from X arrives, tell me" ───────────────
    m = re.match(r"when (?:an? )?email from (.+?) arrives?,?\s*(?:tell|notify|alert|let) me", tl)
    if m:
        who = m.group(1).strip()
        return {
            "kind": "event_rule",
            "name": f"email from {who}",
            "topic_pattern": "email.arrived",
            "predicate": json.dumps([{"path": "from", "op": "contains", "value": who}]),
            "goal_template": "Email from {from}: {subject}",
            "action_kind": "notify",
            "spoken": f"Okay, I'll tell you when an email from {who} arrives.",
        }

    # ── "every morning|day|night|evening [at H] <goal>" → daily ─────────────
    m = re.match(r"every (morning|day|night|evening)(?: at ([0-9: apm]+?))? (.+)", tl)
    if m:
        period, at, goal = m.group(1), m.group(2), m.group(3)
        hh, mm = _parse_hm(at) if at else tuple(int(x) for x in _DAILY_DEFAULTS[period].split(":"))
        rec = json.dumps({"kind": "daily", "at": f"{hh:02d}:{mm:02d}"})
        return {"kind": "schedule", "goal": _clean_goal(goal),
                "execute_at": next_occurrence(json.loads(rec), now), "recurrence": rec,
                "spoken": f"Okay, every {period} at {hh:02d}:{mm:02d}."}

    # ── "every N minutes|hours <goal>" → interval ───────────────────────────
    m = re.match(r"every (\d+) (minute|minutes|hour|hours) (.+)", tl)
    if m:
        n, unit, goal = int(m.group(1)), m.group(2), m.group(3)
        secs = n * (60 if unit.startswith("minute") else 3600)
        return {"kind": "schedule", "goal": _clean_goal(goal), "execute_at": now + secs,
                "recurrence": json.dumps({"kind": "interval", "every_s": secs}),
                "spoken": f"Okay, every {n} {unit}."}

    # ── "in N minutes|hours <goal>" → one-shot ──────────────────────────────
    m = re.match(r"in (\d+) (minute|minutes|hour|hours) (.+)", tl)
    if m:
        n, unit, goal = int(m.group(1)), m.group(2), m.group(3)
        secs = n * (60 if unit.startswith("minute") else 3600)
        return {"kind": "schedule", "goal": _clean_goal(goal), "execute_at": now + secs,
                "recurrence": None, "spoken": f"Okay, in {n} {unit}."}

    # ── "remind me at H to <goal>" → one-shot today/tomorrow ────────────────
    m = re.match(r"remind me at ([0-9: apm]+?) to (.+)", tl)
    if m:
        hh, mm = _parse_hm(m.group(1))
        return {"kind": "schedule", "goal": _clean_goal(m.group(2)),
                "execute_at": _one_shot_at(now, hh, mm), "recurrence": None,
                "spoken": f"Okay, I'll remind you at {hh:02d}:{mm:02d}."}

    return None
