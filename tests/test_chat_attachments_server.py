"""ChatServer attachment + active-dir surfaces (specs/chat-context-attachments R1/R2).

`/upload` type/size/traversal validation, the set_active_dir/list_dirs WS
delegation, and attachment extraction into Command.params.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from aiohttp import web, FormData
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer


class _FakeCoord:
    """Minimal coordinator stub exposing the two methods ChatServer calls."""
    def __init__(self, active_root):
        self._active_root = active_root
        self.calls = []

    def list_writable_roots(self):
        return {"active_root": self._active_root, "writable_roots": [self._active_root]}

    def set_active_directory(self, path, *, confirm=False):
        self.calls.append((path, confirm))
        return {"status": "activated", "path": path, "active_root": path,
                "writable_roots": [path]}


def _server(active_root) -> ChatServer:
    s = ChatServer()
    s.set_coordinator(_FakeCoord(str(active_root)))
    return s


async def _client(server: ChatServer) -> TestClient:
    app = web.Application()
    app.router.add_post("/upload", server._upload_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _file_form(name: str, data: bytes) -> FormData:
    fd = FormData()
    fd.add_field("file", data, filename=name, content_type="application/octet-stream")
    return fd


# --- /upload validation (R2.1/R2.2) -----------------------------------------

@pytest.mark.asyncio
async def test_upload_png_ok(tmp_path):
    server = _server(tmp_path)
    client = await _client(server)
    try:
        resp = await client.post("/upload", data=_file_form("shot.png", b"\x89PNG\r\n\x1a\n\x00"))
        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "shot.png" and body["kind"] == "image"
        aid = body["attachment_id"]
        # Registered + stored under the active root's upload dir (R2.2).
        assert aid in server._uploads
        stored = server._uploads[aid]
        assert os.path.isfile(stored)
        assert str(tmp_path) in stored and ".chat_uploads" in stored
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_type(tmp_path):
    server = _server(tmp_path)
    client = await _client(server)
    try:
        resp = await client.post("/upload", data=_file_form("malware.exe", b"MZ"))
        assert resp.status == 415
        assert not server._uploads
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_rejects_traversal_filename(tmp_path):
    server = _server(tmp_path)
    client = await _client(server)
    try:
        # basename() strips the path, but a bare ".." must still be refused.
        resp = await client.post("/upload", data=_file_form("..", b"x"))
        assert resp.status in (400, 415)
        assert not server._uploads
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_rejects_oversize(tmp_path, monkeypatch):
    import core.chat_server as cs
    monkeypatch.setattr(cs, "_MAX_UPLOAD_BYTES", 10)
    server = _server(tmp_path)
    client = await _client(server)
    try:
        resp = await client.post("/upload", data=_file_form("big.pdf", b"%PDF-" + b"x" * 100))
        assert resp.status == 413
        # The partial file was cleaned up.
        up = Path(tmp_path) / ".chat_uploads"
        assert not up.exists() or not any(up.iterdir())
    finally:
        await client.close()


# --- WS delegation (R1.5/R1.2) ----------------------------------------------

def test_list_dirs_delegates(tmp_path):
    server = _server(tmp_path)
    info = server._list_dirs()
    assert info["active_root"] == str(tmp_path)


def test_set_active_dir_delegates_with_confirm(tmp_path):
    server = _server(tmp_path)
    server._set_active_dir("/some/proj", True)
    assert server._coordinator.calls == [("/some/proj", True)]


def test_list_dirs_without_coordinator_falls_back():
    server = ChatServer()                 # no coordinator wired
    info = server._list_dirs()
    assert "active_root" in info and info["writable_roots"]


# --- extraction into Command.params (R2.4) ----------------------------------

def test_build_attachment_params(tmp_path):
    server = _server(tmp_path)
    # Register a real SVG upload and a pdf-ish text file.
    svg = tmp_path / "d.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>', encoding="utf-8")
    server._uploads["a1"] = str(svg)
    params = server._build_attachment_params(["a1"])
    assert "attachment_context" in params
    assert params["attachment_names"] == ["d.svg"]
    # SVG → image when rasterizable, else text fallback; either way params built.
    assert ("attachment_image_b64" in params)


def test_build_attachment_params_empty():
    server = ChatServer()
    assert server._build_attachment_params([]) == {}
    assert server._build_attachment_params(["unknown-id"]) == {}
