"""E2 — bounded, flag-gated domain-classifier keyword overlay.

Covers the classifier nudge (off by default, bounded when on), the AgentDB overlay
round-trip/clamp, and the ContinuousTrainer learner + rollback discipline.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.domain_classifier as dc
from core.domain_classifier import DomainClassifier
from core.command_executor import Command
from storage.db import AgentDB
from adaptive.continuous_trainer import ContinuousTrainer


async def _open_db():
    d = tempfile.mkdtemp()
    db = AgentDB()
    await db.open(os.path.join(d, "agent.db"))
    return db


# --------------------------------------------------------------------------- #
# classifier overlay nudge
# --------------------------------------------------------------------------- #

def test_overlay_off_by_default_is_noop(monkeypatch):
    monkeypatch.setattr(dc, "_DOMAIN_LEARN", False)
    DomainClassifier.register_keyword_overlay({"code": {"frobnicate": 5.0}})
    try:
        clf = DomainClassifier()
        base = {s.domain: s.score for s in clf.score("please frobnicate the buffer now")}
        # overlay must NOT change scores when the flag is off
        DomainClassifier.register_keyword_overlay({})
        off = {s.domain: s.score for s in clf.score("please frobnicate the buffer now")}
        assert base == off
    finally:
        DomainClassifier.register_keyword_overlay({})


def test_overlay_nudges_when_enabled(monkeypatch):
    monkeypatch.setattr(dc, "_DOMAIN_LEARN", True)
    clf = DomainClassifier()
    text = "please frobnicate the widget thoroughly today"
    DomainClassifier.register_keyword_overlay({})
    before = {s.domain: s.score for s in clf.score(text)}
    DomainClassifier.register_keyword_overlay({"code": {"frobnicate": 5.0}})
    try:
        after = {s.domain: s.score for s in clf.score(text)}
        assert after["code"] > before["code"]
    finally:
        DomainClassifier.register_keyword_overlay({})


def test_overlay_nudge_is_bounded(monkeypatch):
    monkeypatch.setattr(dc, "_DOMAIN_LEARN", True)
    clf = DomainClassifier()
    # many overlay hits, each weight 5 — total far exceeds the cap
    overlay = {"code": {w: 5.0 for w in
                        ["frobnicate", "widget", "buffer", "thoroughly", "today"]}}
    DomainClassifier.register_keyword_overlay(overlay)
    try:
        text = "frobnicate widget buffer thoroughly today"
        nudge = clf._overlay_nudge("code", dc._tokenize(text))
        assert nudge == DomainClassifier._MAX_OVERLAY_NUDGE     # capped
    finally:
        DomainClassifier.register_keyword_overlay({})


# --------------------------------------------------------------------------- #
# AgentDB overlay round-trip + clamp
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_db_overlay_roundtrip_and_clamp():
    db = await _open_db()
    try:
        await db.upsert_domain_keyword_weight("code", "Frobnicate", 3.0)
        await db.upsert_domain_keyword_weight("code", "widget", 99.0)   # clamps to MAX
        await db.upsert_domain_keyword_weight("math", "lemma", 2.0)
        ov = await db.get_domain_keyword_weights()
        assert ov["code"]["frobnicate"] == 3.0          # lower-cased key
        assert ov["code"]["widget"] == db._DOMAIN_OVERLAY_MAX
        assert ov["math"]["lemma"] == 2.0
        await db.clear_domain_keyword_overlay("code")
        ov2 = await db.get_domain_keyword_weights()
        assert "code" not in ov2 and "math" in ov2
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# trainer learner + rollback
# --------------------------------------------------------------------------- #

async def _seed_examples(db, domain, distinctive_token, n=3):
    for k in range(n):
        await db.upsert_few_shot_example(
            Command(text=f"{distinctive_token} sample {k} extra words here",
                    action="", source="voice"),
            "DO it", domain)


@pytest.mark.asyncio
async def test_learner_writes_distinctive_overlay(monkeypatch):
    monkeypatch.setattr(dc, "_DOMAIN_LEARN", True)
    DomainClassifier.register_keyword_overlay({})
    db = await _open_db()
    try:
        await _seed_examples(db, "code", "frobnicate")
        await _seed_examples(db, "math", "eigenvalue")
        trainer = ContinuousTrainer(agent_db=db)
        await trainer._learn_domain_overlay()
        ov = await db.get_domain_keyword_weights()
        assert "frobnicate" in ov.get("code", {})
        assert "eigenvalue" in ov.get("math", {})
        # distinctive: frobnicate must NOT have leaked into math
        assert "frobnicate" not in ov.get("math", {})
        # overlay registered on the shared classifier
        assert DomainClassifier._KEYWORD_OVERLAY.get("code", {}).get("frobnicate")
    finally:
        DomainClassifier.register_keyword_overlay({})
        await db.close()


@pytest.mark.asyncio
async def test_learner_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(dc, "_DOMAIN_LEARN", False)
    db = await _open_db()
    try:
        await _seed_examples(db, "code", "frobnicate")
        trainer = ContinuousTrainer(agent_db=db)
        await trainer._learn_domain_overlay()
        assert await db.get_domain_keyword_weights() == {}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_learner_rolls_back_on_rate_rise(monkeypatch):
    monkeypatch.setattr(dc, "_DOMAIN_LEARN", True)
    db = await _open_db()
    try:
        # an existing learned overlay for code + a prior vocab log at low rate
        await db.upsert_domain_keyword_weight("code", "frobnicate", 2.0)
        await db.log_adaptation(component="vocab:code", metric_before=1.0,
                                metric_after=0.10, domain="code")   # learned at 10%
        trainer = ContinuousTrainer(agent_db=db)
        trainer.misroute_status = {"code": 0.40}   # rate rose well past +0.05
        await trainer._learn_domain_overlay()
        ov = await db.get_domain_keyword_weights()
        assert "code" not in ov                     # overlay rolled back/cleared
    finally:
        DomainClassifier.register_keyword_overlay({})
        await db.close()
