# Scientific Keypad — Design Spec

## Purpose

A SwiftUI view on the iPad that gives the user a hardware-keyboard-style entry surface for numbers and mathematical symbols. The user builds an expression on the iPad display, then sends it to the PC in one tap. Because the PC now uses clipboard-paste (`keyboard_paste`), any unicode character — `π`, `√`, `∑`, Greek letters, superscripts — arrives correctly in whatever PC application has focus.

---

## User Flow

1. User navigates to the **Keypad** tab in the iPad app
2. Taps buttons to build an expression (e.g. `sin(π/4) + √2`)
3. Optionally evaluates on-device to preview the result
4. Taps **Send** → app sends `{"type":"touch_command","action":"DICTATE","text":"sin(π/4) + √2"}` to the bridge
5. PC pastes the expression into whatever has keyboard focus

---

## Layout

```
┌─────────────────────────────────────────────────────┐
│  sin(π/4) + √2                             [CLR][⌫] │  ← expression display (scrollable, monospace)
│  ≈ 1.4142...                                        │  ← live evaluation (optional, greyed)
└─────────────────────────────────────────────────────┘
                                          [Basic / Sci] ← mode toggle

── Scientific row (Sci mode only) ──────────────────────
│  sin  │  cos  │  tan  │  log  │  ln   │  10ˣ  │  eˣ  │
│ sin⁻¹ │ cos⁻¹ │ tan⁻¹ │  log₂ │  abs  │   !   │  mod │

── Common ───────────────────────────────────────────────
│   π   │   e   │   (   │   )   │   ^   │   √   │   ±  │
│   7   │   8   │   9   │   ÷   │   EE  │  ...  │      │
│   4   │   5   │   6   │   ×   │       │       │      │
│   1   │   2   │   3   │   −   │       │       │      │
│   0   │   .   │  ANS  │   +   │            [SEND]    │
```

All buttons: minimum 64×64 pt, `.contentShape(Rectangle())`, generous `.padding()`.

---

## WebSocket Message

```json
{
  "type": "touch_command",
  "id": "<uuid>",
  "action": "DICTATE",
  "text": "sin(π/4) + √2",
  "params": {}
}
```

`DICTATE` routes through `CommandExecutor.keyboard_paste()` on the PC — full unicode arrives via clipboard paste.

---

## SwiftUI Implementation Skeleton

```swift
// ScientificKeypadView.swift
import SwiftUI

struct ScientificKeypadView: View {
    @State private var expression: String = ""
    @State private var evaluation: String = ""
    @State private var isScientificMode: Bool = true
    var onSend: (String) -> Void

    var body: some View {
        VStack(spacing: 8) {
            displayArea
            modePicker
            if isScientificMode { scientificRows }
            commonRows
        }
        .padding()
        .background(Color(uiColor: .systemBackground))
    }

    // MARK: - Display

    private var displayArea: some View {
        VStack(alignment: .trailing, spacing: 4) {
            ScrollView(.horizontal, showsIndicators: false) {
                Text(expression.isEmpty ? "0" : expression)
                    .font(.system(size: 32, design: .monospaced))
                    .foregroundColor(.primary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            if !evaluation.isEmpty {
                Text("≈ \(evaluation)")
                    .font(.system(size: 18, design: .monospaced))
                    .foregroundColor(.secondary)
            }
        }
        .padding()
        .background(Color(uiColor: .secondarySystemBackground))
        .cornerRadius(12)
    }

    // MARK: - Mode Toggle

    private var modePicker: some View {
        Picker("Mode", selection: $isScientificMode) {
            Text("Basic").tag(false)
            Text("Scientific").tag(true)
        }
        .pickerStyle(.segmented)
    }

    // MARK: - Button Grids

    private var scientificRows: some View {
        VStack(spacing: 4) {
            buttonRow(["sin", "cos", "tan", "log", "ln", "10ˣ", "eˣ"])
            buttonRow(["sin⁻¹", "cos⁻¹", "tan⁻¹", "log₂", "abs(", "!", "mod"])
        }
    }

    private var commonRows: some View {
        VStack(spacing: 4) {
            buttonRow(["π", "e", "(", ")", "^", "√(", "±"])
            buttonRow(["7", "8", "9", "÷", "EE", "CLR", "⌫"])
            buttonRow(["4", "5", "6", "×", "", "", ""])
            buttonRow(["1", "2", "3", "−", "", "", ""])
            HStack(spacing: 4) {
                KeypadButton("0",   width: 2) { append("0") }
                KeypadButton(".",   width: 1) { append(".") }
                KeypadButton("ANS", width: 1) { appendAns() }
                KeypadButton("+",   width: 1) { append("+") }
                KeypadButton("SEND", width: 2, role: .send) { send() }
            }
        }
    }

    private func buttonRow(_ labels: [String]) -> some View {
        HStack(spacing: 4) {
            ForEach(labels, id: \.self) { label in
                if label.isEmpty {
                    Spacer()
                } else {
                    KeypadButton(label) { handle(label) }
                }
            }
        }
    }

    // MARK: - Actions

    private func handle(_ label: String) {
        switch label {
        case "CLR":  expression = ""
        case "⌫":   if !expression.isEmpty { expression.removeLast() }
        case "sin":  append("sin(")
        case "cos":  append("cos(")
        case "tan":  append("tan(")
        case "sin⁻¹": append("asin(")
        case "cos⁻¹": append("acos(")
        case "tan⁻¹": append("atan(")
        case "log":  append("log(")
        case "log₂": append("log2(")
        case "ln":   append("ln(")
        case "10ˣ":  append("10^")
        case "eˣ":   append("e^")
        case "abs(": append("abs(")
        case "!":    append("!")
        case "mod":  append(" mod ")
        case "√(":   append("√(")
        case "π":    append("π")
        case "e":    append("e")
        case "EE":   append("×10^")
        case "±":    toggleSign()
        default:     append(label)
        }
        evaluate()
    }

    private func append(_ s: String) { expression += s }

    private func appendAns() {
        expression += evaluation.isEmpty ? "0" : evaluation
    }

    private func toggleSign() {
        if expression.hasPrefix("-") {
            expression.removeFirst()
        } else {
            expression = "-" + expression
        }
    }

    private func evaluate() {
        // On-device evaluation via NSExpression (limited) or a bundled math parser.
        // Replace π → 3.14159..., e → 2.71828..., then evaluate.
        // Set evaluation = "" on parse error (don't show anything invalid).
        let safe = expression
            .replacingOccurrences(of: "π", with: "3.14159265358979")
            .replacingOccurrences(of: "÷", with: "/")
            .replacingOccurrences(of: "×", with: "*")
            .replacingOccurrences(of: "−", with: "-")
        let expr = NSExpression(format: safe)
        if let result = expr.expressionValue(with: nil, context: nil) as? NSNumber {
            evaluation = "\(result.doubleValue)"
        } else {
            evaluation = ""
        }
    }

    private func send() {
        guard !expression.isEmpty else { return }
        onSend(expression)
    }
}


// MARK: - Reusable Button

enum KeypadButtonRole { case normal, send }

struct KeypadButton: View {
    let label: String
    var width: Int = 1
    var role: KeypadButtonRole = .normal
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 20, weight: .medium))
                .frame(maxWidth: .infinity, minHeight: 64)
                .foregroundColor(foregroundColor)
                .background(backgroundColor)
                .cornerRadius(10)
                .contentShape(Rectangle())       // full hit-box for RA
        }
        .buttonStyle(.plain)
        .frame(maxWidth: width == 2 ? .infinity : nil)
    }

    private var backgroundColor: Color {
        switch role {
        case .send:   return .blue
        case .normal: return Color(uiColor: .tertiarySystemBackground)
        }
    }

    private var foregroundColor: Color {
        role == .send ? .white : .primary
    }
}
```

---

## Integration in ContentView / Tab Navigation

```swift
// Add to the main tab view alongside CommandPadView and TrackpadView
TabView {
    CommandPadView(ws: wsManager)
        .tabItem { Label("Commands", systemImage: "hand.tap") }
    TrackpadView(ws: wsManager)
        .tabItem { Label("Trackpad", systemImage: "cursorarrow.motionlines") }
    ScientificKeypadView { expression in
        wsManager.send([
            "type": "touch_command",
            "id": UUID().uuidString,
            "action": "DICTATE",
            "text": expression,
            "params": [:]
        ])
    }
    .tabItem { Label("Keypad", systemImage: "function") }
    SettingsView()
        .tabItem { Label("Settings", systemImage: "gear") }
}
```

---

## On-Device Evaluation Notes

`NSExpression` handles basic arithmetic (`+`, `-`, `*`, `/`, `**`) after substituting unicode operators. For full scientific evaluation (trig, log, sqrt) the recommended approach is to bundle a small Swift math-expression parser such as **Expression** (by Nick Lockwood, MIT licence, no dependencies) or **MathParser** rather than trying to route evaluation through NSExpression.

Evaluation is **display-only** — the user always sends the raw expression string, not the computed result, unless they explicitly want to send the answer (ANS button sends `evaluation`).

---

## Tasks Added

See `tasks.md` — task 2.14 `ScientificKeypadView`.
