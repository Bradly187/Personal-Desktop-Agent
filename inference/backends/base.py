from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Optional

from core.command_executor import Command

log = logging.getLogger(__name__)


class OllamaTimeoutError(Exception):
    """Raised (internally) when DA_OLLAMA_TIMEOUT_S fires..."""

# ---------------------------------------------------------------------------
# Fine-tuning data capture (task-local)
# ---------------------------------------------------------------------------
# (prompt, tokens_in, tokens_out) of the current task's most recent inference,
# read by HybridCoordinator._run_local()/_run_cloud() for the inferences row.
# A ContextVar, NOT backend instance attributes: one backend instance serves
# concurrent route() tasks (the scheduler runs ACCESSIBILITY/VOICE/GESTURE
# tiers concurrently), so instance attributes let task B's infer() overwrite
# task A's prompt between A's infer() returning and A's insert_inference()
# read — misattributing prompts and token counts across commands. infer()
# runs in the same task as the coordinator's read, so a ContextVar set inside
# infer() and read after `await infer()` cannot race.
_INFERENCE_CAPTURE: ContextVar[tuple[Optional[str], Optional[int], Optional[int]]] = \
    ContextVar("da_inference_capture", default=(None, None, None))


def set_inference_capture(
    prompt: Optional[str],
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
) -> None:
    """Record the current task's inference prompt and token counts."""
    _INFERENCE_CAPTURE.set((prompt, tokens_in, tokens_out))


def get_inference_capture() -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Return (prompt, tokens_in, tokens_out) set by the current task's last infer()."""
    return _INFERENCE_CAPTURE.get()


# ---------------------------------------------------------------------------
# Action vocabulary prompt fragment (shared by all backends)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a desktop control assistant. Convert the user's natural-language \
request into exactly ONE action from the following vocabulary. The angle \
brackets below mark placeholders — replace each with the actual value. Never \
output the brackets, the placeholder name, surrounding quotes, or an '=' sign.

CLICK <target>       — click a named UI element or coordinates
SCROLL <direction> [<amount>]  — scroll up/down/left/right
TYPE <text>          — type literal text
OPEN <app-or-file>   — open an application or file
CLOSE [<target>]     — close the active or named window
HOTKEY <key1> [<key2>...]  — press a key combination
DICTATE <text>       — paste text verbatim via clipboard
CLARIFY <question>   — ask the user to clarify; do not act
SCREENSHOT           — capture the desktop screen

Examples (output is the verb followed by the literal value only):
User: click the save button
Assistant: CLICK save button
User: scroll down three times
Assistant: SCROLL down 3
User: close this window
Assistant: CLOSE
User: type hello world
Assistant: TYPE hello world
User: open Chrome browser
Assistant: OPEN Chrome

Rules:
- Reply with ONLY the action string, nothing else.
- Do not explain or comment.
- Do not echo the placeholder notation: no <...>, no quotes, no '=' sign.
- If the request is ambiguous reply with CLARIFY followed by a short question.
- If the request matches no action reply with CLARIFY.
"""


def _build_prompt(
    cmd: Command,
    few_shot_examples: list[dict] | None = None,
    counterexamples: list[dict] | None = None,
) -> str:
    """Build the full prompt sent to the LLM."""
    parts = [_SYSTEM_PROMPT]

    if few_shot_examples:
        parts.append("\nExamples:")
        for ex in few_shot_examples:
            parts.append(f'User: {ex["command_text"]}\nAssistant: {ex["action_text"]}')

    if counterexamples:
        parts.append("\nDo NOT produce these responses:")
        for ex in counterexamples:
            parts.append(f'User: {ex["command_text"]} | Wrong: {ex["wrong_action"]}')

    if cmd.session_context:
        context = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
        parts.append(f"\nRecent commands:\n{context}")

    parts.append(f"\nUser: {cmd.text}\nAssistant:")
    return "\n".join(parts)


def _build_chat_messages(
    cmd: Command,
    few_shot_examples: list[dict] | None = None,
    counterexamples: list[dict] | None = None,
) -> list[dict]:
    """Build OpenAI/Ollama-style chat messages for the /api/chat tool path."""
    system_content = _SYSTEM_PROMPT
    if counterexamples:
        neg_block = "Avoid these incorrect mappings:\n" + "\n".join(
            f'"{ex["command_text"]}" -> NOT "{ex["wrong_action"]}"'
            for ex in counterexamples
        )
        system_content += "\n\n" + neg_block
    messages: list[dict] = [{"role": "system", "content": system_content}]
    if few_shot_examples:
        for ex in few_shot_examples:
            messages.append({"role": "user", "content": ex["command_text"]})
            messages.append({"role": "assistant", "content": ex["action_text"]})
    if cmd.session_context:
        ctx = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
        messages.append({"role": "user", "content": f"Recent commands:\n{ctx}"})
        messages.append({"role": "assistant", "content": "Understood."})
    messages.append({"role": "user", "content": cmd.text})
    return messages

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LocalInference(ABC):
    """Abstract LLM inference backend. All implementations are drop-in replacements."""

    @abstractmethod
    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
        counterexamples: list[dict] | None = None,
    ) -> str:
        """Run inference and return an action string (e.g. 'CLICK Save button').

        Args:
            cmd: The command to classify.
            few_shot_examples: Optional list of {'command_text', 'action_text'} dicts
                from ContinuousTrainer; injected into the prompt when provided.
            counterexamples: Optional list of {'command_text', 'wrong_action'} dicts
                injected as "do NOT" guidance from the few_shot_counterexamples store.
        """

    async def infer_stream(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ):
        """Stream inference tokens as they arrive (AsyncIterator[str]).

        Default implementation buffers the full response and yields it as a
        single token — safe for backends that don't support streaming.
        Override in subclasses (e.g. OllamaInference) for true token-by-token.

        Used by TTS paths (CLARIFY questions, DevAgent EXPLAIN responses) where
        starting audio synthesis before the full response is ready reduces latency.
        """
        result = await self.infer(cmd, few_shot_examples)
        yield result

    async def warmup(self) -> bool:
        """Best-effort pre-load of the backend's model so the first real command
        doesn't pay a cold-load penalty.

        Default no-op (returns False). Override where a cheap pre-load exists
        (OllamaInference). MUST never raise — callers fire-and-forget it at
        startup, off the 60 Hz loop.
        """
        return False

    @abstractmethod
    def get_status(self) -> dict:
        """Return a status dict: {'backend': str, 'available': bool, ...}."""

