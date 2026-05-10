"""AgentCore Fallback Client — called by HybridCoordinator._run_cloud().

This replaces the raw Bedrock API call with a call to the deployed AgentCore
agent. It can also call the local dev server for testing.

Memory-aware: passes session_id and actor_id so the agent can use its
long-term memory to improve disambiguation over time.

Usage:
    from agentcore_fallback.client import AgentCoreFallbackClient

    client = AgentCoreFallbackClient()  # defaults to local dev server
    action = await client.resolve(cmd)
    # action = "CLOSE" or "SCROLL down" etc.

    # Record a correction (teaches the agent)
    await client.record_correction(
        original_text="clothes the window",
        wrong_action="CLICK clothes",
        correct_action="CLOSE"
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class FallbackConfig:
    """Configuration for the AgentCore fallback client."""
    # Local dev server (agentcore dev)
    dev_url: str = "http://localhost:8080/invocations"
    # Deployed agent (agentcore invoke)
    deployed_url: str | None = None
    # Use dev server by default; set to False after deployment
    use_dev: bool = True
    # Timeout for HTTP calls
    timeout: float = 30.0
    # Actor ID for memory (identifies this desktop agent instance)
    actor_id: str = "desktop_user"
    # Session ID prefix (a new session is created per coordinator lifecycle)
    session_id: str = field(default_factory=lambda: f"session_{uuid.uuid4().hex[:8]}")


class AgentCoreFallbackClient:
    """HTTP client that calls the AgentCore fallback agent.

    Integrates with HybridCoordinator as a drop-in replacement for
    _CloudInference. The coordinator calls `resolve(cmd)` which returns
    an action string.

    Memory-aware: maintains a session_id across calls so the agent can
    use short-term memory for context, and actor_id for long-term
    preference learning.
    """

    def __init__(self, config: FallbackConfig | None = None) -> None:
        self._cfg = config or FallbackConfig()

    @property
    def url(self) -> str:
        if self._cfg.use_dev:
            return self._cfg.dev_url
        return self._cfg.deployed_url or self._cfg.dev_url

    async def resolve(self, cmd: Any) -> str:
        """Send an ambiguous command to the AgentCore agent and get an action.

        Args:
            cmd: A Command dataclass instance (from command_executor.py).

        Returns:
            An action string like "CLICK Save button" or "CLARIFY ...".
        """
        payload = {
            "prompt": f"Resolve: {cmd.text}",
            "command_text": cmd.text,
            "source": cmd.source,
            "whisper_logprob": cmd.whisper_logprob,
            "gesture_confidence": cmd.gesture_confidence,
            "session_context": cmd.session_context[-10:] if cmd.session_context else [],
            "session_id": self._cfg.session_id,
            "actor_id": self._cfg.actor_id,
        }

        return await self._post(payload)

    async def record_correction(
        self,
        original_text: str,
        wrong_action: str,
        correct_action: str,
    ) -> str:
        """Record a user correction so the agent learns from mistakes.

        Call this when the user indicates the resolved action was wrong.
        The correction is stored in the agent's long-term memory and
        used to improve future disambiguation.

        Args:
            original_text: The original ambiguous command text.
            wrong_action: The action that was incorrectly chosen.
            correct_action: The correct action the user wanted.

        Returns:
            Confirmation message from the agent.
        """
        payload = {
            "prompt": "Record correction",
            "original_text": original_text,
            "wrong_action": wrong_action,
            "correct_action": correct_action,
            "session_id": "corrections",
            "actor_id": self._cfg.actor_id,
        }

        return await self._post(payload)

    async def _post(self, payload: dict) -> str:
        """Send a POST request to the AgentCore agent."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._cfg.timeout),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        log.error("AgentCore fallback HTTP %d: %s", resp.status, error_text)
                        return f"CLARIFY cloud error: HTTP {resp.status}"

                    data = await resp.json()
                    response = data.get("response", "")

                    # Extract the action string (first non-empty line)
                    lines = [l.strip() for l in response.splitlines() if l.strip()]
                    action = lines[0] if lines else "CLARIFY no response"

                    log.info("AgentCore fallback: %r → %r", payload.get("command_text", ""), action)
                    return action

        except ImportError:
            log.error("aiohttp not installed")
            return "CLARIFY aiohttp not installed"
        except asyncio.TimeoutError:
            log.error("AgentCore fallback timeout (%.0fs)", self._cfg.timeout)
            return "CLARIFY cloud timeout"
        except Exception as exc:
            log.error("AgentCore fallback error: %s", exc)
            return f"CLARIFY cloud error: {exc}"

    async def health_check(self) -> bool:
        """Check if the AgentCore agent is reachable."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.url.replace("/invocations", "/health"),
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
