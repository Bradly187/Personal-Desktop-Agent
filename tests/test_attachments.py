"""Tests for inference/attachments.py (specs/chat-context-attachments R2.3/2.3a).

Pure extractor: pdf→text, png→image_b64, svg→image_b64 (XML fallback), unknown /
missing → error Attachment (never raises), text clipped at max_text_chars.
"""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from inference.attachments import (
    Attachment, extract_attachment, is_allowed, render_attachment_context,
    ALLOWED_EXTS,
)


def _make_png(path) -> None:
    """Write a minimal valid 1x1 PNG so PIL/extraction sees real image bytes."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    path.write_bytes(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
                     + chunk(b"IEND", b""))


# --- allow-list -------------------------------------------------------------

def test_is_allowed_only_pdf_png_svg():
    assert ALLOWED_EXTS == {".pdf", ".png", ".svg"}
    assert is_allowed("a.PDF") and is_allowed("b.png") and is_allowed("c.SvG")
    assert not is_allowed("x.exe") and not is_allowed("noext") and not is_allowed("")


# --- png → image ------------------------------------------------------------

def test_png_returns_base64_image(tmp_path):
    p = tmp_path / "shot.png"
    _make_png(p)
    att = extract_attachment(str(p))
    assert att.kind == "image" and att.name == "shot.png"
    # Round-trips as base64 and matches the file bytes.
    assert base64.b64decode(att.image_b64) == p.read_bytes()


# --- svg → image (raster) or text (xml fallback) ----------------------------

def test_svg_returns_image_or_xml_fallback(tmp_path):
    p = tmp_path / "diagram.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect width="10" height="10" fill="red"/></svg>',
        encoding="utf-8",
    )
    att = extract_attachment(str(p))
    assert att.name == "diagram.svg"
    if att.kind == "image":
        assert base64.b64decode(att.image_b64)          # valid PNG bytes
    else:
        # Rasterizer unavailable → degrades to the raw XML (R2.3a).
        assert att.kind == "text" and "<svg" in att.text


# --- pdf → text -------------------------------------------------------------

def test_pdf_returns_text(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    # A blank PDF has no text layer → empty text, but kind must still be "text"
    # and the call must not raise (R2.3a, image-only/empty pdf path).
    p = tmp_path / "doc.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with open(p, "wb") as fh:
        w.write(fh)
    att = extract_attachment(str(p))
    assert att.kind == "text" and att.name == "doc.pdf" and att.error == ""


# --- error paths: never raise ----------------------------------------------

def test_unknown_extension_is_error(tmp_path):
    p = tmp_path / "malware.exe"
    p.write_bytes(b"MZ...")
    att = extract_attachment(str(p))
    assert att.kind == "error" and "unsupported" in att.error


def test_missing_file_is_error_not_raise():
    att = extract_attachment("does/not/exist.pdf")
    assert att.kind == "error" and "not found" in att.error


def test_empty_path_is_error():
    att = extract_attachment("")
    assert att.kind == "error"


# --- text clipping ----------------------------------------------------------

def test_svg_text_fallback_is_clipped(tmp_path, monkeypatch):
    # Force the XML-fallback branch and a tiny clip budget.
    import inference.attachments as A
    monkeypatch.setattr(A, "_svg_to_png_b64", lambda _p: "")
    p = tmp_path / "big.svg"
    p.write_text("<svg>" + "x" * 5000 + "</svg>", encoding="utf-8")
    att = extract_attachment(str(p), max_text_chars=100)
    assert att.kind == "text" and att.text.endswith("…[truncated]")
    assert len(att.text) <= 100 + len("\n…[truncated]")


# --- render_attachment_context ---------------------------------------------

def test_render_context_mixes_text_image_error():
    atts = [
        Attachment(name="spec.pdf", kind="text", text="hello world"),
        Attachment(name="ui.png", kind="image", image_b64="AAAA"),
        Attachment(name="bad.svg", kind="error", error="boom"),
    ]
    block = render_attachment_context(atts)
    assert '<attachment name="spec.pdf">' in block and "hello world" in block
    assert 'name="ui.png" kind="image"' in block      # name only, no bytes
    assert 'name="bad.svg" error="boom"' in block
    assert "AAAA" not in block                          # image bytes never inlined


def test_render_context_empty_is_blank():
    assert render_attachment_context([]) == ""
    assert render_attachment_context([Attachment("e.pdf", "text", text="")]) == ""
