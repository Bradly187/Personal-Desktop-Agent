"""LocalInference — abstract backend interface for on-device LLM inference."""

from inference.backends.base import (
    OllamaTimeoutError,
    LocalInference,
    _INFERENCE_CAPTURE,
    set_inference_capture,
    get_inference_capture,
    _SYSTEM_PROMPT,
    _build_prompt,
    _build_chat_messages,
)

from inference.backends.ollama import (
    OllamaInference,
    ensure_ollama_running,
    _ollama_alive,
    _find_ollama_exe,
    _ACTION_VERBS,
    _DESKTOP_ACTION_TOOL,
    _action_from_tool_call,
    _recover_action_from_content,
)

from inference.backends.vllm import VLLMInference
from inference.backends.vllm_server import VLLMServerInference
from inference.backends.vllm_embedder import VLLMEmbedder

from inference.backends.llamacpp import (
    LlamaCppInference,
    _build_user_content,
)

__all__ = [
    "OllamaTimeoutError",
    "LocalInference",
    "_INFERENCE_CAPTURE",
    "set_inference_capture",
    "get_inference_capture",
    "_SYSTEM_PROMPT",
    "_build_prompt",
    "_build_chat_messages",
    "OllamaInference",
    "ensure_ollama_running",
    "_ollama_alive",
    "_find_ollama_exe",
    "_ACTION_VERBS",
    "_DESKTOP_ACTION_TOOL",
    "_action_from_tool_call",
    "_recover_action_from_content",
    "VLLMInference",
    "VLLMServerInference",
    "VLLMEmbedder",
    "LlamaCppInference",
    "_build_user_content",
]
