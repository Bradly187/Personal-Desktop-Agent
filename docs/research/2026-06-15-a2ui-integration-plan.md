# A2UI Integration Plan — Personal Desktop Agent

*Agent-generated, declaratively-described touch UI on the iPad, rendered natively over the existing SwiftUI design system*

**Date:** 2026-06-15 · **Companion to:** [`2026-06-15-agent-interop-gap-analysis.md`](./2026-06-15-agent-interop-gap-analysis.md) · **Status:** Phase 1 (approval) + Phase 2-enumerable (CLARIFY templates) landed on PC (tested); iPad render + end-to-end pending a device build

---

## 0. Implementation status (2026-06-15)

| Piece | File | State |
|---|---|---|
| Surface builder + validation | `core/a2ui.py` | ✅ done |
| **CLARIFY template library (token-free)** | `core/a2ui.py` (`template_for_clarify`, `direction_surface`, `open_type_surface`, `post_open_action_surface`, `TEMPLATES`) | ✅ done |
| Bridge send / clear / event handler | `core/ipad_bridge.py` (`send_a2ui_surface`, `clear_a2ui_surface`, `register_a2ui_surface`, `_handle_a2ui_event`) | ✅ done |
| Approval gate-open trigger | `approval_hook.py` (persist prompt) + `sensors/whisper_stream.py` (`on_approval_gate_open`) + `main.py` | ✅ done |
| **CLARIFY emit/clear + tap→voice routing** | `core/hybrid_coordinator.py` (`_maybe_emit_clarify_surface`/`_clear_clarify_surface`) + bridge tap-routing | ✅ done |
| **Click-target palette (Phase 3, prototype)** | `core/a2ui.py` (`is_click_target_clarify`, `click_target_surface`) + coordinator (`_rank_click_targets`, `_build_click_target_surface`, `set_target_cache`) + bridge `click_target` dispatch | 🧪 flag-gated `DA_A2UI_CLICK_TARGETS=1` |
| Swift models / renderer / overlay | `iPadApp/.../Network/A2UIModels.swift`, `UI/A2UIRenderer.swift`, `UI/A2UIOverlay.swift`, `WebSocketManager.swift`, `ContentView.swift` | ✅ scaffolded (generic — handles approval AND clarify with no per-type code) |
| **Persistent dashboard canvas (Agent tab)** — see §7b | PC: `core/a2ui.py` (`canvas`, `status_dashboard`, `canvas_update_message`, `canvas_clear_message`) + bridge (`send/update/clear_a2ui_canvas`). iPad: `UI/A2UICanvasStore.swift` (app-scoped, cached to UserDefaults), `UI/AgentDashboardView.swift` (tab 3), canvas decode + feeds in `WebSocketManager.swift`, `Codable`+`merging` in `A2UIModels.swift`, tab wiring in `ContentView.swift`, store injection in `DesktopAgentApp.swift` | ✅ built (PC tested); ⏳ iPad build |
| End-to-end on device | — | ⏳ needs one iPad build |

PC subset green: **239 passed** (`-k "coordinator or clarif or bridge or whisper or approval or a2ui or gate"`). The Swift side compiles against the existing design system but is unverified until an Xcode build.

**Token economics.** Rendering is on-device (free). Generation is token-free for both shipped paths: approval is a fixed template; enumerable CLARIFY reuses options the model *already* emitted as text, mapped to a template by string match — the LLM is never asked to author UI. Only the (unimplemented) free-form "LLM-generates-UI" pattern would spend tokens, and the 31% free-form CLARIFYs deliberately stay voice-only.

**CLARIFY audit result** (sized Phase 2): of 65 real CLARIFY events in `agent.db`, **42% enumerable / 28% semi / 31% free-form**. Shipped: the 42% enumerable (direction, type, post-open action) **and** the 28% semi — "what would you like to open?" now renders the user's recent apps as buttons, fed by a rolling `_recent_open_targets` buffer in the coordinator (updated on each successful OPEN; falls back to voice until the user has opened something). Net: **~70% of clarifications are now tappable**.

**The 31% free-form, dissected** (Phase 3): ~58% of it is **click-target** ("what would you like me to click on?") — *not* truly free-form, because the live UI tree enumerates every clickable element. The prototype ranks `target_cache.snapshot()` by cursor proximity (dedup, filter unnamed/tiny/oversized, cap 8) and renders them as a tappable palette; a tap fires a coordinate-precise CLICK via the touch-bypass path (no LLM, no re-grounding). The remaining ~42% (genuinely conversational) stays voice-only by design.

**Evaluating the click-target prototype.** Gated `DA_A2UI_CLICK_TARGETS=1` (off by default). When a click-target CLARIFY fires, the ranked element names are logged at INFO (`a2ui: click-target palette (N): [...]`) — eyeball those lists to judge whether the names are legible enough to beat voice before promoting it. If names prove cryptic, the fallback is a numbered screenshot-overlay render mode (more iPad work). Potential reach if promoted: ~70% → ~88% tappable.

---

## 1. Goal & non-goals

**Goal.** Let the PC agent describe an interactive surface (buttons, choices, a small form) as a **declarative JSON message**, and have the iPad render it as native, large-touch-target SwiftUI from a **fixed trusted catalog** — then send the user's interaction back over the existing WebSocket. First payoff: turn **CLARIFY** and the **approval gate** from voice-only into tap-or-voice.

**Non-goals (explicit, to keep scope honest):**
- **No Google `a2ui-agent-sdk` / ADK / Gemini dependency.** We adopt the *message shape and catalog discipline* of A2UI, not the egress-coupled Python SDK. Rendering is a small native Swift component.
- **No arbitrary code or HTML to the iPad.** Renderer instantiates only known catalog component types. Unknown type → ignored/fallback. This is the security guarantee.
- **No new network surface.** Reuses the authenticated `:8765` WebSocket and the existing `broadcast_json` path. Zero egress.
- **No Canvas / persistent-document mode** in v1 (paper §Canvas). Transient surfaces only.
- **No replacement of existing hand-coded screens.** A2UI is additive — for *agent-initiated, dynamic* prompts, not the static app shell.

---

## 2. Where it plugs into the current code

| Concern | Existing seam | Change |
|---|---|---|
| PC → iPad send | `core/ipad_bridge.py:977 broadcast_json()` | + `send_a2ui_surface()` / `clear_a2ui_surface()` |
| iPad inbound decode | `WebSocketManager.swift:394` `switch type` | + `case "a2ui_surface"` / `"a2ui_clear"` → `a2uiFeed` |
| iPad render | `DesignSystem/` (`DAButton`, `DACard`, …) + `UI/CommandToast.swift` overlay pattern | + `A2UIRenderer.swift`, `A2UIOverlay.swift` |
| iPad → PC event | `WebSocketManager.swift:243 sendCommand()` / `:162 send()` | + `sendA2UIEvent(surfaceId:event:values:)` |
| PC inbound decode | `core/ipad_bridge.py:339 _handle_message` (chain of `if msg_type == …`) | + `if msg_type == "a2ui_event"` |
| CLARIFY emit | `command_executor.py:664` (currently speaks + returns) | optionally also emit a surface |
| Approval gate | `approval_hook.py` + `core/approval_keywords.py` + `~/.claude/approval/response` | A2UI event writes the same response signal file |

The proactive-notification feature (added recently: `proactive_notification` → `proactiveFeed` → overlay in `CommandToast.swift`) is the **proven precedent** — A2UI is the same pattern generalized from one fixed layout to a small declarative catalog.

---

## 3. Message schema (A2UI-aligned, trimmed)

Keep the paper's flat, id-referenced adjacency list (easy for an LLM to emit incrementally; easy for the client to diff). Trim to what a single-user accessibility surface needs.

### 3.1 PC → iPad: render a surface

```json
{
  "type": "a2ui_surface",
  "surface_id": "clarify-7f3a",
  "version": "v0.9",
  "dismissible": true,
  "timeout_s": 30,
  "root": "root",
  "components": [
    {"id": "root", "component": "Card", "children": ["q", "choices"]},
    {"id": "q", "component": "Text", "text": "Did you mean Kiro or Cairo?", "variant": "headline"},
    {"id": "choices", "component": "Row", "children": ["c1", "c2"]},
    {"id": "c1", "component": "Button", "label": "Kiro", "icon": "k.circle",
     "action": {"event": "choice", "value": "kiro"}},
    {"id": "c2", "component": "Button", "label": "Cairo", "icon": "c.circle",
     "action": {"event": "choice", "value": "cairo"}}
  ]
}
```

### 3.2 PC → iPad: clear a surface (agent resolved it another way, or timed out server-side)

```json
{"type": "a2ui_clear", "surface_id": "clarify-7f3a"}
```

### 3.3 iPad → PC: user interacted

```json
{
  "type": "a2ui_event",
  "surface_id": "clarify-7f3a",
  "event": "choice",
  "value": "kiro",
  "values": {},                // for forms: {field_id: value}
  "ts": 1718...
}
```

`values` carries form field state (TextField/Slider/Checkbox/ChoicePicker) keyed by component id; `value` carries the firing control's action value. Both optional per event.

---

## 4. The catalog (v1 — map to existing components)

Bring-your-own catalog, per paper p.34. **v1 = the minimum that covers approval + CLARIFY + simple confirm/form**, every entry backed by a component that already exists or is a thin wrapper:

| A2UI type | Renders as | Touch target |
|---|---|---|
| `Column` / `Row` | `VStack` / `HStack` (DesignTokens spacing) | — |
| `Card` | `DACard` | — |
| `Text` (`variant`: headline/body/caption) | `Text` + `DesignTokens.Typography` | — |
| `Button` (`label`, `icon`, `action`) | `DAButton` (already 80pt min) | ✅ 80pt |
| `ChoicePicker` (`options[]`, single/multi) | segmented list of `DAButton`s | ✅ |
| `TextField` (`field_id`, `placeholder`) | `TextField` (large, rounded) | ✅ |
| `Checkbox` / `Slider` | native, sized to `touchTargetCompact` (64pt) | ✅ 64pt |
| `Divider` / `Image` | `Divider` / `AsyncImage`/base64 | — |

Unknown `component` → renderer skips it (and logs via `AppLogger`), never crashes — the graceful-degradation rule from CLAUDE.md conventions.

**Why this is safe:** the renderer is a `switch` over a closed enum of component types instantiating *your* views. There is no code path from message → execution. This is exactly the A2UI security model (p.33), enforced in Swift by exhaustiveness.

---

## 5. iPad rendering design

`A2UIRenderer.swift` (pure function, testable):

```
struct A2UISurface: Decodable { id, version, root, components: [A2UINode], dismissible, timeout_s }
struct A2UINode: Decodable { id, component, children?, text?, variant?, label?, icon?, action?, options?, field_id?, placeholder? }

final class A2UIState: ObservableObject {     // one per live surface
    @Published var fieldValues: [String: AnyCodable]
    let onEvent: (_ event: String, _ value: String?, _ values: [String:Any]) -> Void
}

@ViewBuilder func render(_ node: A2UINode, _ nodes: [String:A2UINode], _ state: A2UIState) -> some View
// recursive over children; leaf controls call state.onEvent(...) which → WebSocketManager.sendA2UIEvent
```

- `A2UIOverlay.swift` mirrors the `CommandToast`/`ProactiveNotificationOverlay` overlay pattern (top of `ContentView`, `allowsHitTesting` only on the card — reuse the hard-won touch-interception discipline from `DwellToolbarContainer`).
- One surface at a time in v1 (a new `a2ui_surface` replaces the live one; `a2ui_clear` dismisses). Keep a `surface_id` guard so a stale clear can't dismiss a newer surface.
- Client-side `timeout_s` auto-dismiss **and** sends `{event: "timeout"}` so the PC side resolves deterministically (fail-safe to the same behavior as today's voice-CLARIFY timeout → DENY for approvals).

---

## 6. PC side

`core/a2ui.py` (new, small):

```
def card(title, *, surface_id=None) -> "A2UIBuilder"     # fluent builder, deterministic ids
class A2UIBuilder:  .text(...) .button(label, value, icon=None) .choices(...) .textfield(...) .build() -> dict
def approval_surface(action_desc) -> dict                # tool-as-template: fixed Approve/Deny
def choice_surface(question, options) -> dict            # CLARIFY disambiguation
```

`ipad_bridge.send_a2ui_surface(payload)` → `broadcast_json`. Inbound `a2ui_event` handler resolves the pending future / writes the approval response file, keyed by `surface_id`.

**Pending-surface registry:** a dict `surface_id → asyncio.Future` (or the existing approval signal-file convention). The event handler resolves it; coordinator/approval awaits it with the same timeout/fail-safe semantics already in place. This means **A2UI reuses the existing decision plumbing — it is a new *input channel*, not a new *decision path***.

---

## 7. Two integration targets (phase order)

### Phase 1 — Approval gate (tool-as-template, deterministic, highest safety value)
The approval flow already has a strict state machine (`approval_hook.py`, `core/approval_keywords.py`, `~/.claude/approval/{pending,response}`, fail-safe-DENY). Add an A2UI surface as a **parallel input**: when `pending` appears, also push `approval_surface(action_desc)` (Approve / Deny buttons). A tap writes the same `response` file that a voice confirmation would; voice still works unchanged. Timeout/ambiguity still DENY. **No change to the security invariants** — purely an added, easier input modality. Best first target because the layout is fixed (no LLM) and the payoff (tap-to-approve on a flare day) is immediate.

### Phase 2 — CLARIFY (LLM-generated, intent-driven)
At `command_executor.py:664`, when CLARIFY carries discrete candidates (e.g., app-name disambiguation, which the coordinator often knows), also emit `choice_surface(question, options)`. Voice answer and tap both resolve `_pending_clarification`. For free-form clarifications with no enumerable options, keep voice-only (don't force a UI where there isn't a discrete choice). Let the **coordinator** decide options where it can; only invoke an LLM to *compose* a surface for genuinely novel cases (paper's default pattern) — and wrap that in try/except → fall back to spoken CLARIFY on any schema-validation failure (paper §Best Practices: "renderer should never see a malformed payload").

### Phase 3 (optional, later) — schedule/goal confirmations, pain-journal quick-entry
Once the renderer is proven, route `schedule_parser` confirmations and a pain-journal quick-entry form through the same path. These are deterministic templates.

---

## 7b. Persistent dashboard canvas (Agent tab) — built 2026-06-15

Distinct from the transient overlay (§5): a **persistent** agent-authored surface that lives on its own tab and survives tab switches + app restart. This is the whitepaper's *Canvas* concept. Chosen shape: **agent-authored canvas, hybrid (native status + A2UI region), cached on-device.**

**Lifecycle is separate from the overlay** — three new PC→iPad message types so the two never collide:

| Message | Effect |
|---|---|
| `a2ui_canvas` | set/replace the whole dashboard (no timeout, never auto-dismissed) |
| `a2ui_canvas_update` | merge components by id (partial refresh, no full re-render) |
| `a2ui_canvas_clear` | reset to the empty state |

**PC side** (`core/a2ui.py`, `core/ipad_bridge.py`):
- `A2UIBuilder.canvas(root, surface_id="dashboard")` — builds a canvas-typed surface (no `dismissible`/`timeout_s`).
- `status_dashboard(title, rows, actions=…)` — example agent-status canvas; `canvas_update_message()` / `canvas_clear_message()` patch/clear helpers.
- Bridge `send_a2ui_canvas()` (validates first), `update_a2ui_canvas()`, `clear_a2ui_canvas()`.
- `validate_surface()` now accepts both `a2ui_surface` and `a2ui_canvas`.

**iPad side**:
- `UI/A2UICanvasStore.swift` — app-scoped `ObservableObject` (injected in `DesktopAgentApp`), bound to the WS canvas feeds. Holds the current canvas, applies `merging(_:)` patches, and **caches the last canvas to `UserDefaults`** so it renders instantly on cold start. Lives at app scope because the tab view is created/destroyed on switch — the state must not.
- `UI/AgentDashboardView.swift` — the **Agent tab** (`ContentView` tab index 3; Settings→4, Sensors→5). Hybrid: a native status `DACard` (connection, mic, last command, last alert from local state) above the agent-authored A2UI region rendered by the existing `A2UIRenderer`. Empty-state card when no canvas pushed.
- `Network/WebSocketManager.swift` — decodes the three canvas messages → `a2uiCanvasFeed` / `a2uiCanvasUpdateFeed` / `a2uiCanvasClearFeed`. Canvas control taps reply via the existing `sendA2UIEvent` tagged `event="canvas"`.
- `Network/A2UIModels.swift` — `A2UISurface` made `Codable` with defaulted `dismissible`/`timeoutS` (canvas omits them) + a memberwise init + `merging(_:)`.

**Why hybrid + cache**: the native section guarantees the tab is never blank (glanceable on a flare day even before the PC connects); the cached canvas means a cold start shows the last dashboard immediately, then refreshes on reconnect.

**Note**: unlike the transient overlay, a canvas is **not** dismissible/timed — it persists until the agent replaces or clears it. That's the whole point of a dashboard, and it's why it gets a tab rather than the centered overlay.

**Canvas tap handling** (`_handle_a2ui_event`, `event=="canvas"`): two paths — if the agent registered a Future for the canvas it's resolved (agent awaiting an interaction); otherwise the button's `value` is routed as a voice-equivalent command through the coordinator (so a button valued `"open kiro"` runs that command, gated like any other). The canvas persists through the tap.

**Seed-on-connect**: `IPadBridge._build_status_dashboard()` composes an agent-status canvas (Status / Pain day / Voice + a "What can you do?" action button) from cheaply-available state, defensively (missing subsystems degrade, never block). It's pushed to each client on connect (`ws_handler`) so the Agent tab is populated immediately.

**Live refresh (wired)**: `ResourceGovernor.set_flare_change_callback()` fires on each pain-day (flare) on/off transition; `main.py` wires it to `bridge.push_status_dashboard()`, so the dashboard's "Pain day" row updates in real time without waiting for a reconnect. Callback is sync-or-async tolerant and fire-and-forget (a slow push never delays flare handling). Mic state needs no canvas refresh — the native dashboard section reads it live on the iPad.

---

## 8. Validation & tests

- **PC:** `tests/test_a2ui.py` — builder produces schema-valid payloads; `approval_surface`/`choice_surface` round-trip; pending-surface future resolves on `a2ui_event`; timeout fail-safe (approval → DENY).
- **iPad:** `A2UIRendererTests.swift` — decode → node tree; unknown component skipped not crashed; button action fires `onEvent` with correct value; form `values` aggregation; stale `surface_id` clear is ignored. Mirror the existing `OverlayTouchInterceptionTests` discipline for hit-testing.
- **End-to-end:** extend `tests/test_bridge_client.py` simulated iPad to send an `a2ui_event` and assert the coordinator/approval resolves.
- **Regression guard:** the approval-gate behavior tests (`tests/test_approval_gate.py`, 44) must still pass unchanged — A2UI must not alter any deny-wins/timeout/echo-guard invariant.

---

## 9. Effort & risk

- **Surface area:** ~1 new PC module (`core/a2ui.py`) + 2 bridge methods + 1 inbound handler; ~2 Swift files (`A2UIRenderer`, `A2UIOverlay`) + 1 `WebSocketManager` case + 1 sender. Small, isolated, additive.
- **Risk:** low. No egress, closed catalog, reuses authenticated transport and existing decision plumbing. The only real care item is **touch hit-testing** in the overlay — but that ground is already mapped (`DwellToolbarContainer` fixes, `OverlayPreservationTests`).
- **iPad rebuild:** Phases need one iPad build to ship the renderer; thereafter **new agent surfaces require no rebuild** — which is the whole point.
- **Biggest open question:** how often does CLARIFY actually carry *enumerable* choices vs. free-form? Phase 1 (approval) is unconditionally valuable; Phase 2's value scales with that ratio — worth a quick audit of recent CLARIFY outcomes in `agent.db` before committing to Phase 2 scope.

---

## 10. Decision checklist before starting

1. Confirm Phase 1 (approval-gate A2UI) as the first slice — deterministic, safest, immediate flare-day payoff.
2. Audit recent CLARIFY rows in `agent.db` for how many are enumerable (sizes Phase 2).
3. Lock the v1 catalog (§4) — resist adding components until a surface needs them (CLAUDE.md: minimum code, no speculative features).
4. Keep voice as a co-equal input throughout — A2UI augments, never replaces, the existing modalities.
