"""AgentCore Fallback Agent — Cloud intelligence for ambiguous commands.

This agent is deployed to AWS Bedrock AgentCore and called by the
HybridCoordinator when local inference can't confidently resolve a command.

Memory integration:
  - STM: Remembers recent commands within a session for context continuity
  - LTM (User Preference): Learns the user's common misrecognitions and
    preferred interpretations over time
  - LTM (Semantic): Stores facts about the user's desktop environment
    (installed apps, common workflows, window names)

It receives:
  - The ambiguous command text
  - Session context (last N successful commands)
  - Source modality (voice, gesture, etc.)
  - Confidence scores

It returns a structured action string from the desktop control vocabulary.
"""

import os
from datetime import datetime
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
log = app.logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Action vocabulary (mirrors local_inference.py)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a desktop control assistant for a user with rheumatoid arthritis.
The user controls their PC through voice commands, hand gestures, eye gaze,
and iPad touch input. Commands may be ambiguous due to low transcription
confidence or unclear gesture recognition.

Your job: convert the user's natural-language request into exactly ONE action
from the following vocabulary:

CLICK <target>                  — click a named UI element or coordinates
SCROLL <direction> [<amount>]   — scroll up/down/left/right (default: 3)
TYPE <text>                     — type literal text
OPEN <app-or-file>              — open an application or file
CLOSE [<target>]                — close the active or named window
HOTKEY <key1> [<key2>...]       — press a key combination (e.g. HOTKEY ctrl c)
DICTATE <text>                  — paste text verbatim via clipboard
CLARIFY <question>              — ask the user to clarify if truly ambiguous
SCREENSHOT                      — capture the desktop screen

Rules:
- Reply with ONLY the action string on a single line. No explanation.
- Use session context AND your memory of past interactions to disambiguate.
- Prefer action over CLARIFY — only clarify if genuinely impossible to infer.
- The user has limited mobility; assume they want the simplest interpretation.
- If you remember this user's past corrections or preferences, apply them.
- Common voice misrecognitions to handle:
  "clothes" → CLOSE, "type" → TYPE, "scroll done" → SCROLL down,
  "hot key" → HOTKEY, "open" vs "oh pen" → OPEN
- If the source is "gesture" and text is a gesture name (POINT, FIST, PALM),
  map to the most likely action: POINT→CLICK, FIST→CLOSE, PALM→SCROLL up.
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def resolve_ambiguous_command(
    command_text: str,
    source: str = "voice",
    whisper_logprob: float = 0.0,
    gesture_confidence: float = 1.0,
    session_context: list[str] | None = None,
) -> str:
    """Resolve an ambiguous desktop command into a structured action.

    Args:
        command_text: The raw transcribed or detected command text.
        source: Input modality (voice, gesture, gaze_dwell, multimodal, etc.)
        whisper_logprob: Whisper transcription confidence (0=perfect, negative=worse).
        gesture_confidence: Gesture detection confidence (0-1).
        session_context: List of last N successful action strings for context.

    Returns:
        A structured action string like "CLICK Save button" or "SCROLL down 5".
    """
    context_str = ""
    if session_context:
        context_str = "\nRecent successful commands:\n" + "\n".join(
            f"  - {c}" for c in session_context[-10:]
        )

    return (
        f"Command: {command_text}\n"
        f"Source: {source}\n"
        f"Whisper confidence: {whisper_logprob}\n"
        f"Gesture confidence: {gesture_confidence}"
        f"{context_str}"
    )


@tool
def get_action_vocabulary() -> str:
    """Return the full action vocabulary with examples for reference."""
    return """
Action Vocabulary:
  CLICK <target>        — e.g. "CLICK Save button", "CLICK 500 300"
  SCROLL <dir> [n]      — e.g. "SCROLL down", "SCROLL up 5"
  TYPE <text>           — e.g. "TYPE hello world"
  OPEN <app>            — e.g. "OPEN notepad", "OPEN chrome"
  CLOSE [target]        — e.g. "CLOSE", "CLOSE chrome"
  HOTKEY <keys>         — e.g. "HOTKEY ctrl c", "HOTKEY alt f4"
  DICTATE <text>        — e.g. "DICTATE Hello, how are you?"
  CLARIFY <question>    — e.g. "CLARIFY Did you mean close or scroll?"
  SCREENSHOT            — no arguments needed

Common gesture mappings:
  POINT → CLICK (at gaze coordinates if available)
  FIST  → CLOSE
  PALM  → SCROLL up (stop gesture)
  SWIPE_LEFT → HOTKEY alt left (back)
  SWIPE_RIGHT → HOTKEY alt right (forward)
"""


@tool
def record_correction(
    original_text: str,
    wrong_action: str,
    correct_action: str,
) -> str:
    """Record a user correction so the agent learns from mistakes.

    This is stored in long-term memory and used to improve future
    disambiguation for similar inputs.

    Args:
        original_text: The original ambiguous command text.
        wrong_action: The action that was incorrectly chosen.
        correct_action: The correct action the user wanted.

    Returns:
        Confirmation that the correction was recorded.
    """
    return (
        f"Correction recorded:\n"
        f"  Input: \"{original_text}\"\n"
        f"  Wrong: {wrong_action}\n"
        f"  Correct: {correct_action}\n"
        f"This will be remembered for future similar commands."
    )


# ---------------------------------------------------------------------------
# Memory setup helper
# ---------------------------------------------------------------------------

def _create_session_manager(session_id: str, actor_id: str):
    """Create an AgentCore Memory session manager if memory is configured."""
    if not MEMORY_ID:
        return None

    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        config = AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
        )

        session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=config,
            region_name=AWS_REGION,
        )
        return session_manager

    except ImportError as exc:
        log.warning("Memory packages not available: %s", exc)
        return None
    except Exception as exc:
        log.error("Failed to create session manager: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Correction handling — integrated into main entrypoint
# ---------------------------------------------------------------------------

def _handle_correction(payload, session_manager):
    """Handle a correction recording request."""
    original_text = payload.get("original_text", "")
    wrong_action = payload.get("wrong_action", "")
    correct_action = payload.get("correct_action", "")

    agent_kwargs = {
        "system_prompt": (
            "You are recording a user correction for a desktop control agent. "
            "Store this correction in memory so future similar commands are "
            "resolved correctly. Acknowledge the correction briefly."
        ),
        "tools": [record_correction],
    }
    if session_manager:
        agent_kwargs["session_manager"] = session_manager

    agent = Agent(**agent_kwargs)

    prompt = (
        f"The user corrected a disambiguation:\n"
        f"  Original input: \"{original_text}\"\n"
        f"  Wrong action: {wrong_action}\n"
        f"  Correct action: {correct_action}\n\n"
        f"Record this correction using the record_correction tool, "
        f"then confirm it was stored."
    )

    return agent(prompt)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@app.entrypoint
async def invoke(payload, context):
    """Handle incoming disambiguation requests or correction recordings.

    Disambiguation payload:
    {
        "prompt": "resolve this command",
        "command_text": "clothes the window",
        "source": "voice",
        "whisper_logprob": -0.8,
        "gesture_confidence": 1.0,
        "session_context": ["OPEN chrome", "CLICK address bar", "TYPE google.com"],
        "session_id": "session_abc123",
        "actor_id": "desktop_user"
    }

    Correction payload:
    {
        "prompt": "Record correction",
        "original_text": "clothes the window",
        "wrong_action": "CLICK clothes",
        "correct_action": "CLOSE",
        "session_id": "corrections",
        "actor_id": "desktop_user"
    }

    Returns the resolved action string or correction confirmation.
    """
    session_id = payload.get(
        "session_id",
        getattr(context, 'session_id', f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    )
    actor_id = payload.get("actor_id", "desktop_user")

    # Set up memory (if configured)
    session_manager = _create_session_manager(session_id, actor_id)

    # Detect if this is a correction request
    prompt_text = payload.get("prompt", "")
    if "correction" in prompt_text.lower() and payload.get("original_text"):
        result = _handle_correction(payload, session_manager)
        yield str(result).strip()
        return

    # --- Normal disambiguation flow ---

    # Extract command details from payload
    command_text = payload.get("command_text", payload.get("prompt", ""))
    source = payload.get("source", "voice")
    whisper_logprob = payload.get("whisper_logprob", 0.0)
    gesture_confidence = payload.get("gesture_confidence", 1.0)
    session_context = payload.get("session_context", [])

    # Build the disambiguation prompt
    context_block = ""
    if session_context:
        context_block = "\n\nRecent successful commands:\n" + "\n".join(
            f"- {c}" for c in session_context[-10:]
        )

    user_prompt = (
        f"Resolve this ambiguous command into a single action string.\n\n"
        f"Input: \"{command_text}\"\n"
        f"Source modality: {source}\n"
        f"Transcription confidence (logprob): {whisper_logprob}\n"
        f"Gesture confidence: {gesture_confidence}"
        f"{context_block}\n\n"
        f"Reply with ONLY the action string."
    )

    # Create agent with memory and tools
    agent_kwargs = {
        "system_prompt": SYSTEM_PROMPT,
        "tools": [resolve_ambiguous_command, get_action_vocabulary, record_correction],
    }
    if session_manager:
        agent_kwargs["session_manager"] = session_manager

    agent = Agent(**agent_kwargs)

    # Get the response
    result = agent(user_prompt)
    response_text = str(result).strip()

    # Extract just the action line (first non-empty line)
    lines = [l.strip() for l in response_text.splitlines() if l.strip()]
    action = lines[0] if lines else "CLARIFY unable to resolve"

    yield action


if __name__ == "__main__":
    app.run()
