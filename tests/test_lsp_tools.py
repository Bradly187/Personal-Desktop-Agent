import pytest
from unittest.mock import patch
from mcp_server.tools import lsp

@pytest.fixture
def mock_lsp_wrapper():
    with patch("mcp_server.tools.lsp._wrapper") as mock:
        mock.initialized = True
        yield mock

def test_get_definition(mock_lsp_wrapper):
    mock_lsp_wrapper.send_request.return_value = {
        "uri": "file:///test/path.py",
        "range": {
            "start": {"line": 10, "character": 5}
        }
    }
    
    res = lsp.get_definition("test_file.py", 5, 0)
    assert res["ok"] is True
    assert len(res["definitions"]) == 1
    assert res["definitions"][0]["line"] == 10

def test_find_references(mock_lsp_wrapper):
    mock_lsp_wrapper.send_request.return_value = [
        {
            "uri": "file:///test/path.py",
            "range": {
                "start": {"line": 10, "character": 5}
            }
        },
        {
            "uri": "file:///test/other.py",
            "range": {
                "start": {"line": 20, "character": 1}
            }
        }
    ]
    
    res = lsp.find_references("test_file.py", 5, 0)
    assert res["ok"] is True
    assert len(res["references"]) == 2
    assert res["references"][1]["line"] == 20
