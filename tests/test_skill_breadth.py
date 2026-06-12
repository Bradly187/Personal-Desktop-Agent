"""Skill-breadth sprint — notes / arxiv / weather / files / diagrams.

Logic functions tested with tmp dirs + fake fetchers (no network, no model);
one real stdio round-trip (notes) proves the end-to-end MCP loop; manifest
validation + a keyword-collision guard protect routing.

Run:
    python -m pytest tests/test_skill_breadth.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from skills.servers import notes_server as ns
from skills.servers import arxiv_server as ax
from skills.servers import weather_server as ws
from skills.servers import files_server as fs
from skills.servers import diagrams_server as ds

_MANIFEST_DIR = Path(__file__).parent.parent / "skills" / "manifests"
_NEW_SKILLS = ("notes", "arxiv", "weather", "files", "diagrams")


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

def test_add_note_creates_dated_file(tmp_path):
    out = ns._add_note(tmp_path, "methotrexate at 6pm", "Medication", now=1750000000.0)
    assert "Saved note" in out
    files = list((tmp_path / "inbox").glob("*-medication.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "# Medication" in body and "methotrexate at 6pm" in body


def test_add_note_dedupes_filename(tmp_path):
    ns._add_note(tmp_path, "one", "same title", now=1750000000.0)
    ns._add_note(tmp_path, "two", "same title", now=1750000000.0)
    assert len(list((tmp_path / "inbox").glob("*same-title*.md"))) == 2


def test_add_note_empty_is_honest(tmp_path):
    assert "empty" in ns._add_note(tmp_path, "   ")
    assert not (tmp_path / "inbox").exists()


def test_append_journal_monthly_file(tmp_path):
    ns._append_journal(tmp_path, "slept badly", now=1750000000.0)
    ns._append_journal(tmp_path, "felt better", now=1750000000.0)
    files = list((tmp_path / "journal").glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "slept badly" in body and "felt better" in body


def test_read_recent_notes_preview(tmp_path):
    ns._add_note(tmp_path, "alpha content", "a")
    out = ns._read_recent_notes(tmp_path, 5)
    assert "alpha content" in out
    assert ns._read_recent_notes(tmp_path / "nope", 5) == "No notes yet."


# ---------------------------------------------------------------------------
# arxiv
# ---------------------------------------------------------------------------

_ATOM_SAMPLE = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2406.01234v1</id>
    <title>Quantum  Error
      Correction Advances</title>
    <summary> We study   stabilizer codes. </summary>
    <published>2026-06-12T01:00:00Z</published>
    <author><name>A. Researcher</name></author>
    <author><name>B. Scholar</name></author>
  </entry>
</feed>"""


def test_parse_feed_normalises():
    papers = ax._parse_feed(_ATOM_SAMPLE)
    assert papers[0]["id"] == "2406.01234v1"
    assert papers[0]["title"] == "Quantum Error Correction Advances"
    assert papers[0]["abstract"] == "We study stabilizer codes."
    assert papers[0]["authors"] == ["A. Researcher", "B. Scholar"]


def test_query_api_formats_and_handles_failure():
    out = ax._query_api("cat:quant-ph", 5, fetch=lambda url: _ATOM_SAMPLE)
    assert "[2406.01234v1]" in out and "stabilizer codes" in out

    def _boom(url):
        raise OSError("offline")
    assert "failed" in ax._query_api("cat:quant-ph", 5, fetch=_boom)


def test_fetch_pdf_validates_id(tmp_path):
    assert "doesn't look like" in ax._fetch_pdf("../../etc/passwd", tmp_path)
    assert "doesn't look like" in ax._fetch_pdf("; rm -rf /", tmp_path)
    out = ax._fetch_pdf("2406.01234", tmp_path, fetch=lambda url: b"%PDF-fake")
    assert "Saved 2406.01234.pdf" in out
    assert (tmp_path / "2406.01234.pdf").read_bytes() == b"%PDF-fake"


def test_fetch_pdf_old_style_id_sanitised(tmp_path):
    out = ax._fetch_pdf("quant-ph/0301001", tmp_path, fetch=lambda url: b"%PDF")
    assert (tmp_path / "quant-ph_0301001.pdf").exists()
    assert "Saved" in out


# ---------------------------------------------------------------------------
# weather
# ---------------------------------------------------------------------------

def test_geocode_and_save(tmp_path):
    cfg_path = tmp_path / "weather.json"
    fake = lambda url: {"results": [{"latitude": 30.27, "longitude": -97.74,
                                     "name": "Austin", "admin1": "Texas"}]}
    out = ws._geocode_and_save("austin texas", cfg_path, fetch=fake)
    assert "Austin, Texas" in out
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["lat"] == 30.27 and cfg["lon"] == -97.74


def test_current_weather_includes_pressure_drop():
    cfg = {"lat": 1, "lon": 2, "place": "Austin, Texas"}
    fake = lambda url: {
        "current": {"temperature_2m": 90, "apparent_temperature": 95,
                    "weather_code": 2, "wind_speed_10m": 8},
        "hourly": {"surface_pressure": [1015.0] + [1014] * 11 + [1009.0]},
    }
    out = ws._current(cfg, fetch=fake)
    assert "90°F" in out and "partly cloudy" in out
    assert "falling 6 hPa" in out and "flare-relevant" in out


def test_current_weather_steady_pressure():
    cfg = {"lat": 1, "lon": 2}
    fake = lambda url: {"current": {"temperature_2m": 70, "weather_code": 0,
                                    "apparent_temperature": 70, "wind_speed_10m": 2},
                        "hourly": {"surface_pressure": [1015.0] * 13}}
    assert "steady" in ws._current(cfg, fetch=fake)


def test_no_location_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_WEATHER_CONFIG", str(tmp_path / "none.json"))
    assert "set my location" in ws.current_weather()


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

@pytest.fixture
def file_roots(tmp_path):
    dl = tmp_path / "Downloads"; dl.mkdir()
    docs = tmp_path / "Documents"; docs.mkdir()
    (dl / "report-budget.pdf").write_bytes(b"x" * 2048)
    (docs / "old.txt").write_text("old", encoding="utf-8")
    import os, time
    os.utime(docs / "old.txt", (time.time() - 86400 * 30,) * 2)
    return [dl, docs]


def test_recent_files_newest_first(file_roots):
    out = fs._recent_files(file_roots, 5)
    assert out.index("report-budget.pdf") < out.index("old.txt")


def test_find_file_by_name(file_roots):
    out = fs._find_file(file_roots, "budget")
    assert "report-budget.pdf" in out
    assert "No file names match" in fs._find_file(file_roots, "zzz-nope")


def test_open_file_refuses_outside_roots(file_roots, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    opened = []
    assert "Refusing" in fs._open_file(file_roots, str(outside), opener=opened.append)
    assert not opened
    inside = file_roots[0] / "report-budget.pdf"
    assert "Opened" in fs._open_file(file_roots, str(inside), opener=opened.append)
    assert opened == [str(inside)]


# ---------------------------------------------------------------------------
# diagrams
# ---------------------------------------------------------------------------

def test_create_mermaid_writes_html_and_source(tmp_path):
    opened = []
    out = ds._create_diagram(tmp_path, "Sensor pipeline",
                             "graph TD; A-->B;", "mermaid", opener=opened.append)
    assert "Saved" in out and "opened" in out
    html = next(tmp_path.glob("*-sensor-pipeline.html"))
    assert "graph TD; A-->B;" in html.read_text(encoding="utf-8")
    assert next(tmp_path.glob("*-sensor-pipeline.mmd"))   # KB-indexable source
    assert opened == [str(html)]


def test_create_svg_validates(tmp_path):
    assert "requires the source" in ds._create_diagram(
        tmp_path, "w", "not svg", "svg", opener=lambda p: None)
    out = ds._create_diagram(tmp_path, "login wireframe",
                             "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                             "svg", opener=lambda p: None)
    assert "Saved" in out
    assert next(tmp_path.glob("*-login-wireframe.svg"))


def test_create_diagram_bad_kind_and_empty(tmp_path):
    assert "Unsupported kind" in ds._create_diagram(tmp_path, "t", "x", "png",
                                                    opener=lambda p: None)
    assert "No diagram source" in ds._create_diagram(tmp_path, "t", "  ", "mermaid",
                                                     opener=lambda p: None)


def test_list_diagrams(tmp_path):
    ds._create_diagram(tmp_path, "one", "graph TD; A;", opener=lambda p: None)
    assert "one" in ds._list_diagrams(tmp_path)
    assert ds._list_diagrams(tmp_path / "none") == "No diagrams yet."


# ---------------------------------------------------------------------------
# Manifests + routing safety
# ---------------------------------------------------------------------------

def _load_manifests() -> dict[str, dict]:
    return {sid: json.loads((_MANIFEST_DIR / f"{sid}.json").read_text(encoding="utf-8"))
            for sid in _NEW_SKILLS}


def test_manifests_valid_and_enabled():
    for sid, m in _load_manifests().items():
        assert m["skill_id"] == sid
        assert m["enabled"] is True            # auth-free → on by default
        assert m["transport"] == "stdio"
        assert m["tools"]["send_tools"] == []  # nothing gated in this wave
        for intent in m["intents"].values():
            assert intent["tool"] in m["tools"]["allow"]
            assert intent["keywords"]


def test_skill_keywords_never_hijack_other_domains():
    # The personal-KB review lesson: generic keywords steal queries from
    # code/command/schedule routing. Every intent keyword across the new
    # manifests must be absent from these representative utterances.
    utterances = [
        "find my bug in the parser",
        "search my codebase for the fusion tick loop",
        "write a python function to train a transformer model",
        "prove that the matrix is positive definite",
        "scroll down", "open the web browser", "click the button",
        "what are my reminders", "every morning at 8 brief me",
        "pain day on", "review queue", "authorize fix the tests",
        "what did I write in my notes about my doctor",   # personal-KB query
    ]
    keywords = [kw.lower()
                for m in _load_manifests().values()
                for intent in m["intents"].values()
                for kw in intent["keywords"]]
    for utterance in utterances:
        tl = utterance.lower()
        hits = [kw for kw in keywords if kw in tl]
        assert not hits, f"{hits} hijacks {utterance!r}"


def test_match_intent_propagates_plan_flag():
    from skills.registry import SkillRegistry, _Skill
    reg = SkillRegistry(manifest_dir=_MANIFEST_DIR)
    manifest = json.loads((_MANIFEST_DIR / "diagrams.json").read_text(encoding="utf-8"))
    reg._skills["diagrams"] = _Skill(
        manifest=manifest, session=None,
        tools={"create_diagram": object(), "list_diagrams": object()},
        lock=asyncio.Lock())
    m = reg.match_intent("draw a diagram of the sensor pipeline")
    assert m and m["tool"] == "create_diagram" and m["plan"] is True
    m2 = reg.match_intent("list my diagrams")
    assert m2 and m2["plan"] is False


async def test_handle_skill_plan_flag_routes_to_planner():
    from inference.dev_agent import DevAgent
    agent = DevAgent(router=MagicMock())
    reg = MagicMock()
    reg.match_intent = MagicMock(return_value={
        "skill_id": "diagrams", "tool": "create_diagram", "send": False,
        "summarize": False, "plan": True, "keyword": "draw a diagram", "score": 14})
    agent._skill_registry = reg
    agent.plan_and_run = AsyncMock(return_value=MagicMock(domain="plan"))
    res = await agent._handle_skill("draw a diagram of the sensor pipeline")
    agent.plan_and_run.assert_awaited_once()
    assert res.domain == "plan"


# ---------------------------------------------------------------------------
# One real stdio round-trip (notes) — proves the end-to-end MCP loop
# ---------------------------------------------------------------------------

async def test_notes_skill_end_to_end(tmp_path, monkeypatch):
    from skills.registry import SkillRegistry
    monkeypatch.setenv("DA_NOTES_ROOT", str(tmp_path / "Notes"))
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    manifest = json.loads((_MANIFEST_DIR / "notes.json").read_text(encoding="utf-8"))
    manifest["server"]["command"] = sys.executable
    (mdir / "notes.json").write_text(json.dumps(manifest), encoding="utf-8")

    reg = SkillRegistry(manifest_dir=mdir)
    await reg.start()
    try:
        assert reg.has_skills()
        res = await reg.call("notes", "add_note",
                             {"text": "remember the thing", "title": "test"})
        assert res["status"] == "ok" and "Saved note" in res["text"]
        notes = list((tmp_path / "Notes" / "inbox").glob("*.md"))
        assert len(notes) == 1
        assert "remember the thing" in notes[0].read_text(encoding="utf-8")
    finally:
        await reg.stop()
        from core.domain_classifier import DomainClassifier
        DomainClassifier.register_skill_keywords(set())   # don't leak
