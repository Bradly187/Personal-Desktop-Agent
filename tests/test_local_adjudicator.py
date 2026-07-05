import pytest
from unittest.mock import AsyncMock, MagicMock

from inference.adjudicator import LocalAdjudicator

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_pending_escalations = AsyncMock(return_value=[
        {"id": 1, "goal": "do impossible", "reason": "max_replans", "failed_action": "cmd", "detail": "..."}
    ])
    db.resolve_escalations = AsyncMock()
    return db

@pytest.fixture
def mock_router():
    router = MagicMock()
    res = MagicMock()
    res.ok = True
    res.text = "DISMISS"
    router.infer = AsyncMock(return_value=res)
    return router

@pytest.mark.asyncio
async def test_adjudicator_off(mock_db, mock_router, monkeypatch):
    monkeypatch.setenv("DA_AUTO_ADJUDICATE", "0")
    adj = LocalAdjudicator(mock_db, mock_router)
    assert not adj.is_enabled()
    
    dismissed = await adj.adjudicate_pending()
    assert dismissed == 0
    mock_router.infer.assert_not_called()
    mock_db.resolve_escalations.assert_not_called()

@pytest.mark.asyncio
async def test_adjudicator_dismiss(mock_db, mock_router, monkeypatch):
    monkeypatch.setenv("DA_AUTO_ADJUDICATE", "1")
    adj = LocalAdjudicator(mock_db, mock_router)
    assert adj.is_enabled()
    
    dismissed = await adj.adjudicate_pending()
    assert dismissed == 1
    mock_router.infer.assert_called_once()
    mock_db.resolve_escalations.assert_called_once_with(status="auto_dismissed", escalation_id=1)
    
    # Second run should skip due to evaluated_ids
    dismissed2 = await adj.adjudicate_pending()
    assert dismissed2 == 0
    assert mock_router.infer.call_count == 1

@pytest.mark.asyncio
async def test_adjudicator_escalate(mock_db, mock_router, monkeypatch):
    monkeypatch.setenv("DA_AUTO_ADJUDICATE", "1")
    mock_router.infer.return_value.text = "ESCALATE"
    adj = LocalAdjudicator(mock_db, mock_router)
    
    dismissed = await adj.adjudicate_pending()
    assert dismissed == 0
    mock_router.infer.assert_called_once()
    mock_db.resolve_escalations.assert_not_called()

@pytest.mark.asyncio
async def test_adjudicator_failsafe_error(mock_db, mock_router, monkeypatch):
    monkeypatch.setenv("DA_AUTO_ADJUDICATE", "1")
    mock_router.infer.side_effect = Exception("Model down")
    adj = LocalAdjudicator(mock_db, mock_router)
    
    dismissed = await adj.adjudicate_pending()
    assert dismissed == 0
    mock_db.resolve_escalations.assert_not_called()

@pytest.mark.asyncio
async def test_adjudicator_failsafe_parse(mock_db, mock_router, monkeypatch):
    monkeypatch.setenv("DA_AUTO_ADJUDICATE", "1")
    mock_router.infer.return_value.text = "UNSURE"
    adj = LocalAdjudicator(mock_db, mock_router)
    
    dismissed = await adj.adjudicate_pending()
    assert dismissed == 0
    mock_db.resolve_escalations.assert_not_called()
