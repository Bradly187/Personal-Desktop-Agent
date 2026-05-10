# AgentCore Fallback Agent (with Memory)

Cloud-based disambiguation agent for the Desktop Accessibility Agent.
Called by `HybridCoordinator` when local inference can't confidently resolve a command.

**Memory-enabled**: learns from past interactions and user corrections to improve
disambiguation accuracy over time.

## Architecture

```
iPad → WebSocket → PC Bridge → HybridCoordinator
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              Gate 2 fail     Gate 3 fail     Gate 4 fail
              (complexity)    (VRAM full)     (latency high)
                    │               │               │
                    └───────────────┼───────────────┘
                                    ▼
                      AgentCore Fallback Agent (AWS)
                      ┌─────────────────────────────┐
                      │  Strands Agent + Bedrock LLM │
                      │         ┌─────────┐         │
                      │         │ Memory  │         │
                      │         ├─────────┤         │
                      │  STM:   │ session │ (recent commands)
                      │  LTM:   │ prefs   │ (learned corrections)
                      │  LTM:   │ facts   │ (desktop environment)
                      │         └─────────┘         │
                      └─────────────────────────────┘
                                    │
                                    ▼
                         Structured Action String
                                    │
                                    ▼
                         MCP Server → Desktop
```

## Memory Strategies

| Strategy | What It Stores | How It Helps |
|----------|---------------|--------------|
| **STM** (Short-Term) | Recent commands in this session | Context for multi-step tasks |
| **User Preference** | Learned corrections & patterns | "clothes" always means CLOSE for this user |
| **Semantic** | Desktop environment facts | Knows which apps are installed, window names |

### Learning Loop

```
1. User says "clothes the window"
2. Agent resolves → "CLOSE" (using memory of past corrections)
3. If wrong, user corrects → invoke() detects correction payload and stores it
4. Next time "clothes" appears → memory retrieves the correction
5. Agent gets better over time without retraining
```

> **Note**: Corrections are handled by the same `invoke` entrypoint as disambiguation.
> The agent detects a correction request when the payload contains `"correction"` in the
> prompt text and an `original_text` field. There is no separate named entrypoint.

## Local Development

```bash
cd agentcore_fallback

# Install dependencies
pip install -e .

# Start the dev server (no memory in local mode)
agentcore dev

# Test disambiguation
agentcore invoke --dev '{
    "command_text": "clothes the window",
    "source": "voice",
    "whisper_logprob": -0.8,
    "session_context": ["OPEN chrome", "CLICK address bar"]
}'
# Expected: "CLOSE"

# Test correction recording
agentcore invoke --dev '{
    "prompt": "Record correction",
    "original_text": "scroll done",
    "wrong_action": "SCROLL done",
    "correct_action": "SCROLL down"
}'
```

## Deployment (with Memory)

### Step 1: Deploy without memory first

```bash
cd agentcore_fallback
aws login
agentcore deploy
agentcore invoke '{"command_text": "oh pen notepad", "source": "voice"}'
# Verify it returns "OPEN notepad"
```

### Step 2: Create memory resource

```bash
agentcore memory create desktop_fallback_memory \
    --description "Disambiguation memory for desktop accessibility agent" \
    --strategies '[
        {
            "userPreferenceMemoryStrategy": {
                "name": "CorrectionLearner",
                "namespaces": ["/corrections/{actorId}"]
            }
        },
        {
            "semanticMemoryStrategy": {
                "name": "DesktopFacts",
                "namespaces": ["/desktop/{actorId}"]
            }
        }
    ]' \
    --region us-east-1 \
    --wait
```

Save the returned memory ID.

### Step 3: Configure memory

```bash
# Set the memory ID as an environment variable for the agent
# Edit .bedrock_agentcore.yaml:
#   memory.mode: STM_AND_LTM
#   memory.memory_id: <your-memory-id>
```

Or set via environment:
```bash
export AGENTCORE_MEMORY_ID=<your-memory-id>
```

### Step 4: Redeploy with memory

Update `.bedrock_agentcore.yaml`:
```yaml
memory:
  mode: STM_AND_LTM
  memory_id: <your-memory-id>
  memory_name: desktop_fallback_memory
  event_expiry_days: 90
```

```bash
agentcore deploy
```

### Step 5: Test memory persistence

```bash
# Teach it a correction
agentcore invoke '{
    "prompt": "Record correction",
    "original_text": "scroll done",
    "wrong_action": "TYPE done",
    "correct_action": "SCROLL down",
    "actor_id": "desktop_user"
}'

# Later, test if it remembers
agentcore invoke '{
    "command_text": "scroll done",
    "source": "voice",
    "whisper_logprob": -0.5,
    "actor_id": "desktop_user"
}'
# Should return "SCROLL down" (learned from correction)
```

## Integration with HybridCoordinator

```python
from agentcore_fallback.client import AgentCoreFallbackClient, FallbackConfig

# For local testing (dev server, no memory)
client = AgentCoreFallbackClient()

# For deployed agent with memory
client = AgentCoreFallbackClient(FallbackConfig(
    use_dev=False,
    deployed_url="https://your-agent-endpoint/invocations",
    actor_id="desktop_user",
))

# Resolve a command
action = await client.resolve(cmd)

# Record a correction (teaches the agent for next time)
await client.record_correction(
    original_text="clothes the window",
    wrong_action="CLICK clothes",
    correct_action="CLOSE"
)
```

## Memory Management

```bash
# List all memories
agentcore memory list --region us-east-1

# Check memory status
agentcore memory get <memory-id> --region us-east-1

# Delete memory (WARNING: permanent, loses all learned corrections)
agentcore memory delete <memory-id> --region us-east-1 --wait
```
