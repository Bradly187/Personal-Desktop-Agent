"""Cloud LLM backend selection — direct Anthropic API vs Amazon Bedrock.

Both cloud consumers — the command-path ``_CloudInference`` (Haiku 4.5) and the
dev-path ``CloudDevAgent`` (Opus 4.8) — build their client through here, so the
backend choice is made in exactly one place.

Backend selection
-----------------
- If an **Amazon Bedrock API key** is configured (``AWS_BEARER_TOKEN_BEDROCK``)
  and ``DA_CLOUD_BACKEND`` is not forced to ``anthropic``, route through Amazon
  Bedrock. ``DA_CLOUD_BACKEND=bedrock`` forces it on; ``=anthropic`` forces off.
- Otherwise use the first-party Anthropic API (``ANTHROPIC_API_KEY``).

Bedrock path — classic InvokeModel via AnthropicBedrock
-------------------------------------------------------
Uses the ``anthropic`` SDK's ``AnthropicBedrock`` / ``AsyncAnthropicBedrock``
client (the ``bedrock-runtime`` InvokeModel backend), which reads the Bedrock
API key from ``AWS_BEARER_TOKEN_BEDROCK`` (also passed explicitly as ``api_key``)
and signs requests for the given ``aws_region``. The request shape
(``messages.create`` / ``.stream``) is identical to the first-party client.

Bedrock requires a **cross-region inference-profile** model id, not the bare
model id — e.g. ``us.anthropic.claude-haiku-4-5-20251001-v1:0``. The newer
models dropped the date/version suffix (``us.anthropic.claude-opus-4-8``); Haiku
4.5 still carries one. ``map_model`` applies the right base id + region prefix.
The prefix is ``DA_BEDROCK_PROFILE_PREFIX`` (default ``us``; ``global`` has no
regional pricing premium and broadest availability). The base ids below were
confirmed ACTIVE via ``bedrock:list_inference_profiles`` in us-east-1.

(The newer "Claude in Amazon Bedrock" Mantle Messages endpoint exists too, but a
standard Bedrock API key with ``AmazonBedrockLimitedAccess`` returns "not
available for this account" there, so this uses the classic InvokeModel path.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Region used when neither DA_BEDROCK_REGION nor AWS_REGION is set.
_BEDROCK_REGION_DEFAULT = "us-east-1"

# First-party alias → Bedrock base model id (no region prefix). Newer models
# have no date/version suffix; older ones (Haiku 4.5) do.
_BEDROCK_MODEL_BASE = {
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-opus-4-8":  "anthropic.claude-opus-4-8",
    "claude-opus-4-7":  "anthropic.claude-opus-4-7",
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
    "claude-fable-5":   "anthropic.claude-fable-5",
}


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


def _profile_prefix() -> str:
    # Cross-region inference-profile prefix: "us" (confirmed working, 10% regional
    # premium) or "global" (no premium, broadest availability). Empty = bare base
    # id (rarely valid for on-demand on newer models).
    return os.environ.get("DA_BEDROCK_PROFILE_PREFIX", "us").strip().lower()


@dataclass
class CloudBackend:
    """Resolved cloud backend: how to build the client and map model ids."""

    name: str                          # "bedrock:us-east-1" | "anthropic"
    is_bedrock: bool = False
    aws_region: str | None = None
    api_key: str | None = None         # bearer token (Bedrock) or explicit key (direct)
    profile_prefix: str = ""

    def map_model(self, model: str) -> str:
        """First-party model id → backend-specific id.

        Direct Anthropic: unchanged. Bedrock: base id from the table (fallback:
        ``anthropic.``-prefix the alias) + the cross-region profile prefix.
        """
        if not self.is_bedrock:
            return model
        base = _BEDROCK_MODEL_BASE.get(model)
        if base is None:
            base = model if model.startswith("anthropic.") else f"anthropic.{model}"
        return f"{self.profile_prefix}.{base}" if self.profile_prefix else base

    def make_client(self, *, async_: bool = False, timeout: float | None = None):
        """Construct the SDK client for this backend (lazy import)."""
        import anthropic
        if self.is_bedrock:
            cls = anthropic.AsyncAnthropicBedrock if async_ else anthropic.AnthropicBedrock
            kwargs: dict = {"aws_region": self.aws_region, "api_key": self.api_key}
            if timeout is not None:
                kwargs["timeout"] = timeout
            return cls(**kwargs)
        cls = anthropic.AsyncAnthropic if async_ else anthropic.Anthropic
        kwargs = {}
        if self.api_key:               # explicit override; else SDK resolves env
            kwargs["api_key"] = self.api_key
        if timeout is not None:
            kwargs["timeout"] = timeout
        return cls(**kwargs)


def credential_available() -> bool:
    """True if a usable cloud credential is configured (no network call)."""
    if bedrock_selected():
        return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def resolve_backend(api_key: str | None = None) -> CloudBackend:
    """Resolve the active cloud backend, or raise an actionable ``RuntimeError``.

    ``api_key`` is an explicit override for the *direct Anthropic* path (e.g. a
    key passed to ``CloudDevAgent(api_key=...)``); it is ignored when Bedrock is
    selected (the Bedrock bearer token from the environment wins). The raised
    message names the exact env var to set so a missing credential degrades to a
    clear CLARIFY rather than a raw SDK traceback at request time.
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
            is_bedrock=True,
            aws_region=region,
            api_key=key,
            profile_prefix=_profile_prefix(),
        )

    if not (api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — the cloud fallback is disabled. Set it "
            '(User scope) with: setx ANTHROPIC_API_KEY "sk-ant-..." then restart the '
            "agent, or set AWS_BEARER_TOKEN_BEDROCK to use your Amazon Bedrock API key. "
            "Note: ANTHROPIC_API_KEY is a separate pay-as-you-go key from "
            "console.anthropic.com, not your Claude Max subscription."
        )
    return CloudBackend(name="anthropic", is_bedrock=False, api_key=api_key)
