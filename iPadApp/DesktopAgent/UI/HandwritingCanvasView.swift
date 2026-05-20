import SwiftUI
import PencilKit
import Vision

// MARK: — Mode

enum WriteMode: String, CaseIterable {
    case math  = "Math"
    case text  = "Text"

    var icon: String {
        switch self {
        case .math: return "function"
        case .text: return "text.cursor"
        }
    }
}

// MARK: — Main view

/// Handwriting canvas for both math expressions and freeform prompt input.
///
/// Math mode  — sends drawing to the PC pix2tex server for LaTeX + unicode OCR.
/// Text mode  — runs on-device Vision (VNRecognizeTextRequest) for plain text;
///              no round-trip to the PC, works offline.
///
/// Result bar offers:
///   Send          — DICTATE: pastes result into the currently focused PC field
///   Click & Send  — CLICK at current cursor position to focus a field, then DICTATE
///
/// Apple Pencil draws; fingers pan. All toolbar buttons ≥ 64pt touch targets.
struct HandwritingCanvasView: View {
    @EnvironmentObject var wsManager: WebSocketManager

    @Environment(\.appTheme) private var theme

    @StateObject private var vm = HandwritingViewModel()
    @State private var mode: WriteMode = .math

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {

                // Mode selector
                Picker("Mode", selection: $mode) {
                    ForEach(WriteMode.allCases, id: \.self) { m in
                        Label(m.rawValue, systemImage: m.icon).tag(m)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.vertical, DesignTokens.Spacing.sm)
                .onChange(of: mode) { _, _ in vm.clear() }

                // Drawing canvas
                PKCanvasRepresentable(drawing: $vm.drawing)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(theme.surfacePrimary)
                    .overlay(alignment: .bottomTrailing) {
                        canvasControls
                    }

                // Result bar
                if let result = vm.result {
                    resultBar(result)
                } else if vm.isRecognizing {
                    HStack(spacing: DesignTokens.Spacing.sm) {
                        ProgressView().controlSize(.small)
                        Text(mode == .math ? "Sending to PC for LaTeX recognition…"
                                           : "Recognizing text on-device…")
                            .font(DesignTokens.Typography.caption)
                            .foregroundStyle(theme.textSecondary)
                    }
                    .padding(DesignTokens.Spacing.lg)
                }
            }
            .navigationTitle("Write")
        }
        .onReceive(wsManager.messageStream) { msg in
            // Math mode: PC returns handwriting_result
            if case .handwritingResult(_, let latex, let unicode, let error) = msg {
                vm.handleResult(latex: latex, unicode: unicode, error: error)
            }
        }
    }

    // MARK: — Canvas controls (bottom-right overlay)

    private var canvasControls: some View {
        HStack(spacing: DesignTokens.Spacing.md) {

            // Undo
            Button { vm.undo() } label: {
                Image(systemName: "arrow.uturn.backward")
                    .iconButton(theme: theme)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Undo last stroke")

            // Clear
            Button(role: .destructive) { vm.clear() } label: {
                Image(systemName: "trash")
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.destructive)
                    .frame(minWidth: DesignTokens.Size.touchTargetCompact,
                           minHeight: DesignTokens.Size.touchTargetCompact)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Clear canvas")

            // Recognise
            Button {
                if mode == .math {
                    vm.recognizeMath(ws: wsManager)
                } else {
                    vm.recognizeText()
                }
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
                .background(vm.drawing.strokes.isEmpty || vm.isRecognizing
                            ? theme.accent.opacity(0.4) : theme.accent)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(vm.drawing.strokes.isEmpty || vm.isRecognizing)
            .accessibilityLabel("Recognise handwriting")
        }
        .padding(DesignTokens.Spacing.md)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
        .padding(DesignTokens.Spacing.lg)
    }

    // MARK: — Result bar

    private func resultBar(_ result: HandwritingResult) -> some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {

            // LaTeX preview (math mode only, collapsible)
            if mode == .math, let latex = result.latex {
                Text(latex)
                    .font(DesignTokens.Typography.mono)
                    .foregroundStyle(theme.textSecondary)
                    .lineLimit(2)
                    .accessibilityLabel("LaTeX: \(latex)")
            }

            // Error
            if let error = result.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(theme.destructive)
                    .font(DesignTokens.Typography.caption)
            }

            // Editable output + send buttons
            HStack(spacing: DesignTokens.Spacing.sm) {

                TextField(mode == .math ? "Expression" : "Recognised text",
                          text: $vm.editedText, axis: .vertical)
                    .font(DesignTokens.Typography.mono)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...3)
                    .accessibilityLabel("Recognised output — edit before sending")

                VStack(spacing: DesignTokens.Spacing.sm) {

                    // Send — DICTATE into currently active PC field
                    sendButton(
                        label: "Send",
                        icon: "paperplane.fill",
                        help: "Paste into active field"
                    ) {
                        wsManager.sendCommand(action: "DICTATE", text: vm.editedText)
                        vm.clear()
                    }

                    // Click & Send — click at cursor position first, then DICTATE
                    sendButton(
                        label: "Click\n& Send",
                        icon: "cursorarrow.click",
                        help: "Click current cursor position then paste",
                        accent: false
                    ) {
                        // CLICK activates the field under the cursor; short delay
                        // lets the field focus before DICTATE pastes.
                        wsManager.sendCommand(action: "CLICK", text: "")
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                            wsManager.sendCommand(action: "DICTATE", text: vm.editedText)
                            vm.clear()
                        }
                    }
                }
            }
        }
        .padding(DesignTokens.Spacing.lg)
        .background(theme.surfaceSecondary)
    }

    @ViewBuilder
    private func sendButton(
        label: String,
        icon: String,
        help: String,
        accent: Bool = true,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            VStack(spacing: 2) {
                Image(systemName: icon)
                    .font(.system(size: 14, weight: .semibold))
                Text(label)
                    .font(.system(size: 11, weight: .medium))
                    .multilineTextAlignment(.center)
            }
            .foregroundStyle(.white)
            .frame(minWidth: 64, minHeight: DesignTokens.Size.touchTargetCompact)
            .background(accent ? theme.accent : theme.accent.opacity(0.7))
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(vm.editedText.isEmpty)
        .accessibilityLabel(label.replacingOccurrences(of: "\n", with: " "))
        .accessibilityHint(help)
    }
}

// MARK: — ViewModel

struct HandwritingResult {
    var latex: String?
    var error: String?
}

@MainActor
final class HandwritingViewModel: ObservableObject {
    @Published var drawing = PKDrawing()
    @Published var result: HandwritingResult?
    @Published var editedText = ""
    @Published var isRecognizing = false

    private var canvasView: PKCanvasView?

    func setCanvasView(_ v: PKCanvasView) { canvasView = v }

    func undo() { canvasView?.undoManager?.undo() }

    func clear() {
        drawing = PKDrawing()
        result = nil
        editedText = ""
        isRecognizing = false
    }

    // MARK: Math mode — pix2tex via PC bridge

    func recognizeMath(ws: WebSocketManager) {
        guard !drawing.strokes.isEmpty else { return }
        isRecognizing = true
        result = nil
        editedText = ""

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
            await MainActor.run { ws.sendHandwritingImage(base64PNG: b64) }
        }
    }

    /// Called by ContentView when the PC bridge returns handwriting_result.
    func handleResult(latex: String?, unicode: String?, error: String?) {
        isRecognizing = false
        result = HandwritingResult(latex: latex, error: error)
        // Prefer unicode (human-readable); fall back to LaTeX for math mode
        editedText = unicode ?? latex ?? ""
    }

    // MARK: Text mode — on-device Vision (VNRecognizeTextRequest)

    func recognizeText() {
        guard !drawing.strokes.isEmpty else { return }
        isRecognizing = true
        result = nil
        editedText = ""

        let drawingCopy = drawing
        let scale = UIScreen.main.scale

        Task.detached(priority: .userInitiated) {
            let bounds = drawingCopy.bounds.insetBy(dx: -20, dy: -20)
            let uiImage = drawingCopy.image(from: bounds, scale: scale)
            guard let cgImage = uiImage.cgImage else {
                await MainActor.run { self.isRecognizing = false }
                return
            }

            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = true
            request.automaticallyDetectsLanguage = true

            let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
            do {
                try handler.perform([request])
            } catch {
                await MainActor.run {
                    self.isRecognizing = false
                    self.result = HandwritingResult(error: "Vision error: \(error.localizedDescription)")
                }
                return
            }

            let recognized = (request.results ?? [])
                .compactMap { $0.topCandidates(1).first?.string }
                .joined(separator: " ")

            await MainActor.run {
                self.isRecognizing = false
                if recognized.isEmpty {
                    self.result = HandwritingResult(error: "No text recognised — try writing larger or more clearly")
                } else {
                    self.result = HandwritingResult()
                    self.editedText = recognized
                }
            }
        }
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

        // Fix #10: Tool picker is retained by the Coordinator (not a local variable).
        let picker = context.coordinator.toolPicker
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
        let toolPicker = PKToolPicker()

        init(drawing: Binding<PKDrawing>) { _drawing = drawing }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            drawing = canvasView.drawing
        }
    }
}

// MARK: — Convenience modifier

private extension Image {
    func iconButton(theme: AppTheme) -> some View {
        self
            .font(.system(size: DesignTokens.Size.iconSize))
            .foregroundStyle(theme.accent)
            .frame(minWidth: DesignTokens.Size.touchTargetCompact,
                   minHeight: DesignTokens.Size.touchTargetCompact)
            .contentShape(Rectangle())
    }
}
