# Sprint Roadmap — Next Phases

_As of 2026-05-20. Sprints 1–4 complete. Sessions 22–23 running in production._

---

## Current System State (baseline for sprint planning)

```mermaid
block-beta
    columns 4

    block:sensors["Sensors (iPad)"]:1
        tilt["Tilt ✅"]
        touch["Touch ✅"]
        voice["Voice ✅"]
        gesture["Gesture ✅\n(HandLandmarker)"]
        lidar["Depth ✅\n(RealSense L515)"]
    end

    block:pipeline["Pipeline (PC)"]:1
        bridge["IPadBridge ✅"]
        fusion["FusionEngine ✅\n60Hz / 6-level"]
        coord["HybridCoordinator ✅\nGate 0 + 1–4"]
        twin["BehavioralTwinState ✅\nChromaDB live"]
        trainer["ContinuousTrainer ✅"]
        whisper["WhisperStream ✅\nlarge-v3 CUDA"]
    end

    block:execution["Execution (PC)"]:1
        executor["CommandExecutor ✅\n16 verbs"]
        mcp["MCP Server ✅\n14 tools"]
        ocr["Tesseract OCR\n(word-match only) ⚠️"]
        grounding["Vision grounding ❌\nGap 1"]
    end

    block:cloud["Cloud (AWS)"]:1
        bedrock["Bedrock Haiku ✅\nfallback active"]
        transcribe["Transcribe ✅\nGate 1 fallback"]
        polly["Polly / Kokoro / SAPI ✅\nTTS live"]
        agentcore["AgentCore ⏸️\ndeploy deferred"]
    end
```

---

## Sprint 5 — Vision Grounding (Gap 1)

**Goal:** Voice commands reliably land on targets instead of CLARIFYing.

**The problem today:** `"click the submit button"` → Whisper → llama3.1:8b emits `CLICK submit button` → CommandExecutor calls `find_text_on_screen("submit")` (Tesseract, word-match only) → often fails → CLARIFY. There is no vision-model-in-the-loop to actually look at the screen and find the element.

**Solution:** After gate evaluation produces an action with a named target, take a screenshot and send it to Claude vision to get pixel coordinates before executing.

```mermaid
sequenceDiagram
    participant U as User
    participant W as WhisperStream
    participant C as HybridCoordinator
    participant V as VisionGrounder (new)
    participant E as CommandExecutor
    participant D as Desktop

    U->>W: "click the submit button"
    W->>C: Command(text="click submit", source="voice")
    C->>C: Gate 0–4 → action="CLICK submit button"
    C->>V: ground(action="CLICK", target="submit button")
    V->>D: screenshot()
    D-->>V: base64 PNG
    V->>V: Claude claude-sonnet-4-6 vision\n"Where is 'submit button'? Return pixel coords."
    V-->>C: coords=(847, 632)
    C->>E: execute(CLICK, coords=(847,632))
    E->>D: pyautogui.click(847, 632)
```

**New component:** `vision_grounder.py`

```mermaid
classDiagram
    class VisionGrounder {
        +ground(action: str, target: str, screenshot_b64: str) tuple[int,int] | None
        -_client: anthropic.Anthropic
        -_cache: dict[str, tuple]
        +_ask_claude(screenshot, target) dict
        +_parse_coords(response) tuple[int,int] | None
    }

    class HybridCoordinator {
        +_grounder: VisionGrounder
        +_execute_action(action_str, cmd) dict
    }

    HybridCoordinator --> VisionGrounder : calls ground() for CLICK/CLOSE targets
```

**Key implementation decisions:**
- Only invoke vision grounding for `CLICK` and `CLOSE` verbs with a named target (not for `SCROLL`, `TYPE`, `HOTKEY` which don't need coordinates)
- Cache screenshot + target → coords for 2 seconds to handle rapid re-clicks
- Fallback chain: vision coords → Tesseract OCR → screen centre + CLARIFY
- Use `claude-sonnet-4-6` vision (fast, cheap); prompt: structured JSON response `{"x": N, "y": N, "confidence": 0.0–1.0}`
- Add `GROUNDING_MIN_CONFIDENCE = 0.7`; below threshold, fall through to Tesseract

**Files touched:** `vision_grounder.py` (new), `hybrid_coordinator.py`, `command_executor.py`, `requirements.txt` (add `anthropic`)

**Success metric:** >80% of voice `CLICK` commands land without CLARIFY after 50 real commands logged.

---

## Sprint 6 — Accessibility Tree Integration (Gap 2)

**Goal:** For the top 3–4 daily apps (browser, IDE, PDF reader), enumerate UI elements structurally instead of relying on OCR or vision.

**The problem:** Win32 UIAutomation gives bounding boxes, roles, and labels for all interactive elements without a screenshot. For apps that implement accessibility (VS Code, Chrome, Acrobat), this is more reliable than OCR and faster than vision grounding.

```mermaid
flowchart TD
    A([CLICK 'submit button']) --> B{App supports\nUIAutomation?}

    B -->|yes — structured| C["UIAutomationProvider.find(target)\nPython comtypes / pywinauto\nreturns BoundingRect + role + name"]
    B -->|no — canvas/Electron| D["Fall through to\nVisionGrounder (Sprint 5)"]

    C --> E{Match found?}
    E -->|yes| F["pyautogui.click(x, y)\nfrom BoundingRect centre"]
    E -->|no| D

    F --> LOG["Log to agent.db\nsource='uiautomation'\nmatched_element=name"]
```

**New component:** `ui_automation.py`

```mermaid
classDiagram
    class UIAutomationProvider {
        +find(target: str, app: str) UIElement | None
        +list_clickable(app: str) list[UIElement]
        -_supported_apps: set[str]
        -_cache: dict
    }

    class UIElement {
        +name: str
        +role: str
        +bounds: tuple[int,int,int,int]
        +center() tuple[int,int]
        +is_enabled: bool
    }

    class CommandExecutor {
        +_ui_provider: UIAutomationProvider
    }

    CommandExecutor --> UIAutomationProvider : calls find() before OCR fallback
    UIAutomationProvider --> UIElement : returns
```

**Target app list (prioritised for grad school workflows):**
1. VS Code — coding, ML experiments
2. Chrome / Edge — research papers, web
3. Zotero / Acrobat — PDF reading
4. Windows Terminal — CLI operations

**Files touched:** `ui_automation.py` (new), `command_executor.py`, `mcp_server/tools/windows.py` (add `enumerate_elements`)

**Success metric:** CLICK lands without vision API call for >70% of VS Code and Chrome interactions.

---

## Sprint 7 — Action Verification Loop (Gap 3)

**Goal:** After every action, take a screenshot, compare with pre-action state, and confirm the command succeeded before moving on. Enable multi-step dev-agent chains without silent failures.

**The problem today:** The pipeline executes and logs, but success/failure is inferred (HTTP 200 from pyautogui) not observed (did the screen actually change?). Multi-step voice commands like "open the terminal then run pytest" can silently fail on step 1 and waste step 2.

```mermaid
sequenceDiagram
    participant E as CommandExecutor
    participant V as ActionVerifier (new)
    participant D as Desktop
    participant DB as AgentDB

    E->>D: pyautogui.click(847, 632)
    E->>V: verify(action="CLICK", pre_screenshot=..., timeout_ms=500)
    V->>D: screenshot() after 500ms
    D-->>V: post_screenshot
    V->>V: compare(pre, post)\nperceptual diff > threshold?
    alt Screen changed as expected
        V-->>E: VerifyResult(success=True, diff_pct=12.3%)
        E->>DB: insert_command(success=True)
    else No visible change
        V-->>E: VerifyResult(success=False, reason="no_change")
        E->>DB: insert_command(success=False, error="no_change")
        E->>E: emit CLARIFY "I clicked but nothing changed"
    end
```

**New component:** `action_verifier.py`

```mermaid
classDiagram
    class ActionVerifier {
        +verify(action: str, pre_b64: str, post_b64: str) VerifyResult
        -_diff_perceptual(a: bytes, b: bytes) float
        -_CHANGE_THRESHOLD: float = 0.02
        -_VERIFIABLE_VERBS: set = CLICK, OPEN, CLOSE, SCROLL
    }

    class VerifyResult {
        +success: bool
        +diff_pct: float
        +reason: str
        +pre_b64: str
        +post_b64: str
    }

    class CommandExecutor {
        +_verifier: ActionVerifier
        +_verify_after_ms: int = 400
    }

    CommandExecutor --> ActionVerifier : wraps execute()
    ActionVerifier --> VerifyResult : returns
```

**Implementation notes:**
- Perceptual diff using `ImageChops.difference` from Pillow — no ML model needed
- `CHANGE_THRESHOLD = 2%` pixel change → success (scrolling, window changes, button state changes all exceed this)
- Only verify `CLICK`, `OPEN`, `CLOSE`, `SCROLL` — `TYPE` and `HOTKEY` are inherently unverifiable by visual diff
- Verification timeout: 400 ms (fast enough to not feel sluggish, long enough for animations)
- Store `pre_b64` + `post_b64` in `agent.db` for the first 30 days (truncate after → keep only the outcome flag)
- Failed verifications feed into `BehavioralTwinState` as failure signal → pain day score

**Files touched:** `action_verifier.py` (new), `command_executor.py`, `db.py` (add `verification_result` column to commands), `behavioral_twin_state.py` (wire failure signal)

**Success metric:** ContinuousTrainer receives real success/failure signal for >90% of CLICK commands; CLARIFY rate on failed actions drops as grounding (Sprint 5) + verification (Sprint 7) close the loop.

---

## Sprint Dependency Graph

```mermaid
gantt
    title Sprint Roadmap 2026
    dateFormat YYYY-MM-DD
    axisFormat %b %d

    section Complete
    Sprints 1-4 (all specs)     :done, s1_4, 2026-05-01, 2026-05-20

    section Production use
    Real data collection        :active, collect, 2026-05-20, 2026-06-03

    section Sprint 5
    vision_grounder.py          :s5a, 2026-06-03, 3d
    Coordinator grounding hook  :s5b, after s5a, 2d
    Grounding tests             :s5c, after s5b, 1d

    section Sprint 6
    ui_automation.py            :s6a, after s5c, 4d
    MCP enumerate_elements      :s6b, after s6a, 1d
    App-specific integration    :s6c, after s6b, 3d

    section Sprint 7
    action_verifier.py          :s7a, after s6c, 2d
    DB verification column      :s7b, after s7a, 1d
    Failure → twin state wire   :s7c, after s7b, 1d

    section Grad school prep
    Study mode profile          :gs, after s7c, 2026-08-01
```

---

## Gap Closure Trajectory

```mermaid
xychart-beta
    title "Expected CLICK success rate by sprint"
    x-axis ["Now\n(Session 23)", "Post Sprint 5\n(Vision grounding)", "Post Sprint 6\n(UIAutomation)", "Post Sprint 7\n(Verification loop)"]
    y-axis "CLICK success rate (%)" 0 --> 100
    bar [42, 78, 88, 92]
    line [42, 78, 88, 92]
```

_Baseline 42% estimated from research: 56.7% of UI interactions miss their target in SOTA systems; this system is lower due to Tesseract word-match-only grounding. Post-Sprint 5 estimate based on Claude vision grounding benchmark (~78%). Post-Sprint 6 adds UIAutomation structured access for supported apps. Post-Sprint 7 verification closes the feedback loop._
