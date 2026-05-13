import SwiftUI
import PencilKit

/// Apple Pencil canvas for handwritten math expressions.
/// PKCanvasView uses .pencilOnly policy — finger touches pan the view, do not draw.
/// On "Recognise" tap: renders PKDrawing to PNG → base64 → handwriting_image WebSocket message.
/// On receiving handwriting_result: displays LaTeX + unicode, allows edit before send.
struct HandwritingCanvasView: View {
    @EnvironmentObject var wsManager: WebSocketManager

    @StateObject private var vm = HandwritingViewModel()
    @State private var showEditSheet = false

    var body: some View {
        VStack(spacing: 0) {
            // Canvas
            PKCanvasRepresentable(drawing: $vm.drawing)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(.systemBackground))
                .overlay(alignment: .bottomTrailing) {
                    canvasControls
                }

            // Result bar (appears after recognition)
            if let result = vm.result {
                resultBar(result)
            } else if vm.isRecognizing {
                ProgressView("Recognizing…")
                    .padding()
            }
        }
        .navigationTitle("Handwriting")
        .onReceive(wsManager.$lastMessage.compactMap { $0 }) { msg in
            if case .handwritingResult(_, let latex, let unicode, let error) = msg {
                vm.handleResult(latex: latex, unicode: unicode, error: error)
            }
        }
    }

    // MARK: — Toolbar

    private var canvasControls: some View {
        HStack(spacing: 12) {
            Button {
                vm.undo()
            } label: {
                Label("Undo", systemImage: "arrow.uturn.backward")
                    .labelStyle(.iconOnly)
            }

            Button(role: .destructive) {
                vm.clear()
            } label: {
                Label("Clear", systemImage: "trash")
                    .labelStyle(.iconOnly)
            }

            Button {
                vm.recognize(ws: wsManager)
            } label: {
                Label("Recognise", systemImage: "wand.and.stars")
                    .labelStyle(.titleAndIcon)
            }
            .buttonStyle(.borderedProminent)
            .disabled(vm.drawing.strokes.isEmpty || vm.isRecognizing)
        }
        .padding()
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding()
    }

    // MARK: — Result bar

    private func resultBar(_ result: HandwritingResult) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let latex = result.latex {
                Label(latex, systemImage: "function")
                    .font(.system(.body, design: .monospaced))
            }
            if let error = result.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
                    .font(.caption)
            }
            HStack {
                TextField("Unicode expression", text: $vm.editedUnicode)
                    .font(.system(.body, design: .monospaced))
                    .textFieldStyle(.roundedBorder)

                Button("Send") {
                    wsManager.sendCommand(action: "DICTATE", text: vm.editedUnicode)
                    vm.clear()
                }
                .buttonStyle(.borderedProminent)
                .disabled(vm.editedUnicode.isEmpty)
            }
        }
        .padding()
        .background(Color(.secondarySystemGroupedBackground))
    }
}

// MARK: — ViewModel

struct HandwritingResult {
    var latex: String?
    var unicode: String?
    var error: String?
}

@MainActor
final class HandwritingViewModel: ObservableObject {
    @Published var drawing = PKDrawing()
    @Published var result: HandwritingResult?
    @Published var editedUnicode = ""
    @Published var isRecognizing = false

    private var canvasView: PKCanvasView?

    func setCanvasView(_ v: PKCanvasView) { canvasView = v }

    func undo() {
        canvasView?.undoManager?.undo()
    }

    func clear() {
        drawing = PKDrawing()
        result = nil
        editedUnicode = ""
        isRecognizing = false
    }

    func recognize(ws: WebSocketManager) {
        guard !drawing.strokes.isEmpty else { return }
        isRecognizing = true
        result = nil

        // Render drawing onto a white background so pix2tex receives dark-on-light
        // content regardless of device dark/light mode.  PKDrawing.image(from:scale:)
        // produces a transparent-background PNG; compositing here means the PC-side
        // recognizer never sees a solid-black image.
        let bounds = drawing.bounds.insetBy(dx: -20, dy: -20)
        let scale = UIScreen.main.scale
        let fmt = UIGraphicsImageRendererFormat()
        fmt.scale = scale
        let renderer = UIGraphicsImageRenderer(size: bounds.size, format: fmt)
        let image = renderer.image { ctx in
            UIColor.white.setFill()
            ctx.fill(CGRect(origin: .zero, size: bounds.size))
            drawing.image(from: bounds, scale: scale).draw(at: .zero)
        }
        guard let pngData = image.pngData() else {
            isRecognizing = false
            return
        }
        let b64 = pngData.base64EncodedString()
        ws.sendHandwritingImage(base64PNG: b64)
    }

    func handleResult(latex: String?, unicode: String?, error: String?) {
        isRecognizing = false
        result = HandwritingResult(latex: latex, unicode: unicode, error: error)
        editedUnicode = unicode ?? latex ?? ""
    }
}

// MARK: — PKCanvasView wrapper

struct PKCanvasRepresentable: UIViewRepresentable {
    @Binding var drawing: PKDrawing

    func makeUIView(context: Context) -> PKCanvasView {
        let canvas = PKCanvasView()
        canvas.drawing = drawing
        canvas.drawingPolicy = .pencilOnly        // fingers pan, Pencil draws
        canvas.backgroundColor = .systemBackground
        canvas.isOpaque = true
        canvas.delegate = context.coordinator

        // Provide a tool picker; stored on the coordinator to prevent deallocation.
        let picker = PKToolPicker()
        picker.setVisible(true, forFirstResponder: canvas)
        picker.addObserver(canvas)
        context.coordinator.toolPicker = picker
        canvas.becomeFirstResponder()

        return canvas
    }

    func updateUIView(_ uiView: PKCanvasView, context: Context) {
        if uiView.drawing != drawing {
            uiView.drawing = drawing
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(drawing: $drawing) }

    final class Coordinator: NSObject, PKCanvasViewDelegate {
        @Binding var drawing: PKDrawing
        var toolPicker: PKToolPicker?   // retains picker so it isn't immediately deallocated

        init(drawing: Binding<PKDrawing>) { _drawing = drawing }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            drawing = canvasView.drawing
        }
    }
}
