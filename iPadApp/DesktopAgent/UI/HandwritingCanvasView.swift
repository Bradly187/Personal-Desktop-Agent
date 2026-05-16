import SwiftUI
import PencilKit

/// Apple Pencil canvas for handwritten math expressions.
/// PKCanvasView uses .pencilOnly policy — finger touches pan the view, do not draw.
/// On "Recognise" tap: renders PKDrawing to PNG → base64 → handwriting_image WebSocket message.
/// On receiving handwriting_result: displays LaTeX + unicode, allows edit before send.
/// Toolbar buttons use DesignTokens.Size.touchTargetCompact (64pt) minimum.
struct HandwritingCanvasView: View {
    @EnvironmentObject var wsManager: WebSocketManager

    @Environment(\.appTheme) private var theme

    @StateObject private var vm = HandwritingViewModel()
    @State private var showEditSheet = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Canvas
                PKCanvasRepresentable(drawing: $vm.drawing)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(theme.surfacePrimary)
                    .overlay(alignment: .bottomTrailing) {
                        canvasControls
                    }

                // Result bar (appears after recognition)
                if let result = vm.result {
                    resultBar(result)
                } else if vm.isRecognizing {
                    ProgressView("Recognizing…")
                        .padding(DesignTokens.Spacing.lg)
                }
            }
            .navigationTitle("Handwriting")
        }
        .onReceive(wsManager.$lastMessage.compactMap { $0 }) { msg in
            if case .handwritingResult(_, let latex, let unicode, let error) = msg {
                vm.handleResult(latex: latex, unicode: unicode, error: error)
            }
        }
    }

    // MARK: — Toolbar

    private var canvasControls: some View {
        HStack(spacing: DesignTokens.Spacing.md) {
            Button {
                vm.undo()
            } label: {
                Image(systemName: "arrow.uturn.backward")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.accent)
                    .frame(minWidth: DesignTokens.Size.touchTargetCompact,
                           minHeight: DesignTokens.Size.touchTargetCompact)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Undo")
            .accessibilityHint("Double-tap to undo last stroke")

            Button(role: .destructive) {
                vm.clear()
            } label: {
                Image(systemName: "trash")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.destructive)
                    .frame(minWidth: DesignTokens.Size.touchTargetCompact,
                           minHeight: DesignTokens.Size.touchTargetCompact)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Clear canvas")
            .accessibilityHint("Double-tap to erase all strokes")

            Button {
                vm.recognize(ws: wsManager)
            } label: {
                HStack(spacing: DesignTokens.Spacing.sm) {
                    Image(systemName: "wand.and.stars")
                        .font(.system(size: DesignTokens.Size.iconSize))
                    Text("Recognise")
                        .font(DesignTokens.Typography.caption)
                }
                .foregroundStyle(.white)
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                .background(theme.accent)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(vm.drawing.strokes.isEmpty || vm.isRecognizing)
            .accessibilityLabel("Recognise handwriting")
            .accessibilityHint("Double-tap to send drawing for recognition")
        }
        .padding(DesignTokens.Spacing.md)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        .padding(DesignTokens.Spacing.lg)
    }

    // MARK: — Result bar

    private func resultBar(_ result: HandwritingResult) -> some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
            if let latex = result.latex {
                Label(latex, systemImage: "function")
                    .font(DesignTokens.Typography.mono)
                    .foregroundStyle(theme.textPrimary)
            }
            if let error = result.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(theme.destructive)
                    .font(DesignTokens.Typography.caption)
            }
            HStack(spacing: DesignTokens.Spacing.md) {
                TextField("Unicode expression", text: $vm.editedUnicode)
                    .font(DesignTokens.Typography.mono)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Recognised expression")

                Button {
                    wsManager.sendCommand(action: "DICTATE", text: vm.editedUnicode)
                    vm.clear()
                } label: {
                    Text("Send")
                        .font(DesignTokens.Typography.body)
                        .foregroundStyle(.white)
                        .padding(.horizontal, DesignTokens.Spacing.lg)
                        .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                        .background(theme.accent)
                        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(vm.editedUnicode.isEmpty)
                .accessibilityLabel("Send expression")
                .accessibilityHint("Double-tap to type expression on PC")
            }
        }
        .padding(DesignTokens.Spacing.lg)
        .background(theme.surfaceSecondary)
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

        // Capture drawing state for background rendering
        let drawingCopy = drawing
        let scale = UIScreen.main.scale

        Task.detached(priority: .userInitiated) {
            let bounds = drawingCopy.bounds.insetBy(dx: -20, dy: -20)
            let image = drawingCopy.image(from: bounds, scale: scale)
            guard let pngData = image.pngData() else {
                await MainActor.run { self.isRecognizing = false }
                return
            }
            let b64 = pngData.base64EncodedString()
            await MainActor.run {
                ws.sendHandwritingImage(base64PNG: b64)
            }
        }
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

        // Provide a tool picker — deferred so it doesn't animate during the tab switch
        let picker = PKToolPicker()
        picker.addObserver(canvas)
        DispatchQueue.main.async {
            picker.setVisible(true, forFirstResponder: canvas)
            canvas.becomeFirstResponder()
        }

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
        init(drawing: Binding<PKDrawing>) { _drawing = drawing }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            drawing = canvasView.drawing
        }
    }
}
