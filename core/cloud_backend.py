"""Cloud LLM backend — Amazon Bedrock (Claude models) only.

Both cloud consumers — the command-path ``_CloudInference`` (Haiku 4.5) and the
dev-path ``CloudDevAgent`` (Opus 4.8) — build their client through here, so the
backend is constructed in exactly one place. There is a single backend: Amazon
Bedrock. The direct first-party Anthropic API path was removed — this project
accesses Claude **exclusively** through Bedrock, so there is no
``ANTHROPIC_API_KEY`` dependency anywhere in the cloud path.

Credential: ``AWS_BEARER_TOKEN_BEDROCK`` (an Amazon Bedrock API key). A missing
credential degrades to a clear, actionable CLARIFY at the call site rather than a
raw SDK "Could not resolve authentication method" traceback at request time.

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
    """Resolved Amazon Bedrock backend: how to build the client and map model ids."""

    name: str                          # "bedrock:us-east-1"
    aws_region: str
    api_key: str                       # Bedrock bearer token (AWS_BEARER_TOKEN_BEDROCK)
    profile_prefix: str = ""

    def map_model(self, model: str) -> str:
        """First-party model alias → Bedrock inference-profile id.

        Base id from the table (fallback: ``anthropic.``-prefix the alias) plus
        the cross-region profile prefix.
        """
        base = _BEDROCK_MODEL_BASE.get(model)
        if base is None:
            base = model if model.startswith("anthropic.") else f"anthropic.{model}"
        return f"{self.profile_prefix}.{base}" if self.profile_prefix else base

    def make_client(self, *, async_: bool = False, timeout: float | None = None):
        """Construct the AnthropicBedrock SDK client for this backend (lazy import)."""
        import anthropic
        cls = anthropic.AsyncAnthropicBedrock if async_ else anthropic.AnthropicBedrock
        kwargs: dict = {"aws_region": self.aws_region, "api_key": self.api_key}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return cls(**kwargs)


def credential_available() -> bool:
    """True if an Amazon Bedrock credential is configured (no network call)."""
    return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))


def resolve_backend() -> CloudBackend:
    """Resolve the Amazon Bedrock backend, or raise an actionable ``RuntimeError``.

    The raised message names the exact env var to set so a missing credential
    degrades to a clear CLARIFY rather than a raw SDK traceback at request time.
    """
    key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not key:
        raise RuntimeError(
            "AWS_BEARER_TOKEN_BEDROCK not set — the cloud fallback is disabled. "
            "Set your Amazon Bedrock API key (User scope) with: "
            'setx AWS_BEARER_TOKEN_BEDROCK "<key>" then restart the agent. '
            "This project accesses Claude models exclusively through Amazon Bedrock."
        )
    region = _bedrock_region()
    return CloudBackend(
        name=f"bedrock:{region}",
        aws_region=region,
        api_key=key,
        profile_prefix=_profile_prefix(),
    )
