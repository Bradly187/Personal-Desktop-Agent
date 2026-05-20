import SwiftUI

/// Scientific calculator keypad for entering math expressions and symbols.
/// Evaluates expressions on-device (NSExpression) and sends via DICTATE.
/// All key buttons use DesignTokens.Size.touchTargetCompact (64pt) minimum.
struct ScientificKeypadView: View {
    @EnvironmentObject var wsManager: WebSocketManager

    @Environment(\.appTheme) private var theme

    @State private var expression = ""
    @State private var preview = ""
    @State private var isScientific = false
    @State private var lastAnswer = ""

    // MARK: — Layout

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Display
                displayPanel
                Divider()
                // Mode toggle
                Picker("Mode", selection: $isScientific) {
                    Text("Basic").tag(false)
                    Text("Scientific").tag(true)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, DesignTokens.Spacing.lg)
                .padding(.vertical, DesignTokens.Spacing.sm)
                // Keys
                keyGrid
            }
            .navigationTitle("Keypad")
        }
    }

    private var displayPanel: some View {
        VStack(alignment: .trailing, spacing: DesignTokens.Spacing.xs) {
            ScrollView(.horizontal, showsIndicators: false) {
                Text(expression.isEmpty ? "0" : expression)
                    .font(.system(size: 32, weight: .light, design: .monospaced))
                    .foregroundStyle(theme.textPrimary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            Text(preview)
                .font(DesignTokens.Typography.mono)
                .foregroundStyle(theme.textSecondary)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .lineLimit(1)
        }
        .padding(DesignTokens.Spacing.lg)
        .frame(minHeight: 90, alignment: .bottomTrailing)
    }

    private var keyGrid: some View {
        let keys: [[KeyDef]] = isScientific ? scientificKeys : basicKeys
        return VStack(spacing: DesignTokens.Spacing.sm) {
            ForEach(keys.indices, id: \.self) { row in
                HStack(spacing: DesignTokens.Spacing.sm) {
                    ForEach(keys[row]) { key in
                        KeyButton(key: key) { tap(key) }
                    }
                }
            }
        }
        .padding(.horizontal, DesignTokens.Spacing.sm)
        .padding(.bottom, DesignTokens.Spacing.md)
    }

    // MARK: — Key definitions

    private var basicKeys: [[KeyDef]] {
        [
            [.ac, .del, .paren, .div],
            [.n("7"), .n("8"), .n("9"), .op("×")],
            [.n("4"), .n("5"), .n("6"), .op("−")],
            [.n("1"), .n("2"), .n("3"), .op("+")],
            [.n("0"), .dot, .ans, .send],
        ]
    }

    private var scientificKeys: [[KeyDef]] {
        [
            [.fn("sin"), .fn("cos"), .fn("tan"), .fn("log"), .fn("ln")],
            [.fn("sin⁻¹"), .fn("cos⁻¹"), .fn("tan⁻¹"), .fn("log₂"), .fn("√")],
            [.sym("π"), .sym("e"), .op("^"), .sym("!"), .fn("|x|")],
            [.sym("±"), .sym("EE"), .sym("mod"), .paren, .del],
            [.n("7"), .n("8"), .n("9"), .div, .ac],
            [.n("4"), .n("5"), .n("6"), .op("×"), .op("−")],
            [.n("1"), .n("2"), .n("3"), .op("+"), .ans],
            [.n("0"), .dot, .send],
        ]
    }

    // MARK: — Tap handler

    private func tap(_ key: KeyDef) {
        switch key {
        case .n(let s), .sym(let s):
            expression += s
        case .op(let s):
            expression += s
        case .fn(let name):
            expression += functionToken(name)
        case .paren:
            expression += parenToAdd()
        case .dot:
            expression += "."
        case .del:
            expression = String(expression.dropLast())
        case .ac:
            expression = ""
            preview = ""
        case .ans:
            expression += lastAnswer.isEmpty ? "0" : lastAnswer
        case .send:
            sendExpression()
            return
        case .div:
            expression += "÷"
        }
        evaluate()
    }

    private func parenToAdd() -> String {
        let opens  = expression.filter { $0 == "(" }.count
        let closes = expression.filter { $0 == ")" }.count
        return opens > closes ? ")" : "("
    }

    private func functionToken(_ name: String) -> String {
        switch name {
        case "sin":   return "sin("
        case "cos":   return "cos("
        case "tan":   return "tan("
        case "sin⁻¹": return "asin("
        case "cos⁻¹": return "acos("
        case "tan⁻¹": return "atan("
        case "log":   return "log("
        case "ln":    return "ln("
        case "log₂":  return "log2("
        case "√":     return "sqrt("
        case "|x|":   return "abs("
        default:      return name + "("
        }
    }

    // MARK: — Evaluation

    private func evaluate() {
        guard !expression.isEmpty else { preview = ""; return }
        if let result = evalExpression(expression) {
            preview = "= \(formatNumber(result))"
        } else {
            preview = ""
        }
    }

    private func evalExpression(_ expr: String) -> Double? {
        // Normalise display symbols to ASCII operators
        var s = expr
        s = s.replacingOccurrences(of: "×", with: "*")
        s = s.replacingOccurrences(of: "÷", with: "/")
        s = s.replacingOccurrences(of: "−", with: "-")
        s = s.replacingOccurrences(of: "π", with: "3.14159265358979")

        // Replace standalone "e" (Euler's number) only when surrounded by
        // non-alphanumeric characters, to avoid corrupting tokens like "1e6".
        // Simple approach: only replace if "e" appears as the whole expression
        // or adjacent to operators/parens — use word-boundary via regex-free check.
        s = replaceEulerE(in: s)

        // ^ (power): NSExpression does NOT support ** or ^.
        // Evaluate power sub-expressions via Swift.pow before handing to NSExpression.
        // If expression contains ^ and we can't safely rewrite it, return nil
        // (preview stays blank — the raw expression still sends via DICTATE).
        if s.contains("^") {
            return evalWithPower(s)
        }

        return nsExpressionEval(s)
    }

    /// Replace lone "e" (Euler's number) but not "e" inside numeric tokens like 1e6.
    private func replaceEulerE(in s: String) -> String {
        var result = ""
        var chars = Array(s)
        var i = 0
        while i < chars.count {
            let c = chars[i]
            if c == "e" {
                let prevIsDigit = i > 0 && chars[i - 1].isNumber
                let nextIsDigit = i + 1 < chars.count && chars[i + 1].isNumber
                // Keep "e" in scientific notation (1e6) but replace standalone e
                if prevIsDigit && nextIsDigit {
                    result.append(c)  // part of scientific notation — leave alone
                } else {
                    result += "2.71828182845905"
                }
            } else {
                result.append(c)
            }
            i += 1
        }
        return result
    }

    /// Evaluate expressions containing ^ by splitting on the operator and using pow().
    /// Only handles simple "a^b" with no further nesting — returns nil otherwise.
    private func evalWithPower(_ s: String) -> Double? {
        // Find the last ^ (right-associative is fine for simple cases)
        guard let caretRange = s.range(of: "^", options: .backwards) else { return nil }
        let baseStr  = String(s[s.startIndex ..< caretRange.lowerBound])
        let expStr   = String(s[caretRange.upperBound...])
        guard !baseStr.isEmpty, !expStr.isEmpty else { return nil }
        // Recurse for chained powers (e.g. 2^3^4); avoid expressions with further ^
        guard let base = nsExpressionEval(baseStr),
              let exp  = nsExpressionEval(expStr) else { return nil }
        let result = Foundation.pow(base, exp)
        return result.isFinite ? result : nil
    }

    /// Thin wrapper around NSExpression that returns nil on any failure.
    /// Pre-condition: caller has validated the expression string does not
    /// contain "**" or "^" (those crash NSExpression on iOS 26).
    private func nsExpressionEval(_ s: String) -> Double? {
        guard !s.isEmpty else { return nil }
        // Double-check: never pass ** to NSExpression (iOS 26 throws ObjC exception)
        guard !s.contains("**"), !s.contains("^") else { return nil }

        let opens  = s.filter { $0 == "(" }.count
        let closes = s.filter { $0 == ")" }.count
        guard opens == closes else { return nil }

        guard let lastChar = s.last,
              lastChar != "(",
              lastChar != "+",
              lastChar != "-",
              lastChar != "*",
              lastChar != "/" else { return nil }

        let nsExpr = NSExpression(format: s)
        guard let v = nsExpr.expressionValue(with: nil, context: nil) as? NSNumber else {
            return nil
        }
        let d = v.doubleValue
        return d.isFinite ? d : nil
    }

    private func formatNumber(_ n: Double) -> String {
        if n == n.rounded() && abs(n) < 1e12 {
            return String(Int(n))
        }
        return String(format: "%g", n)
    }

    // MARK: — Send

    private func sendExpression() {
        let toSend = expression
        if let result = evalExpression(expression) {
            lastAnswer = formatNumber(result)
        }
        wsManager.sendCommand(action: "DICTATE", text: toSend)
        expression = ""
        preview = ""
    }
}

// MARK: — Key model

enum KeyDef: Identifiable {
    case n(String)
    case op(String)
    case fn(String)
    case sym(String)
    case paren, dot, del, ac, ans, send, div

    var id: String {
        switch self {
        case .n(let s), .op(let s), .fn(let s), .sym(let s): return s
        case .paren: return "()"
        case .dot:   return "."
        case .del:   return "⌫"
        case .ac:    return "AC"
        case .ans:   return "ANS"
        case .send:  return "Send"
        case .div:   return "÷"
        }
    }

    var label: String {
        switch self {
        case .n(let s), .op(let s), .fn(let s), .sym(let s): return s
        case .paren: return "(  )"
        case .dot:   return "."
        case .del:   return "⌫"
        case .ac:    return "AC"
        case .ans:   return "ANS"
        case .send:  return "Send"
        case .div:   return "÷"
        }
    }

    var accent: Bool {
        switch self {
        case .send: return true
        default:    return false
        }
    }

    var isWide: Bool {
        switch self {
        case .send: return true
        default:    return false
        }
    }
}

// MARK: — Key button view (DesignTokens.Size.touchTargetCompact = 64pt minimum)

private struct KeyButton: View {
    let key: KeyDef
    let action: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        Button(action: action) {
            Text(key.label)
                .font(.system(size: 18, weight: .medium, design: .rounded))
                .foregroundStyle(key.accent ? .white : theme.textPrimary)
                .frame(minWidth: DesignTokens.Size.touchTargetCompact,
                       maxWidth: key.isWide ? .infinity : nil,
                       minHeight: DesignTokens.Size.touchTargetCompact)
                .background(key.accent ? theme.accent : theme.surfaceSecondary)
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.sm))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(key.label)
        .accessibilityHint("Double-tap to enter \(key.label)")
    }
}
