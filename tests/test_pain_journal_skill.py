"""pain_journal skill — local voice pain/medication journal.

Pure helpers are tested against a tmp SQLite db with an injected clock, mirroring
the notes/weather skill tests. A manifest-sanity test guards the wiring (every
intent points at an allowed tool; no accidental send_tools on a local-only skill).

Run: python -m pytest tests/test_pain_journal_skill.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

sys.path.insert(0, str(Path(__file__).parent.parent))

from skills.servers import pain_journal_server as pj

T0 = 1_750_000_000.0  # fixed clock for deterministic tests
DAY = 86_400.0


@pytest.fixture
def db(tmp_path):
    return tmp_path / "pain.db"


# --------------------------------------------------------------------------- #
# log_symptom
# --------------------------------------------------------------------------- #

def test_log_symptom_creates_db_and_row(db):
    out = pj._log_symptom(db, "hands", 6, now=T0)
    assert "hands" in out and "6 out of 10" in out
    assert db.exists()
    assert "1 entr" in pj._recent_symptoms(db, days=7, now=T0)


def test_log_symptom_includes_note(db):
    out = pj._log_symptom(db, "knees", 4, note="after the walk", now=T0)
    assert "after the walk" in out
    recent = pj._recent_symptoms(db, days=7, now=T0)
    assert "after the walk" in recent


def test_log_symptom_empty_area_is_honest(db):
    out = pj._log_symptom(db, "  ", 5, now=T0)
    assert "didn't catch" in out.lower()
    assert not db.exists()  # nothing written


@pytest.mark.parametrize("raw,expected", [(99, 10), (-3, 0), (7, 7)])
def test_severity_is_clamped(db, raw, expected):
    pj._log_symptom(db, "wrist", raw, now=T0)
    # _recent_symptoms renders severity as "<sev>/10".
    assert f"{expected}/10" in pj._recent_symptoms(db, days=1, now=T0)


def test_non_integer_severity_defaults_zero(db):
    out = pj._log_symptom(db, "elbow", "lots", now=T0)  # type: ignore[arg-type]
    assert "0 out of 10" in out


def test_area_is_normalised_lowercase(db):
    pj._log_symptom(db, "Hands", 5, now=T0)
    assert "hands" in pj._recent_symptoms(db, days=1, now=T0).lower()


# --------------------------------------------------------------------------- #
# log_med
# --------------------------------------------------------------------------- #

def test_log_med(db):
    out = pj._log_med(db, "methotrexate", "15mg", now=T0)
    assert "methotrexate" in out and "15mg" in out


def test_log_med_empty_name_honest(db):
    assert "didn't catch" in pj._log_med(db, "", now=T0).lower()


# --------------------------------------------------------------------------- #
# recent_symptoms
# --------------------------------------------------------------------------- #

def test_recent_no_db_is_honest(db):
    assert "no pain logged" in pj._recent_symptoms(db, days=7, now=T0).lower()


def test_recent_excludes_old_entries(db):
    pj._log_symptom(db, "hands", 6, now=T0 - 10 * DAY)  # outside 7-day window
    pj._log_symptom(db, "knees", 4, now=T0)             # inside
    out = pj._recent_symptoms(db, days=7, now=T0)
    assert "knees" in out
    assert "hands" not in out


def test_recent_newest_first(db):
    pj._log_symptom(db, "early", 3, now=T0 - 2 * DAY)
    pj._log_symptom(db, "late", 8, now=T0 - 1 * DAY)
    out = pj._recent_symptoms(db, days=7, now=T0)
    assert out.index("late") < out.index("early")


# --------------------------------------------------------------------------- #
# flare_summary
# --------------------------------------------------------------------------- #

def test_flare_summary_aggregates(db):
    pj._log_symptom(db, "hands", 8, now=T0 - 1 * DAY)
    pj._log_symptom(db, "hands", 6, now=T0 - 1 * DAY)
    pj._log_symptom(db, "knees", 2, now=T0)
    out = pj._flare_summary(db, days=7, now=T0)
    assert "3 entries" in out
    assert "peak 8" in out
    assert "hands" in out  # worst area by mean severity


def test_flare_summary_empty_window(db):
    pj._log_symptom(db, "hands", 5, now=T0 - 30 * DAY)
    assert "no pain logged in the last 7 days" in pj._flare_summary(db, days=7, now=T0).lower()


# --------------------------------------------------------------------------- #
# appointment_brief
# --------------------------------------------------------------------------- #

def test_appointment_brief_includes_symptoms_and_meds(db):
    pj._log_symptom(db, "hands", 7, now=T0 - 3 * DAY)
    pj._log_symptom(db, "hands", 5, now=T0 - 1 * DAY)
    pj._log_med(db, "humira", now=T0 - 2 * DAY)
    out = pj._appointment_brief(db, days=30, now=T0)
    assert "hands: 2x" in out
    assert "severity 5-7" in out
    assert "humira: 1x" in out


def test_appointment_brief_empty_window(db):
    pj._log_symptom(db, "hands", 5, now=T0 - 60 * DAY)  # db exists, but outside 30d
    assert "nothing logged" in pj._appointment_brief(db, days=30, now=T0).lower()


def test_appointment_brief_no_db(db):
    assert "no pain logged" in pj._appointment_brief(db, days=30, now=T0).lower()


# --------------------------------------------------------------------------- #
# manifest sanity
# --------------------------------------------------------------------------- #

def test_manifest_is_consistent():
    manifest = json.loads(
        (Path(__file__).parent.parent / "skills" / "manifests" / "pain_journal.json")
        .read_text(encoding="utf-8")
    )
    allow = set(manifest["tools"]["allow"])
    # Local-only skill: nothing should be flagged as an egress/send tool.
    assert manifest["tools"]["send_tools"] == []
    # Every intent must resolve to a declared, allowed tool.
    for name, intent in manifest["intents"].items():
        assert intent["tool"] in allow, f"intent {name} -> unknown tool {intent['tool']}"
        assert intent["send"] is False
    # Every allowed tool must actually exist on the server module.
    for tool in allow:
        assert hasattr(pj, tool), f"server missing tool {tool}"


def test_keywords_never_hijack_other_domains():
    # Mirror the skill-breadth hijack guard (pain_journal is outside its
    # _NEW_SKILLS set, so it needs its own): no intent keyword may appear in a
    # representative dev / command / schedule / system-control / personal-KB
    # utterance — including the system-control phrase "pain day on".
    utterances = [
        "find my bug in the parser",
        "write a python function to train a transformer model",
        "scroll down", "open the web browser", "click the button",
        "what are my reminders", "every morning at 8 brief me",
        "pain day on", "pain day off", "review queue",
        "authorize fix the tests",
        "what did I write in my notes about my doctor",
    ]
    manifest = json.loads(
        (Path(__file__).parent.parent / "skills" / "manifests" / "pain_journal.json")
        .read_text(encoding="utf-8")
    )
    keywords = [kw.lower() for intent in manifest["intents"].values()
                for kw in intent["keywords"]]
    for utterance in utterances:
        tl = utterance.lower()
        hits = [kw for kw in keywords if kw in tl]
        assert not hits, f"{hits} hijacks {utterance!r}"


# --------------------------------------------------------------------------- #
# One real stdio round-trip through the SkillRegistry — proves the MCP loop
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_pain_journal_end_to_end(tmp_path, monkeypatch):
    from skills.registry import SkillRegistry

    monkeypatch.setenv("DA_PAIN_DB", str(tmp_path / "pain.db"))
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    manifest = json.loads(
        (Path(__file__).parent.parent / "skills" / "manifests" / "pain_journal.json")
        .read_text(encoding="utf-8")
    )
    manifest["server"]["command"] = sys.executable  # current interpreter, not bare "python"
    (mdir / "pain_journal.json").write_text(json.dumps(manifest), encoding="utf-8")

    reg = SkillRegistry(manifest_dir=mdir)
    await reg.start()
    try:
        assert reg.has_skills()
        res = await reg.call("pain_journal", "log_symptom",
                             {"area": "hands", "severity": 6})
        assert res["status"] == "ok" and "hands" in res["text"]
        out = await reg.call("pain_journal", "recent_symptoms", {"days": 7})
        assert out["status"] == "ok" and "hands" in out["text"]
    finally:
        await reg.stop()
        from core.domain_classifier import DomainClassifier
        DomainClassifier.register_skill_keywords(set())   # don't leak into other tests
