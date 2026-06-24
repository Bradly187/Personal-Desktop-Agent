import base64
import io
import logging
from typing import Optional

import mss
import mss.tools
from PIL import Image

try:
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

log = logging.getLogger(__name__)


def screenshot(region: Optional[dict] = None) -> dict:
    """Capture a screenshot and return it as a base64-encoded PNG.

    region (optional): {"left": int, "top": int, "width": int, "height": int}
    If omitted, captures the full primary monitor.
    Returns: {"image_base64": str, "width": int, "height": int}
    """
    log.debug("screenshot region=%s", region)
    with mss.MSS() as sct:
        if region:
            monitor = {
                "left": region["left"],
                "top": region["top"],
                "width": region["width"],
                "height": region["height"],
            }
        else:
            monitor = sct.monitors[1]  # primary monitor

        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")

    return {
        "image_base64": encoded,
        "width": img.width,
        "height": img.height,
    }


def get_screen_size() -> dict:
    """Return the width and height of the primary monitor."""
    log.debug("get_screen_size")
    with mss.MSS() as sct:
        mon = sct.monitors[1]
        return {"width": mon["width"], "height": mon["height"]}


def find_text_on_screen(text: str) -> dict:
    """OCR the current screen and return the location of the given text.

    Returns: {"found": bool, "x": int|None, "y": int|None, "confidence": float|None}
    Requires pytesseract + Tesseract OCR installed on the system.
    """
    log.debug("find_text_on_screen %r", text[:60])
    if not _OCR_AVAILABLE:
        log.warning("find_text_on_screen: pytesseract not installed — OCR unavailable")
        return {"found": False, "x": None, "y": None, "confidence": None,
                "error": "pytesseract not installed"}

    with mss.MSS() as sct:
        raw = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    target = text.lower()
    for i, word in enumerate(data["text"]):
        if target in word.lower() and int(data["conf"][i]) > 0:
            x = data["left"][i] + data["width"][i] // 2
            y = data["top"][i] + data["height"][i] // 2
            return {
                "found": True,
                "x": x,
                "y": y,
                "confidence": float(data["conf"][i]) / 100.0,
            }

    return {"found": False, "x": None, "y": None, "confidence": None}
