"""Cloud LLM backend selection — direct Anthropic API vs Amazon Bedrock.

Both cloud consumers — the command-path ``_CloudInference`` (Haiku 4.5) and the
dev-path ``CloudDevAgent`` (Opus 4.8) — build their client through here, so the
backend choice is made in exactly one place.

Backend selection
-----------------
- If an **Amazon Bedrock API key** is configured (``AWS_BEARER_TOKEN_BEDROCK``)
  and ``DA_CLOUD_BACKEND`` is not forced to ``anthropic``, route through
  **Claude in Amazon Bedrock** (the Mantle Messages endpoint) using bearer-token
  auth. ``DA_CLOUD_BACKEND=bedrock`` forces it on; ``=anthropic`` forces it off.
- Otherwise use the first-party Anthropic API (``ANTHROPIC_API_KEY``).

Why the standard client + ``base_url`` for Bedrock
--------------------------------------------------
Claude in Amazon Bedrock serves the *same* Messages API shape at
``https://bedrock-mantle.{region}.api.aws/anthropic``. Anthropic documents a
bearer-token path that uses the ordinary ``Anthropic`` / ``AsyncAnthropic``
client with ``base_url`` set and the Bedrock API key passed as ``api_key`` (sent
as the ``x-api-key`` header). That keeps the request shape
(``client.messages.create`` / ``.stream``) identical on both backends — no
SigV4, no second client class, no per-call-site branching. Bedrock model IDs
carry an ``anthropic.`` provider prefix and **no** ARN version suffix
(``anthropic.claude-haiku-4-5``, ``anthropic.claude-opus-4-8``); ``map_model``
applies that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Region used when neither DA_BEDROCK_REGION nor AWS_REGION is set.
_BEDROCK_REGION_DEFAULT = "us-east-1"


@dataclass
class CloudBackend:
    """Resolved cloud backend: how to build the client and map model IDs."""

    name: str                                   # "bedrock:us-east-1" | "anthropic"
    client_kwargs: dict = field(default_factory=dict)  # → Anthropic()/AsyncAnthropic()
    is_bedrock: bool = False

    def map_model(self, model: str) -> str:
        """First-party model id → backend-specific id.

        Direct Anthropic: unchanged. Bedrock (Mantle): ensure the ``anthropic.``
        provider prefix (``claude-opus-4-8`` → ``anthropic.claude-opus-4-8``).
        """
        if not self.is_bedrock:
            return model
        return model if model.startswith("anthropic.") else f"anthropic.{model}"


def bedrock_selected() -> bool:
    """Whether the Amazon Bedrock backend is the active choice (no network call)."""
    forced = os.environ.get("DA_CLOUD_BACKEND", "").strip().lower()
    if forced == "anthropic":
        return False
    if forced == "bedrock":
        return True
    # Auto: a configured Bedrock API key opts in.
    return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))


def _bedrock_region() -> str:
    return (
        os.environ.get("DA_BEDROCK_REGION")
        or os.environ.get("AWS_REGION")
        or _BEDROCK_REGION_DEFAULT
    )


def credential_available() -> bool:
    """True if a usable cloud credential is configured (no network call).

    Lets callers report availability honestly in ``get_status()`` without
    constructing a client.
    """
    if bedrock_selected():
        return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def resolve_backend() -> CloudBackend:
    """Resolve the active cloud backend, or raise an actionable ``RuntimeError``.

    The message names the exact env var to set so a missing credential degrades
    to a clear CLARIFY rather than a raw SDK ``Could not resolve authentication
    method`` traceback at request time.
    """
    if bedrock_selected():
        key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if not key:
            raise RuntimeError(
                "Amazon Bedrock selected (DA_CLOUD_BACKEND=bedrock) but "
                "AWS_BEARER_TOKEN_BEDROCK is not set. Set your Bedrock API key with: "
                'setx AWS_BEARER_TOKEN_BEDROCK "<key>" then restart the agent.'
            )
        region = _bedrock_region()
        return CloudBackend(
            name=f"bedrock:{region}",
            client_kwargs={
                "api_key": key,
                "base_url": f"https://bedrock-mantle.{region}.api.aws/anthropic",
            },
            is_bedrock=True,
        )

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — the cloud fallback is disabled. Set it "
            '(User scope) with: setx ANTHROPIC_API_KEY "sk-ant-..." then restart the '
            "agent, or set AWS_BEARER_TOKEN_BEDROCK to use your Amazon Bedrock API key. "
            "Note: ANTHROPIC_API_KEY is a separate pay-as-you-go key from "
            "console.anthropic.com, not your Claude Max subscription."
        )
    return CloudBackend(name="anthropic", client_kwargs={}, is_bedrock=False)
