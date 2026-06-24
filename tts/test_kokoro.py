import os
import sys

# Ensure repo root is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)

from tts.polly_stream import get_client

def test_kokoro():
    print("Testing Kokoro TTS via get_client()...")
    
    # This should now pull from approval_config.json which we set to "kokoro"
    client = get_client()
    
    print(f"Loaded client: {client.__class__.__name__}")
    
    status = client.get_status()
    print("Status:", status)

    text = "Hello there! This is a test of the Kokoro Text to Speech engine running entirely on your local CPU. It sounds very natural, doesn't it?"
    
    print(f"Speaking: {text}")
    success = client.speak_sync(text)
    
    print("Playback success:", success)

if __name__ == "__main__":
    test_kokoro()
