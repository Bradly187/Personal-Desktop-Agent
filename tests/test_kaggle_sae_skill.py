"""GAP-8 — Kaggle SAE benchmark skill (disabled-by-default, no live API).

The manifest is valid and disabled, and the server's _request fails gracefully
("not configured") without an API key — so booting the registry never reaches
the network.

Run:
    python -m pytest tests/test_kaggle_sae_skill.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_MANIFEST = Path(__file__).parent.parent / "skills" / "manifests" / "kaggle_sae.json"


def test_manifest_valid_and_disabled():
    m = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert m["skill_id"] == "kaggle_sae"
    assert m["enabled"] is False
    assert set(m["tools"]["allow"]) == {
        "register_agent", "fetch_exam", "submit_answer", "get_score"}
    # send tools are a subset of allowed tools
    assert set(m["tools"]["send_tools"]) <= set(m["tools"]["allow"])
    # every intent points at an allowed tool
    for intent in m["intents"].values():
        assert intent["tool"] in m["tools"]["allow"]


def test_server_not_configured(monkeypatch):
    monkeypatch.delenv("KAGGLE_SAE_API_KEY", raising=False)
    from skills.servers import kaggle_sae_server as s
    out = s._request("GET", "/exams/123")
    assert "not configured" in out.lower()
