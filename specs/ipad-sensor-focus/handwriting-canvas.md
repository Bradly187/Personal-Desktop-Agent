# Apple Pencil Handwriting Canvas — Design Spec

## Purpose

A SwiftUI view that wraps a PencilKit `PKCanvasView`, letting the user write mathematical expressions freehand with their Apple Pencil. The ink is rendered to a PNG and sent to the PC, where `pix2tex` (running on the RTX 5090) recognises the expression as LaTeX. The result is returned to the iPad for confirmation before being pasted into the focused PC application.

---

## Full Interaction Flow

```
User writes with Apple Pencil
         ↓
 [PKCanvasView captures ink]
         ↓
 User taps [Recognise]
         ↓
 iPad renders canvas → PNG bytes → base64
         ↓
 WebSocket: handwriting_image → PC bridge
         ↓
 PC: pix2tex (GPU) → LaTeX string
         ↓
 WebSocket: handwriting_result ← PC bridge
         ↓
 iPad displays LaTeX + unicode preview
         ↓
 User taps [Send to PC]  or  [Try Again]
         ↓
 touch_command / DICTATE → keyboard_paste → Ctrl+V in PC app
```

---

## WebSocket Protocol

### iPad → PC  (`handwriting_image`)

```json
{
  "type": "handwriting_image",
  "id": "<uuid>",
  "image": "<base64-encoded PNG, white background, black ink>",
  "width": 1024,
  "height": 400
}
```

### PC → iPad  (`handwriting_result`)

```json
{
  "type": "handwriting_result",
  "id": "<same uuid>",
  "latex":   "\\sin\\left(\\frac{\\pi}{4}\\right) + \\sqrt{2}",
  "unicode": "sin((π)/(4)) + √2",
  "confidence": 1.0
}
```

On error:
```json
{
  "type": "handwriting_result",
  "id": "<same uuid>",
  "error": "pix2tex not installed — run: pip install pix2tex"
}
```

---

## Layout

```
┌────────────────────────────────────────────────────────────┐
│                                                            │
│   [blank canvas — write here with Apple Pencil]           │
│                                                            │
│   ~ 320 pt tall, full width, white bg, black strokes      │
│                                                            │
└────────────────────────────────────────────────────────────┘
  [Clear]   [Undo]                            [Recognise →]

── Result (shown after recognition) ────────────────────────
  LaTeX:    \sin\left(\frac{\pi}{4}\right) + \sqrt{2}
  Unicode:  sin((π)/(4)) + √2
────────────────────────────────────────────────────────────
            [Edit unicode]        [Send to PC ✓]
```

- Canvas is full-width with a light grey border
- Minimum stylus input — finger touches are rejected (`.allowsFingerDrawing = false`)
- Undo restores the last stroke (standard `PKCanvasView` undo)
- Recognition state: idle / waiting / result / error
- Edit unicode: opens a text field pre-filled with the unicode string so the user can fix misrecognitions before sending

---

## SwiftUI Implementation Skeleton

```swift
// HandwritingCanvasView.swift
import SwiftUI
import PencilKit

struct HandwritingCanvasView: View {
    @StateObject private var vm = HandwritingViewModel()
    var onSend: (String) -> Void

    var body: some View {
        VStack(spacing: 12) {
            canvas
            toolbar
            if let result = vm.result { resultPanel(result) }
            if let error  = vm.errorMessage { errorPanel(error) }
        }
        .padding()
    }

    // MARK: - Canvas

    private var canvas: some View {
        PKCanvasRepresentable(canvasView: vm.canvasView)
            .frame(height: 320)
            .background(Color.white)
            .cornerRadius(12)
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(Color(uiColor: .separator), lineWidth: 1)
            )
            .overlay(
                Group {
                    if vm.canvasView.drawing.strokes.isEmpty {
                        Text("Write expression with Apple Pencil")
                            .foregroundColor(.secondary)
                            .font(.body)
                    }
                }
            )
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        HStack {
            Button("Clear") { vm.clear() }
                .buttonStyle(.bordered)
                .frame(minWidth: 80, minHeight: 44)
                .contentShape(Rectangle())

            Button("Undo") { vm.undo() }
                .buttonStyle(.bordered)
                .frame(minWidth: 80, minHeight: 44)
                .contentShape(Rectangle())

            Spacer()

            Button {
                vm.recognise()
            } label: {
                HStack {
                    if vm.isRecognising {
                        ProgressView().scaleEffect(0.8)
                    }
                    Text(vm.isRecognising ? "Recognising…" : "Recognise →")
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(vm.canvasView.drawing.strokes.isEmpty || vm.isRecognising)
            .frame(minWidth: 140, minHeight: 44)
            .contentShape(Rectangle())
        }
    }

    // MARK: - Result

    private func resultPanel(_ result: HandwritingResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("LaTeX", systemImage: "function")
                .font(.caption).foregroundColor(.secondary)
            Text(result.latex)
                .font(.system(size: 14, design: .monospaced))
                .textSelection(.enabled)

            Divider()

            Label("Unicode", systemImage: "character")
                .font(.caption).foregroundColor(.secondary)

            if vm.isEditing {
                TextField("Edit expression", text: $vm.editedUnicode)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 18, design: .monospaced))
            } else {
                Text(result.unicode)
                    .font(.system(size: 18, design: .monospaced))
                    .textSelection(.enabled)
            }

            HStack {
                Button(vm.isEditing ? "Done" : "Edit") {
                    if vm.isEditing { vm.isEditing = false }
                    else { vm.editedUnicode = result.unicode; vm.isEditing = true }
                }
                .buttonStyle(.bordered)
                .frame(minWidth: 80, minHeight: 44)
                .contentShape(Rectangle())

                Spacer()

                Button("Send to PC") {
                    let text = vm.isEditing ? vm.editedUnicode : result.unicode
                    onSend(text)
                }
                .buttonStyle(.borderedProminent)
                .frame(minWidth: 120, minHeight: 44)
                .contentShape(Rectangle())
            }
        }
        .padding()
        .background(Color(uiColor: .secondarySystemBackground))
        .cornerRadius(12)
    }

    private func errorPanel(_ message: String) -> some View {
        Label(message, systemImage: "exclamationmark.triangle")
            .foregroundColor(.orange)
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(uiColor: .secondarySystemBackground))
            .cornerRadius(12)
    }
}


// MARK: - PKCanvasView UIViewRepresentable

struct PKCanvasRepresentable: UIViewRepresentable {
    let canvasView: PKCanvasView

    func makeUIView(context: Context) -> PKCanvasView {
        canvasView.drawingPolicy = .pencilOnly   // Apple Pencil only; fingers pan/scroll
        canvasView.backgroundColor = .white
        canvasView.tool = PKInkingTool(.pen, color: .black, width: 3)
        return canvasView
    }

    func updateUIView(_ uiView: PKCanvasView, context: Context) {}
}


// MARK: - ViewModel

struct HandwritingResult {
    let latex: String
    let unicode: String
}

@MainActor
class HandwritingViewModel: ObservableObject {
    let canvasView = PKCanvasView()

    @Published var isRecognising = false
    @Published var result: HandwritingResult?
    @Published var errorMessage: String?
    @Published var isEditing = false
    @Published var editedUnicode = ""

    // Injected by parent — sends the handwriting_image WebSocket message
    // and calls onResult when handwriting_result arrives.
    var sendImage: ((Data, @escaping (HandwritingResult?, String?) -> Void) -> Void)?

    func clear() {
        canvasView.drawing = PKDrawing()
        result = nil
        errorMessage = nil
        isEditing = false
    }

    func undo() {
        canvasView.undoManager?.undo()
    }

    func recognise() {
        guard !isRecognising else { return }
        let drawing = canvasView.drawing
        guard !drawing.strokes.isEmpty else { return }

        // Render to UIImage with white background
        let bounds = drawing.bounds.insetBy(dx: -20, dy: -20)
        let scale = UIScreen.main.scale
        let image = drawing.image(from: bounds, scale: scale)

        guard let pngData = image.pngData() else {
            errorMessage = "Failed to render canvas to PNG"
            return
        }

        isRecognising = true
        errorMessage = nil
        result = nil

        sendImage?(pngData) { [weak self] result, error in
            guard let self else { return }
            self.isRecognising = false
            if let result {
                self.result = result
            } else {
                self.errorMessage = error ?? "Recognition failed"
            }
        }
    }
}
```

---

## Integration in ContentView Tab

```swift
TabView {
    CommandPadView(ws: wsManager)
        .tabItem { Label("Commands", systemImage: "hand.tap") }
    TrackpadView(ws: wsManager)
        .tabItem { Label("Trackpad", systemImage: "cursorarrow.motionlines") }
    ScientificKeypadView { expr in
        wsManager.dictate(expr)
    }
    .tabItem { Label("Keypad", systemImage: "function") }
    HandwritingCanvasView { expr in
        wsManager.dictate(expr)
    }
    .tabItem { Label("Handwrite", systemImage: "pencil.tip") }
    SettingsView()
        .tabItem { Label("Settings", systemImage: "gear") }
}
```

`wsManager.dictate(expr)` sends:
```json
{
  "type": "touch_command",
  "id": "<uuid>",
  "action": "DICTATE",
  "text": "<expression>",
  "params": {}
}
```

---

## WebSocket Manager — Handwriting Integration

```swift
// In WebSocketManager.swift — wire handwriting round-trip
func sendHandwritingImage(_ data: Data, completion: @escaping (HandwritingResult?, String?) -> Void) {
    let id = UUID().uuidString
    let b64 = data.base64EncodedString()
    pendingHandwriting[id] = completion    // store callback keyed by message id

    send([
        "type":  "handwriting_image",
        "id":    id,
        "image": b64,
        "width":  1024,   // match actual rendered size
        "height": 400,
    ])
}

// In receive handler:
case "handwriting_result":
    let id = msg["id"] as? String ?? ""
    if let cb = pendingHandwriting.removeValue(forKey: id) {
        if let error = msg["error"] as? String {
            cb(nil, error)
        } else {
            let latex   = msg["latex"]   as? String ?? ""
            let unicode = msg["unicode"] as? String ?? ""
            cb(HandwritingResult(latex: latex, unicode: unicode), nil)
        }
    }
```

---

## Notes on pix2tex

- Model download: ~1.5 GB on first `pip install pix2tex` (ViT + GPT-2 weights)
- First call loads the model in ~10s; subsequent calls are fast (~0.5–1s on RTX 5090)
- Works best on clean ink strokes against a white background — PencilKit's default output is ideal
- Handles both handwritten and printed math; handles multi-line expressions if they fit in the canvas
- Outputs standard LaTeX — compatible with Overleaf, VS Code LaTeX Workshop, Jupyter, etc.
- The `unicode` field is a best-effort conversion for pasting into plain text editors

---

## Tasks Added

See `tasks.md` — task 2.15 `HandwritingCanvasView`.
