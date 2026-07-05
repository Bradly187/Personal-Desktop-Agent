"""Simulated iPad client -- Phase 1 end-to-end test

Sends a sequence of touch_command and trackpad messages to the bridge
and verifies each gets an 'ack' response with status 'ok'.

Run the bridge first:
    python ipad_bridge.py --no-mdns

Then in another terminal:
    python tests/test_bridge_client.py

Exit codes:
    0 -- all tests passed
    1 -- one or more tests failed
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp not installed. Run: pip install aiohttp")
    sys.exit(1)

# Read the bridge pairing token (C1) so this manual client can authenticate.
# Override with the DA_BRIDGE_TOKEN env var if the bridge runs on another machine.
def _bridge_token() -> str:
    import os
    from pathlib import Path
    tok = os.environ.get("DA_BRIDGE_TOKEN")
    if tok:
        return tok.strip()
    path = Path.home() / ".claude" / "ipad_bridge" / "paired_token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


BRIDGE_URL = f"ws://localhost:8765/ws?token={_bridge_token()}"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

TEST_MESSAGES = [
    {
        "id": "t1",
        "type": "touch_command",
        "action": "SCROLL",
        "params": {"direction": "down", "amount": 3},
        "description": "Scroll down 3 clicks",
    },
    {
        "id": "t2",
        "type": "touch_command",
        "action": "HOTKEY",
        "params": {"keys": ["win", "d"]},
        "description": "Show desktop (Win+D)",
    },
    {
        "id": "t3",
        "type": "touch_command",
        "action": "HOTKEY",
        "params": {"keys": ["win", "d"]},
        "description": "Restore windows (Win+D again)",
    },
    {
        "id": "t4",
        "type": "trackpad",
        "event": "move",
        "dx": 50,
        "dy": 0,
        "description": "Trackpad move cursor right 50px",
        "expect_ack": False,
    },
    {
        "id": "t5",
        "type": "trackpad",
        "event": "scroll",
        "direction": "up",
        "clicks": 2,
        "description": "Trackpad scroll up",
        "expect_ack": False,
    },
    {
        "id": "t6",
        "type": "touch_command",
        "action": "CLARIFY",
        "params": {"message": "Test clarify -- no desktop action"},
        "description": "CLARIFY (no-op, should still ack ok)",
    },
    {
        "id": "t7",
        "type": "touch_command",
        "action": "SCREENSHOT",
        "params": {},
        "description": "SCREENSHOT — full desktop capture",
    },
    {
        "id": "t8",
        "type": "touch_command",
        "action": "DICTATE",
        "text": "sin(π/4) + √2 ≈ 2.121",
        "params": {},
        "description": "DICTATE with unicode math — clipboard paste path",
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    passed = 0
    failed = 0

    print(f"Connecting to {BRIDGE_URL} ...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(BRIDGE_URL) as ws:
                print("Connected.\n")

                for test in TEST_MESSAGES:
                    desc = test["description"]
                    msg_id = test["id"]
                    expect_ack = test.get("expect_ack", True)
                    payload = {k: v for k, v in test.items()
                               if k not in ("description", "expect_ack")}

                    print(f"  [{msg_id}] {desc}")
                    print(f"        > sending: {json.dumps(payload)}")

                    await ws.send_json(payload)
                    start = time.perf_counter()

                    # Trackpad events don't send acks (high-frequency, fire-and-forget)
                    if not expect_ack:
                        await asyncio.sleep(0.2)
                        elapsed = (time.perf_counter() - start) * 1000
                        print(f"        < (no ack expected — fire-and-forget)  ({elapsed:.0f} ms)")
                        passed += 1
                        print()
                        continue

                    # Collect ack (and any status message that follows)
                    ack_received = False
                    deadline = start + 5.0  # 5s timeout per message

                    while time.perf_counter() < deadline:
                        try:
                            reply = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                        except asyncio.TimeoutError:
                            break

                        rtype = reply.get("type")
                        if rtype == "ack" and reply.get("id") == msg_id:
                            elapsed = (time.perf_counter() - start) * 1000
                            status = reply.get("status", "?")
                            error = reply.get("error", "")
                            if status in ("ok", "ignored"):
                                print(f"        < ack: {status}  ({elapsed:.0f} ms)")
                                passed += 1
                            else:
                                print(f"        < FAIL: status={status!r}  error={error!r}")
                                failed += 1
                            ack_received = True
                        elif rtype == "screenshot":
                            size = len(reply.get("image", ""))
                            print(f"        < screenshot: {size} base64 chars")
                        elif rtype == "status":
                            win = reply.get("active_window", "?")
                            cur = reply.get("cursor", {})
                            print(f"        < status: window={win!r}  cursor=({cur.get('x')},{cur.get('y')})")

                        if ack_received:
                            break

                    if not ack_received:
                        print("        < TIMEOUT -- no ack received within 5s")
                        failed += 1

                    print()
                    await asyncio.sleep(0.3)  # brief pause between commands

    except aiohttp.ClientConnectorError:
        print(f"\nERROR: Could not connect to {BRIDGE_URL}")
        print("Make sure the bridge is running:  python ipad_bridge.py --no-mdns")
        return 1

    print("-" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(run_tests()))


if __name__ == "__main__":
    main()
