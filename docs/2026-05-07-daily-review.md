# Daily Review — 2026-05-07

## Yesterday's Work (2026-05-06)

### What Was Built

Phase 1 of the Personal Desktop Agent was implemented — the PC-side bridge and MCP server infrastructure that connects an iPad (via WebSocket) to Windows desktop control.

#### New Files

| File | Purpose |
|------|---------|
| `ipad_bridge.py` | `aiohttp` WebSocket server on `:8765`; routes 11 message types from iPad app to `CommandExecutor` or direct pyautogui |
| `command_executor.py` | Maps 8-verb action vocabulary (CLICK/SCROLL/TYPE/OPEN/CLOSE/HOTKEY/DICTATE/CLARIFY) to MCP tool calls |
| `mcp_server/desktop_mcp_server.py` | Standalone MCP server exposing desktop control to Claude (stdio transport); supports `SAFE_MODE=1` |
| `mcp_server/tools/mouse.py` | pyautogui wrappers: move, click, double-click, scroll, drag |
| `mcp_server/tools/keyboard.py` | pyautogui wrappers: type, hotkey, press |
| `mcp_server/tools/screen.py` | mss screenshot, screen size, pytesseract OCR text-finder |
| `mcp_server/tools/windows.py` | win32gui/psutil wrappers: active window, list windows, focus window |
| `tests/test_bridge_client.py` | Simulated iPad client; sends 6 test messages and verifies `ack` responses |
| `requirements.txt` | PC-side Python dependencies (unpinned — task 4.5 will pin versions) |

#### New Spec Diagrams

| File | Description |
|------|-------------|
| `.kiro/specs/ipad-sensor-focus/diagrams/07-bridge-architecture.md` | Mermaid flowchart of iPad↔Bridge↔MCP↔pyautogui stack |
| `.kiro/specs/ipad-sensor-focus/diagrams/08-bridge-message-routing.md` | Full message routing flowchart (11 types, 8 action verbs) |
| `.kiro/specs/ipad-sensor-focus/diagrams/09-bridge-sequence.md` | Sequence diagram: touch_command and trackpad end-to-end |

#### Updated Files

- `.claude/launch.json` — added `ipad-bridge` launch configuration
- `.kiro/steering/tech.md` / `product.md` / `structure.md` — steering docs kept current
- `.kiro/hooks/sync-docs-on-change.kiro.hook` — hook wiring

---

## Housekeeping (2026-05-07)

### Bugs Fixed

#### 1. `mouse.py` — `mouse_drag` crash (critical, runtime `TypeError`)

`pyautogui.drag()` does not accept `startX`/`startY` kwargs; passing them raises `TypeError` at call time.

**Before:**
```python
pyautogui.drag(end_x - start_x, end_y - start_y, duration=duration,
               startX=start_x, startY=start_y)
```
**After:**
```python
pyautogui.moveTo(start_x, start_y)
pyautogui.dragTo(end_x, end_y, duration=duration)
```

#### 2. `command_executor.py` — inline `import time` mid-method

`import time; time.sleep(0.4)` was inlined inside the `OPEN` branch of `_dispatch`. Moved `import time` to module-level imports; removed inline statement.

#### 3. `ipad_bridge.py` — repeated `sys.path.insert` inside two methods

`sys.path.insert(0, .../mcp_server)` was duplicated inside `_handle_trackpad` and `_send_status`, running on every call. Moved to module level (runs once on import).

#### 4. `tests/test_bridge_client.py` — `dict.pop()` mutates shared test data

`test.pop("description")` removed the key from `TEST_MESSAGES` entries in place. A second test run would fail with `KeyError`. Changed to `test["description"]` with a separate `payload` dict (excluding `"description"`) sent over the wire.

#### 5. `requirements.txt` — missing `psutil`

`mcp_server/tools/windows.py` imports `psutil` but the package was absent from `requirements.txt`. Added `psutil>=5.9.0`.

---

### Known Issues / Deferred (Not Fixed Today)

| Issue | Location | Notes |
|-------|----------|-------|
| DICTATE uses `typewrite` (ASCII-only) | `command_executor.py:117`, `keyboard.py:8` | Requirement 17.3 says DICTATE should paste via clipboard. `pyautogui.typewrite` drops non-ASCII chars. Fix deferred — needs clipboard paste path added to `keyboard.py`. |
| `keyboard_type` limited to ASCII | `keyboard.py:8` | `pyautogui.typewrite` does not support unicode. For full unicode support, switch to `pyperclip` + `Ctrl+V` for DICTATE; `pyautogui.write` for TYPE (same limitation). |
| Config path in comments | `desktop_mcp_server.py:12`, `tech.md:62` | Comment references `claude_desktop_config.json` — verify this is the current Claude Code MCP config filename for this installation. |
| `find_text_on_screen` single-word only | `screen.py:69-81` | OCR search iterates individual words; multi-word phrases across OCR word boundaries won't match. Tracked as future improvement. |
| Tasks 1.1–1.6 incomplete | `tasks.md` | Phase 1 items remain open: `LocalInference` ABC, VRAM measurement, `FusionEngine`, `HybridCoordinator` update, integration test. |

---

### Current State

The Phase 1 bridge skeleton is functional for the following flows:
- iPad → WebSocket → `ipad_bridge.py` → `CommandExecutor` → MCP tool → pyautogui → Windows
- iPad trackpad events → WebSocket → `ipad_bridge.py` → pyautogui (direct, no executor)
- Claude Code → MCP stdio → `desktop_mcp_server.py` → pyautogui/win32gui → Windows

All 5 bugs above are fixed on the working tree. The bridge is not yet wired to `FusionEngine` or `HybridCoordinator` (those don't exist yet — Phase 1 tasks 1.4/1.5).
