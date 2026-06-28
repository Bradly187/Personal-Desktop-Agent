"""File-context attachment extraction (specs/chat-context-attachments, R2).

The desktop chat lets the user attach ``.pdf`` / ``.png`` / ``.svg`` files to a
turn. This module turns an uploaded file into an :class:`Attachment` the DevAgent
can consume: a **text** extraction (pdf text, svg XML) injected into the planner /
answer context, or an **image** (png bytes, or svg rasterized) handed to the
resident vision model as ``screenshot_b64`` (AGENTS.md #6 — no new model).

Pure / deterministic. ``extract_attachment`` MUST NOT raise — every failure path
returns an ``Attachment(kind="error", …)`` so the chat server can surface a chip
without crashing the turn (R2.3a). All heavy deps (pypdf/pdfplumber/PyMuPDF/PIL)
are imported lazily and degrade gracefully when absent.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Allowed upload types (R2.1). Extension check; the server adds a content sniff.
ALLOWED_EXTS: frozenset = frozenset({".pdf", ".png", ".svg"})


@dataclass
class Attachment:
    """One extracted attachment. ``kind`` selects how the DevAgent consumes it."""
    name: str
    kind: str            # "text" | "image" | "error"
    text: str = ""       # populated for kind == "text"
    image_b64: str = ""  # populated for kind == "image"
    error: str = ""      # populated for kind == "error"


def is_allowed(name: str) -> bool:
    """True when ``name`` has an allowed attachment extension (case-insensitive)."""
    return os.path.splitext(name or "")[1].lower() in ALLOWED_EXTS


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _extract_pdf_text(path: str) -> str:
    """PDF text layer via pypdf, falling back to pdfplumber. '' if neither yields
    text (e.g. an image-only scan — OCR is a documented non-goal)."""
    # pypdf first (fast, pure-python).
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(path)
        parts = [(pg.extract_text() or "") for pg in reader.pages]
        text = "\n".join(p for p in parts if p.strip())
        if text.strip():
            return text
    except Exception as exc:  # noqa: BLE001
        log.debug("attachments: pypdf failed for %s: %s", path, exc)
    # pdfplumber fallback (better at some layouts).
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            parts = [(pg.extract_text() or "") for pg in pdf.pages]
        return "\n".join(p for p in parts if p.strip())
    except Exception as exc:  # noqa: BLE001
        log.debug("attachments: pdfplumber failed for %s: %s", path, exc)
        return ""


def _file_b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _svg_to_png_b64(path: str) -> str:
    """Rasterize an SVG to PNG base64 via PyMuPDF (fitz). '' if unavailable so the
    caller can fall back to the raw XML (R2.3a). No cairosvg dependency."""
    try:
        import fitz  # type: ignore  (PyMuPDF)
        doc = fitz.open(path)
        try:
            page = doc.load_page(0)
            pix = page.get_pixmap()
            return base64.b64encode(pix.tobytes("png")).decode("ascii")
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("attachments: svg raster failed for %s: %s", path, exc)
        return ""


def extract_attachment(path: str, *, max_text_chars: int = 20000) -> Attachment:
    """Extract an uploaded file into an :class:`Attachment`. Never raises (R2.3a).

    - ``.pdf`` → text layer (kind="text"); empty text when none extractable.
    - ``.png`` → base64 image (kind="image").
    - ``.svg`` → rasterized PNG base64 (kind="image"); falls back to raw XML
      (kind="text") when rasterization is unavailable.
    - anything else / missing / unreadable → kind="error".
    """
    name = os.path.basename(path or "")
    try:
        if not path or not os.path.isfile(path):
            return Attachment(name=name, kind="error", error="file not found")
        ext = os.path.splitext(name)[1].lower()

        if ext == ".pdf":
            text = _extract_pdf_text(path)
            return Attachment(name=name, kind="text",
                              text=_clip(text, max_text_chars))

        if ext == ".png":
            return Attachment(name=name, kind="image", image_b64=_file_b64(path))

        if ext == ".svg":
            png = _svg_to_png_b64(path)
            if png:
                return Attachment(name=name, kind="image", image_b64=png)
            # Fallback: the SVG source is XML text the model can still reason about.
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    return Attachment(name=name, kind="text",
                                      text=_clip(fh.read(), max_text_chars))
            except Exception as exc:  # noqa: BLE001
                return Attachment(name=name, kind="error", error=str(exc))

        return Attachment(name=name, kind="error",
                          error=f"unsupported type: {ext or '(none)'}")
    except Exception as exc:  # noqa: BLE001 — extraction must never crash the turn
        log.debug("attachments: extract failed for %s: %s", path, exc)
        return Attachment(name=name, kind="error", error=str(exc))


def render_attachment_context(attachments: list[Attachment]) -> str:
    """Render text attachments into one bounded context block for the planner, or
    '' when none have text. Image attachments contribute their name only (the bytes
    ride separately as screenshot_b64). Deterministic ordering (input order)."""
    if not attachments:
        return ""
    lines: list[str] = []
    for att in attachments:
        if att.kind == "text" and att.text.strip():
            lines.append(f'<attachment name="{att.name}">\n{att.text}\n</attachment>')
        elif att.kind == "image":
            lines.append(f'<attachment name="{att.name}" kind="image" '
                         f'note="provided to the vision model"/>')
        elif att.kind == "error":
            lines.append(f'<attachment name="{att.name}" error="{att.error}"/>')
    return "\n".join(lines)
