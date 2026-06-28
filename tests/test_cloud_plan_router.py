"""Tests for CloudPlanRouter — route domain="plan" to Sonnet 4.6 (Bedrock),
keep execution + fallback local (specs/cloud-plan-routing).

No network: the Bedrock client is faked by patching the instance's `_get_client`
(or its `_client`), and the inner ModelRouter is a recording fake. One test per
numbered acceptance criterion (cited in the test name).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.cloud_plan_router import CloudPlanRouter, cloud_plan_enabled  # noqa: E402
from inference.model_router import RouterResult  # noqa: E402
from inference.dev_agent import _parse_plan_json_report  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeInner:
    """Recording stand-in for ModelRouter."""
    some_attr = "inner-attr"

    def __init__(self) -> None:
        self.infer_calls: list[tuple] = []

    async def infer(self, domain, user_text, screenshot_b64=None, context=None):
        self.infer_calls.append((domain, user_text, context))
        return RouterResult(
            text='{"steps":[{"action":"EXPLAIN","args":"from-local"}]}',
            model="qwen3-coder:30b", domain=domain,
            latency_ms=1.0, free_form=True, backend="ollama",
        )

    def select_profile(self, domain):           # delegated method (R1.2)
        return f"PROFILE:{domain}"


class _FakeFinding:
    def __init__(self, severity: str) -> None:
        self.severity = severity


class _FakeFilter:
    """Returns (redacted_text, findings). `redact` rewrites text so we can assert
    the SCRUBBED text reaches the cloud; `findings` drives the critical path."""
    def __init__(self, findings=None, redact=False) -> None:
        self._findings = findings or []
        self._redact = redact

    async def scrub(self, text):
        out = text.replace("SECRET", "[REDACTED]") if self._redact else text
        return out, list(self._findings)


class _FakeUsage:
    def __init__(self, i, o) -> None:
        self.input_tokens, self.output_tokens = i, o


class _FakeToolBlock:
    def __init__(self, name, inp) -> None:
        self.type, self.name, self.input = "tool_use", name, inp


class _FakeTextBlock:
    def __init__(self, text) -> None:
        self.type, self.text = "text", text


class _FakeMessage:
    def __init__(self, content, usage=None) -> None:
        self.content, self.usage = content, usage


class _FakeMessages:
    def __init__(self, message=None, raise_exc=None) -> None:
        self._message, self._raise = message, raise_exc
        self.create_kwargs = None

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return self._message


class _FakeClient:
    def __init__(self, messages) -> None:
        self.messages = messages


class _FakeDB:
    available = True

    def __init__(self) -> None:
        self.inserts: list[dict] = []

    async def insert_inference(self, **kwargs):
        self.inserts.append(kwargs)
        return 1


def _plan_message(usage=None):
    return _FakeMessage(
        [_FakeToolBlock("emit_plan", {"steps": [
            {"action": "WRITE_FILE", "args": "a.py", "body": "x=1", "after": []},
        ]})],
        usage=usage,
    )


def _install_fake_client(router: CloudPlanRouter, client: _FakeClient, model="us.sonnet"):
    """Bypass _get_client (no SDK/credential) by pre-setting the client + model."""
    router._client = client
    router.model = model


# ---------------------------------------------------------------------------
# R1.2 — only plan is intercepted; everything else delegates to inner
# ---------------------------------------------------------------------------

async def test_r1_2_non_plan_domain_delegates_to_inner():
    inner = _FakeInner()
    r = CloudPlanRouter(inner)
    out = await r.infer("code", "write a function")
    assert out.backend == "ollama"                       # came from inner
    assert inner.infer_calls == [("code", "write a function", None)]


def test_r1_2_unknown_attrs_delegate_to_inner():
    inner = _FakeInner()
    r = CloudPlanRouter(inner)
    assert r.select_profile("plan") == "PROFILE:plan"    # method passthrough
    assert r.some_attr == "inner-attr"                   # attribute passthrough


# ---------------------------------------------------------------------------
# R1.3 — cloud plan output parses byte-compatibly with the local path
# ---------------------------------------------------------------------------

async def test_r1_3_cloud_plan_text_parses_into_steps():
    inner = _FakeInner()
    r = CloudPlanRouter(inner)
    _install_fake_client(r, _FakeClient(_FakeMessages(_plan_message())))
    out = await r.infer("plan", "build a thing")
    assert out.backend == "bedrock"
    assert inner.infer_calls == []                       # local NOT used
    report = _parse_plan_json_report(out.text)
    assert report.parsed_ok and len(report.steps) == 1
    assert report.steps[0].action == "WRITE_FILE" and report.steps[0].args == "a.py"


# ---------------------------------------------------------------------------
# R2.1 — fail-safe: any cloud failure → transparent local fallback
# ---------------------------------------------------------------------------

async def test_r2_1_client_error_falls_back_to_local():
    inner = _FakeInner()
    r = CloudPlanRouter(inner)
    _install_fake_client(r, _FakeClient(_FakeMessages(raise_exc=RuntimeError("boom"))))
    out = await r.infer("plan", "the goal", context="ctx")
    assert out.backend == "ollama"
    assert inner.infer_calls == [("plan", "the goal", "ctx")]   # original args


async def test_r2_1_missing_tool_block_falls_back_to_local():
    inner = _FakeInner()
    r = CloudPlanRouter(inner)
    msg = _FakeMessage([_FakeTextBlock("here is a plan in prose")])   # no tool_use
    _install_fake_client(r, _FakeClient(_FakeMessages(msg)))
    out = await r.infer("plan", "the goal")
    assert out.backend == "ollama" and len(inner.infer_calls) == 1


async def test_r2_1_no_credential_falls_back_to_local(monkeypatch):
    # No _client pre-set → _get_client runs; force resolve_backend to raise.
    inner = _FakeInner()
    r = CloudPlanRouter(inner)
    import core.cloud_backend as cb
    monkeypatch.setattr(cb, "resolve_backend",
                        lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    out = await r.infer("plan", "g")
    assert out.backend == "ollama" and len(inner.infer_calls) == 1


# ---------------------------------------------------------------------------
# R2.2 — default OFF
# ---------------------------------------------------------------------------

def test_r2_2_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DA_CLOUD_PLAN", raising=False)
    assert cloud_plan_enabled() is False
    monkeypatch.setenv("DA_CLOUD_PLAN", "1")
    assert cloud_plan_enabled() is True


# ---------------------------------------------------------------------------
# R3.1 / R3.2 — privacy: scrub before egress; critical finding forces local
# ---------------------------------------------------------------------------

async def test_r3_2_critical_finding_forces_local_no_client():
    inner = _FakeInner()
    cf = _FakeFilter(findings=[_FakeFinding("critical")])
    r = CloudPlanRouter(inner, content_filter=cf)
    # If the cloud path were taken, _get_client would raise (no client/SDK) — but
    # it must never be reached; the local path returns cleanly instead.
    out = await r.infer("plan", "deploy with SECRET", context="repo ctx")
    assert out.backend == "ollama"
    assert inner.infer_calls == [("plan", "deploy with SECRET", "repo ctx")]


async def test_r3_1_benign_context_is_scrubbed_then_sent():
    inner = _FakeInner()
    cf = _FakeFilter(findings=[_FakeFinding("warning")], redact=True)
    r = CloudPlanRouter(inner, content_filter=cf)
    msgs = _FakeMessages(_plan_message())
    _install_fake_client(r, _FakeClient(msgs))
    out = await r.infer("plan", "use SECRET token", context="ctx SECRET here")
    assert out.backend == "bedrock"
    sent = msgs.create_kwargs["messages"][0]["content"]
    assert "SECRET" not in sent and "[REDACTED]" in sent      # scrubbed reached cloud


# ---------------------------------------------------------------------------
# R4.1 — cost ledger records the bedrock plan call
# ---------------------------------------------------------------------------

async def test_r4_1_records_cost_to_ledger():
    inner = _FakeInner()
    db = _FakeDB()
    r = CloudPlanRouter(inner, agent_db=db)
    _install_fake_client(r, _FakeClient(_FakeMessages(_plan_message(_FakeUsage(100, 50)))))
    await r.infer("plan", "g")
    assert len(db.inserts) == 1
    row = db.inserts[0]
    assert row["backend"] == "bedrock" and row["domain"] == "plan"
    assert row["tokens_in"] == 100 and row["tokens_out"] == 50
