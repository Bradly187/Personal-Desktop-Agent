"""Audit-log hash chain — tamper-evidence (M1 / audit finding #23).

The append-only triggers block UPDATE/DELETE, but a writer that drops the
triggers could still modify/delete rows. The per-row SHA-256 chain makes any
such interior tampering detectable via verify_chain().

Run:
    python -m pytest tests/test_audit_chain.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.audit_log import AuditLog, _CHAIN_GENESIS


@pytest.fixture
async def audit(tmp_path):
    a = AuditLog()
    await a.open(tmp_path / "audit.db")
    if not a.available:
        pytest.skip("aiosqlite unavailable")
    yield a
    await a.close()


async def test_chain_links_rows(audit):
    await audit.log("mcp_call", tool="mouse_click")
    await audit.log("shell_exec", detail="ls")
    await audit.log("security_event", detail="x", severity="warning")
    rows = sorted(await audit.get_recent(10), key=lambda r: r["id"])
    assert rows[0]["prev_hash"] == _CHAIN_GENESIS          # genesis anchor
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]     # each links the prior
    assert rows[2]["prev_hash"] == rows[1]["row_hash"]
    assert audit.chain_head() == rows[2]["row_hash"]


async def test_verify_clean_chain(audit):
    for i in range(5):
        await audit.log("mcp_call", tool=f"t{i}")
    res = await audit.verify_chain()
    assert res["ok"] is True
    assert res["rows_checked"] == 5
    assert res["unchained"] == 0
    assert res["break_at"] is None


async def test_verify_detects_field_tampering(audit):
    await audit.log("mcp_call", tool="a")
    await audit.log("mcp_call", tool="b")
    await audit.log("mcp_call", tool="c")
    # Tamper a row's payload directly (simulate a writer that dropped the trigger).
    await audit._conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
    await audit._conn.execute(
        "UPDATE audit_events SET detail='tampered' WHERE id=2")
    await audit._conn.commit()
    res = await audit.verify_chain()
    assert res["ok"] is False
    assert res["break_at"] == 2          # the modified row fails recomputation


async def test_verify_detects_interior_deletion(audit):
    for i in range(4):
        await audit.log("mcp_call", tool=f"t{i}")
    await audit._conn.execute("DROP TRIGGER IF EXISTS audit_no_delete")
    await audit._conn.execute("DELETE FROM audit_events WHERE id=2")
    await audit._conn.commit()
    res = await audit.verify_chain()
    assert res["ok"] is False
    # Row 3's prev_hash points to the now-missing row 2 → linkage break at id 3.
    assert res["break_at"] == 3


async def test_chain_survives_restart(tmp_path):
    path = tmp_path / "audit.db"
    a = AuditLog()
    await a.open(path)
    if not a.available:
        pytest.skip("aiosqlite unavailable")
    await a.log("mcp_call", tool="before")
    head_before = a.chain_head()
    await a.close()

    # Reopen: the chain head must resume so the next row links the prior one.
    b = AuditLog()
    await b.open(path)
    assert b.chain_head() == head_before
    await b.log("mcp_call", tool="after")
    res = await b.verify_chain()
    assert res["ok"] is True and res["rows_checked"] == 2
    rows = sorted(await b.get_recent(10), key=lambda r: r["id"])
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]    # spans the restart
    await b.close()


async def test_legacy_rows_counted_as_unchained(tmp_path):
    # Simulate a pre-chain audit.db: a row with NULL prev_hash/row_hash, then a
    # new chained row appended after the upgrade.
    path = tmp_path / "audit.db"
    a = AuditLog()
    await a.open(path)
    if not a.available:
        pytest.skip("aiosqlite unavailable")
    await a._conn.execute(
        "INSERT INTO audit_events (ts, event_type, severity, actor) "
        "VALUES (1.0, 'legacy', 'info', 'agent')")
    await a._conn.commit()
    await a.log("mcp_call", tool="chained")    # first chained row
    res = await a.verify_chain()
    assert res["ok"] is True
    assert res["unchained"] == 1
    assert res["rows_checked"] == 1
    await a.close()


async def test_params_and_redacted_are_chained(audit):
    # Fields beyond detail/tool must also be bound by the hash.
    await audit.log("api_call", tool="anthropic", params={"k": "v"}, redacted=True)
    await audit.log("mcp_call", tool="next")
    await audit._conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
    await audit._conn.execute("UPDATE audit_events SET redacted=0 WHERE id=1")
    await audit._conn.commit()
    res = await audit.verify_chain()
    assert res["ok"] is False
    assert res["break_at"] == 1
