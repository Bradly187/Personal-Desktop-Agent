import asyncio
import logging
import webbrowser
from typing import Optional
from inference.dev_common import _get_trust_classifier, _strip_html

log = logging.getLogger(__name__)

async def fetch_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return extracted text (replaces browser-open SEARCH_WEB)."""
    from core.egress import EgressController, EgressError
    try:
        await EgressController.validate_url(url)
    except EgressError as e:
        raise RuntimeError(f"Egress policy violation: {e}")

    try:
        import aiohttp
    except ImportError:
        # Fall back to webbrowser open (old behaviour)
        await asyncio.to_thread(webbrowser.open, url)
        return f"Opened browser: {url}"

    headers = {"User-Agent": "Mozilla/5.0 (compatible; DesktopAgent/1.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                content_type = resp.content_type or ""
                if "html" in content_type:
                    html = await resp.text(errors="replace")
                    text = _strip_html(html)
                else:
                    text = await resp.text(errors="replace")
                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n… [truncated at {max_chars}]"
                text = await scan_web_content(url, text)
                log.info("DevAgent: fetched %s (%d chars)", url, len(text))
                return text
    except Exception as exc:
        raise RuntimeError(f"FETCH_URL {url} failed: {exc}") from exc

async def scan_web_content(url: str, text: str) -> str:
    """Taint-screen fetched web content before it enters the reasoning context."""
    try:
        verdict = await _get_trust_classifier().classify("fetch_url", text)
    except Exception as exc:  # noqa: BLE001 - fail open
        log.debug("DevAgent: web content scan failed (%s) — passing through", exc)
        return text
    if verdict.should_block:
        log.warning(
            "DevAgent: withheld HIGH-risk web content from %s [%s]",
            url, ", ".join(verdict.flags) or "?",
        )
        return ("[fetched content withheld — flagged as a possible prompt-"
                "injection / unsafe payload]")
    if verdict.should_warn:
        return "[TAINT] " + text
    return text

async def capture_screenshot() -> Optional[str]:
    try:
        from mcp_server.tools import screen as _screen
        result = await asyncio.to_thread(_screen.screenshot)
        return result.get("image_base64")
    except Exception as exc:
        log.warning("DevAgent: screenshot failed: %s", exc)
        return None
