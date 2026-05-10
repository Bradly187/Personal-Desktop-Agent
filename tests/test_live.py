"""Live test against the running AgentCore dev server."""
import urllib.request
import json

URL = "http://localhost:8090/invocations"

def test(label, payload):
    print(f"\n--- {label} ---")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        body = resp.read().decode()
        print(f"Status: {resp.status}")
        print(f"Response: {body[:500]}")
    except Exception as e:
        print(f"Error: {e}")

# Test 1: Voice misrecognition
test("clothes the window (should be CLOSE)", {
    "command_text": "clothes the window",
    "source": "voice",
    "whisper_logprob": -0.8,
    "session_context": ["OPEN chrome", "CLICK address bar"],
})

# Test 2: Another misrecognition
test("scroll done (should be SCROLL down)", {
    "command_text": "scroll done",
    "source": "voice",
    "whisper_logprob": -0.5,
    "session_context": ["OPEN notepad", "TYPE hello"],
})

# Test 3: Gesture
test("POINT gesture (should be CLICK)", {
    "command_text": "POINT",
    "source": "gesture",
    "gesture_confidence": 0.7,
})

# Test 4: Correction
test("Record correction", {
    "prompt": "Record correction",
    "original_text": "oh pen notepad",
    "wrong_action": "TYPE pen notepad",
    "correct_action": "OPEN notepad",
    "actor_id": "desktop_user",
})
