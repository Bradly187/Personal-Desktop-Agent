"use strict";
/**
 * Desktop Agent Bridge — VS Code extension
 *
 * Exposes a WebSocket server on localhost:8767 so the Personal Desktop Agent
 * Python backend can read IDE state and send edit commands without screen-scraping.
 *
 * Supported actions (JSON request → JSON response):
 *   get_editor_context  → file, language, cursor, selection, context_above/below, diagnostics
 *   get_git_context     → branch, ahead, behind, staged, unstaged, last_commit
 *   apply_edit          → replace a range in a file; saves; triggers LSP re-check
 *   run_terminal        → run a command in the integrated terminal; capture output
 *   open_file           → open a file and jump to a line
 *   get_diagnostics     → all workspace diagnostics (errors/warnings)
 *   ping                → { pong: true }
 *
 * Security: the WebSocket handshake requires a shared token (?token=…) stored at
 * ~/.claude/desktop_agent_bridge/token. The Python backend reads/generates the
 * same file (inference/bridge_protocol.py) — keep the two sides in sync.
 *
 * Model picker: "Desktop Agent: Select Dev-Agent Model" reads the roster the
 * backend publishes (roster.json) and writes the user's pick to override.json,
 * which the backend's ModelRouter honors for all dev-agent queries.
 *
 * Install: npm install && npm run compile → 'code --install-extension' the .vsix
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const WebSocket = __importStar(require("ws"));
const fs = __importStar(require("fs"));
const os = __importStar(require("os"));
const path = __importStar(require("path"));
const crypto = __importStar(require("crypto"));
// ---------------------------------------------------------------------------
// Shared file contract (mirrors inference/bridge_protocol.py)
// ---------------------------------------------------------------------------
const BRIDGE_DIR = path.join(os.homedir(), '.claude', 'desktop_agent_bridge');
const TOKEN_FILE = path.join(BRIDGE_DIR, 'token');
const ROSTER_FILE = path.join(BRIDGE_DIR, 'roster.json');
const OVERRIDE_FILE = path.join(BRIDGE_DIR, 'override.json');
/** Read the shared token, generating it (0600) if absent. Never overwrites. */
function ensureToken() {
    try {
        fs.mkdirSync(BRIDGE_DIR, { recursive: true });
    }
    catch { /* ignore */ }
    try {
        const existing = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
        if (existing) {
            return existing;
        }
    }
    catch { /* not present yet */ }
    const token = crypto.randomBytes(32).toString('hex');
    try {
        // wx = create-exclusive: if the backend created it first, fall back to read.
        fs.writeFileSync(TOKEN_FILE, token, { flag: 'wx', mode: 0o600 });
        return token;
    }
    catch {
        try {
            return fs.readFileSync(TOKEN_FILE, 'utf8').trim() || null;
        }
        catch {
            return null;
        }
    }
}
/** Constant-time token comparison; false on any length/format mismatch. */
function tokenMatches(provided, expected) {
    if (!expected) {
        // No server token could be established (unwritable home) — fail closed.
        return false;
    }
    if (!provided) {
        return false;
    }
    const a = Buffer.from(provided);
    const b = Buffer.from(expected);
    if (a.length !== b.length) {
        return false;
    }
    try {
        return crypto.timingSafeEqual(a, b);
    }
    catch {
        return false;
    }
}
function readCurrentOverride() {
    try {
        const data = JSON.parse(fs.readFileSync(OVERRIDE_FILE, 'utf8'));
        return typeof data?.model === 'string' && data.model ? data.model : null;
    }
    catch {
        return null;
    }
}
function readRoster() {
    try {
        const data = JSON.parse(fs.readFileSync(ROSTER_FILE, 'utf8'));
        return Array.isArray(data?.models) ? data.models.filter((m) => typeof m === 'string') : [];
    }
    catch {
        return [];
    }
}
// ---------------------------------------------------------------------------
// Extension state
// ---------------------------------------------------------------------------
let _server = null;
let _statusBar;
let _clientCount = 0;
let _log;
let _token = null;
// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------
function activate(context) {
    _log = vscode.window.createOutputChannel('Desktop Agent Bridge');
    _log.appendLine('[Desktop Agent Bridge] Activating...');
    _token = ensureToken();
    if (!_token) {
        _log.appendLine('[Bridge] WARNING: could not establish auth token — connections will be refused.');
    }
    _statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    _statusBar.command = 'desktopAgent.showStatus';
    context.subscriptions.push(_statusBar);
    // Register commands
    context.subscriptions.push(vscode.commands.registerCommand('desktopAgent.restartBridge', () => {
        _token = ensureToken();
        stopServer();
        startServer(context);
    }), vscode.commands.registerCommand('desktopAgent.showStatus', () => {
        const port = getPort();
        const pin = readCurrentOverride() ?? 'Auto (domain routing)';
        vscode.window.showInformationMessage(`Desktop Agent Bridge: ${_clientCount} client(s) on port ${port} — dev model: ${pin}`);
    }), vscode.commands.registerCommand('desktopAgent.selectModel', selectModel));
    startServer(context);
}
function deactivate() {
    stopServer();
    _log.dispose();
}
// ---------------------------------------------------------------------------
// Model picker
// ---------------------------------------------------------------------------
const AUTO_LABEL = '$(sync) Auto (domain routing)';
async function selectModel() {
    const roster = readRoster();
    if (roster.length === 0) {
        vscode.window.showWarningMessage('Desktop Agent: no model roster yet. Start the Python backend (it publishes the roster on launch), then try again.');
        return;
    }
    const current = readCurrentOverride();
    const items = [
        {
            label: AUTO_LABEL,
            description: current === null ? '• current' : undefined,
            detail: 'Let the backend pick a model per domain by VRAM fit (default).',
        },
        ...roster.map((m) => ({
            label: m,
            description: current === m ? '• current' : undefined,
            detail: 'Pin this model for all dev-agent queries (code, math, plan, general).',
        })),
    ];
    const choice = await vscode.window.showQuickPick(items, {
        title: 'Desktop Agent — Dev-Agent Model',
        placeHolder: 'Pin a model for dev-agent queries, or Auto for domain routing',
    });
    if (!choice) {
        return;
    }
    const model = choice.label === AUTO_LABEL ? null : choice.label;
    try {
        fs.mkdirSync(BRIDGE_DIR, { recursive: true });
        fs.writeFileSync(OVERRIDE_FILE, JSON.stringify({ model }), 'utf8');
    }
    catch (err) {
        vscode.window.showErrorMessage(`Desktop Agent: failed to write model override: ${err}`);
        return;
    }
    _log.appendLine(`[Bridge] Dev-agent model override set to: ${model ?? 'Auto'}`);
    updateStatusBar();
    vscode.window.showInformationMessage(`Desktop Agent: dev model → ${model ?? 'Auto (domain routing)'}`);
}
// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------
function getPort() {
    return vscode.workspace.getConfiguration('desktopAgent').get('port', 8767);
}
function startServer(context) {
    const port = getPort();
    try {
        _server = new WebSocket.Server({
            host: '127.0.0.1',
            port,
            // Reject the handshake unless ?token= matches the shared secret.
            verifyClient: (info, cb) => {
                let provided = null;
                try {
                    const q = (info.req.url || '').split('?')[1] || '';
                    provided = new URLSearchParams(q).get('token');
                }
                catch { /* malformed URL → reject below */ }
                if (tokenMatches(provided, _token)) {
                    cb(true);
                }
                else {
                    _log.appendLine('[Bridge] Rejected unauthenticated connection (bad/missing token).');
                    cb(false, 4401, 'Unauthorized');
                }
            },
        });
        _server.on('listening', () => {
            _log.appendLine(`[Bridge] WebSocket server listening on ws://127.0.0.1:${port} (token required)`);
            updateStatusBar();
        });
        _server.on('connection', (ws) => {
            _clientCount++;
            _log.appendLine(`[Bridge] Client connected (total: ${_clientCount})`);
            updateStatusBar();
            ws.on('message', async (raw) => {
                let req;
                try {
                    req = JSON.parse(raw.toString());
                }
                catch {
                    ws.send(JSON.stringify({ ok: false, error: 'Invalid JSON' }));
                    return;
                }
                const resp = await handleRequest(req);
                ws.send(JSON.stringify(resp));
            });
            ws.on('close', () => {
                _clientCount = Math.max(0, _clientCount - 1);
                _log.appendLine(`[Bridge] Client disconnected (total: ${_clientCount})`);
                updateStatusBar();
            });
            ws.on('error', (err) => {
                _log.appendLine(`[Bridge] Client error: ${err.message}`);
            });
        });
        _server.on('error', (err) => {
            _log.appendLine(`[Bridge] Server error: ${err.message}`);
            vscode.window.showErrorMessage(`Desktop Agent Bridge error: ${err.message}`);
            updateStatusBar(true);
        });
    }
    catch (err) {
        _log.appendLine(`[Bridge] Failed to start: ${err}`);
        updateStatusBar(true);
    }
}
function stopServer() {
    if (_server) {
        _server.close();
        _server = null;
        _clientCount = 0;
        _log.appendLine('[Bridge] Server stopped');
        updateStatusBar();
    }
}
function updateStatusBar(error = false) {
    if (error) {
        _statusBar.text = '$(error) Agent Bridge: ERROR';
        _statusBar.tooltip = 'Desktop Agent Bridge failed to start. Check Output > Desktop Agent Bridge.';
        _statusBar.color = new vscode.ThemeColor('errorForeground');
    }
    else if (_server) {
        const icon = _clientCount > 0 ? '$(plug)' : '$(circle-outline)';
        const pin = readCurrentOverride();
        _statusBar.text = `${icon} Agent: ${_clientCount}${pin ? ` · ${pin}` : ''}`;
        _statusBar.tooltip = `Desktop Agent Bridge — ${_clientCount} client(s) on port ${getPort()}\nDev model: ${pin ?? 'Auto (domain routing)'}\nClick for status.`;
        _statusBar.color = undefined;
    }
    else {
        _statusBar.text = '$(circle-slash) Agent Bridge: OFF';
        _statusBar.tooltip = 'Desktop Agent Bridge is stopped.';
        _statusBar.color = undefined;
    }
    _statusBar.show();
}
// ---------------------------------------------------------------------------
// Request dispatcher
// ---------------------------------------------------------------------------
async function handleRequest(req) {
    const id = req.id;
    try {
        switch (req.action) {
            case 'ping':
                return { id, ok: true, data: { pong: true, ts: Date.now() } };
            case 'get_editor_context':
                return { id, ok: true, data: await getEditorContext() };
            case 'get_git_context':
                return { id, ok: true, data: await getGitContext() };
            case 'apply_edit':
                await applyEdit(req.file, req.range, req.text);
                return { id, ok: true, data: { applied: true } };
            case 'run_terminal': {
                const cmd = req.command;
                const cwd = req.cwd;
                const result = await runInTerminal(cmd, cwd);
                return { id, ok: true, data: result };
            }
            case 'open_file': {
                const filePath = req.file;
                const line = req.line ?? 0;
                await openFile(filePath, line);
                return { id, ok: true, data: { opened: true } };
            }
            case 'get_diagnostics':
                return { id, ok: true, data: getAllDiagnostics() };
            default:
                return { id, ok: false, error: `Unknown action: ${req.action}` };
        }
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        _log.appendLine(`[Bridge] Error handling ${req.action}: ${msg}`);
        return { id, ok: false, error: msg };
    }
}
// ---------------------------------------------------------------------------
// Editor context
// ---------------------------------------------------------------------------
async function getEditorContext() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        return null;
    }
    const doc = editor.document;
    const sel = editor.selection;
    const contextLines = vscode.workspace
        .getConfiguration('desktopAgent')
        .get('contextLines', 50);
    const aboveStart = new vscode.Position(Math.max(0, sel.active.line - contextLines), 0);
    const aboveEnd = new vscode.Position(sel.active.line, 0);
    const belowStart = new vscode.Position(sel.active.line + 1, 0);
    const belowEnd = new vscode.Position(Math.min(doc.lineCount - 1, sel.active.line + contextLines + 1), 0);
    const diags = vscode.languages
        .getDiagnostics(doc.uri)
        .filter(d => d.range.contains(sel.active))
        .map(d => ({
        severity: vscode.DiagnosticSeverity[d.severity].toLowerCase(),
        message: d.message,
        source: d.source,
        range: {
            start: { line: d.range.start.line, char: d.range.start.character },
            end: { line: d.range.end.line, char: d.range.end.character },
        },
    }));
    return {
        file: doc.fileName,
        language: doc.languageId,
        cursor: { line: sel.active.line, char: sel.active.character },
        selection: sel.isEmpty ? null : doc.getText(sel),
        selection_range: sel.isEmpty ? null : {
            start: { line: sel.start.line, char: sel.start.character },
            end: { line: sel.end.line, char: sel.end.character },
        },
        context_above: doc.getText(new vscode.Range(aboveStart, aboveEnd)),
        context_below: doc.getText(new vscode.Range(belowStart, belowEnd)),
        total_lines: doc.lineCount,
        is_dirty: doc.isDirty,
        diagnostics_at_cursor: diags,
    };
}
// ---------------------------------------------------------------------------
// Git context
// ---------------------------------------------------------------------------
async function getGitContext() {
    try {
        // Access VS Code's built-in git extension
        const gitExtension = vscode.extensions.getExtension('vscode.git');
        if (!gitExtension) {
            return { error: 'vscode.git extension not available' };
        }
        const api = gitExtension.isActive
            ? gitExtension.exports.getAPI(1)
            : (await gitExtension.activate()).getAPI(1);
        const repo = api.repositories[0];
        if (!repo) {
            return { error: 'No git repository open' };
        }
        const head = repo.state.HEAD;
        const staged = repo.state.indexChanges.map(c => ({
            path: c.uri.fsPath,
            status: statusToString(c.status),
        }));
        const unstaged = repo.state.workingTreeChanges.map(c => ({
            path: c.uri.fsPath,
            status: statusToString(c.status),
        }));
        return {
            branch: head?.name ?? null,
            commit: head?.commit ?? null,
            ahead: head?.ahead ?? 0,
            behind: head?.behind ?? 0,
            staged,
            unstaged,
            merge_conflicts: repo.state.mergeChanges.length,
        };
    }
    catch (err) {
        return { error: String(err) };
    }
}
function statusToString(status) {
    // VS Code Status enum: INDEX_MODIFIED=0, INDEX_ADDED=1, INDEX_DELETED=2,
    // MODIFIED=5, UNTRACKED=7, DELETED=6, etc.
    const map = {
        0: 'M', 1: 'A', 2: 'D', 3: 'R', 4: 'C', 5: 'M', 6: 'D', 7: '?',
    };
    return map[status] ?? '?';
}
async function applyEdit(file, range, text) {
    const uri = vscode.Uri.file(file);
    const doc = await vscode.workspace.openTextDocument(uri);
    const vsRange = new vscode.Range(new vscode.Position(range.start.line, range.start.char), new vscode.Position(range.end.line, range.end.char));
    const edit = new vscode.WorkspaceEdit();
    edit.replace(uri, vsRange, text);
    const applied = await vscode.workspace.applyEdit(edit);
    if (!applied) {
        throw new Error(`applyEdit rejected for ${file}`);
    }
    await doc.save();
    _log.appendLine(`[Bridge] Applied edit to ${file} lines ${range.start.line}–${range.end.line}`);
}
const _MAX_OUTPUT = 64000; // cap returned output so a noisy build can't flood the wire
const _SHELL_INTEGRATION_WAIT = 3000; // ms to wait for shell integration before falling back
const _CAPTURE_TIMEOUT = 8000; // ms max to wait for command completion
function stripAnsi(s) {
    // eslint-disable-next-line no-control-regex
    return s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');
}
async function waitForShellIntegration(terminal) {
    if (terminal.shellIntegration) {
        return terminal.shellIntegration;
    }
    return new Promise((resolve) => {
        const timer = setTimeout(() => { sub.dispose(); resolve(undefined); }, _SHELL_INTEGRATION_WAIT);
        const sub = vscode.window.onDidChangeTerminalShellIntegration((e) => {
            if (e.terminal === terminal && e.shellIntegration) {
                clearTimeout(timer);
                sub.dispose();
                resolve(e.shellIntegration);
            }
        });
    });
}
async function runInTerminal(command, cwd) {
    let terminal = vscode.window.activeTerminal;
    if (!terminal) {
        terminal = vscode.window.createTerminal({
            name: 'Desktop Agent',
            cwd: cwd ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
        });
    }
    terminal.show(true); // preserveFocus=true so editor stays active
    const si = await waitForShellIntegration(terminal);
    if (!si) {
        // No shell integration — best-effort fire-and-forget (no output capture).
        terminal.sendText(command);
        _log.appendLine(`[Bridge] Sent to terminal (no capture): ${command.substring(0, 80)}`);
        return { sent: true, captured: false };
    }
    const execution = si.executeCommand(command);
    let output = '';
    const endPromise = new Promise((resolve) => {
        const timer = setTimeout(() => { sub.dispose(); resolve(undefined); }, _CAPTURE_TIMEOUT);
        const sub = vscode.window.onDidEndTerminalShellExecution((e) => {
            if (e.execution === execution) {
                clearTimeout(timer);
                sub.dispose();
                resolve(e.exitCode);
            }
        });
    });
    try {
        for await (const chunk of execution.read()) {
            output += chunk;
            if (output.length > _MAX_OUTPUT * 2) {
                break; // hard stop on runaway output; trimmed below
            }
        }
    }
    catch { /* stream ended/closed */ }
    const exitCode = await endPromise;
    const clean = stripAnsi(output).slice(0, _MAX_OUTPUT);
    _log.appendLine(`[Bridge] Ran in terminal (exit=${exitCode ?? '?'}): ${command.substring(0, 80)}`);
    return { sent: true, captured: true, output: clean, exit_code: exitCode };
}
// ---------------------------------------------------------------------------
// Open file
// ---------------------------------------------------------------------------
async function openFile(filePath, line) {
    const uri = vscode.Uri.file(filePath);
    const doc = await vscode.workspace.openTextDocument(uri);
    const editor = await vscode.window.showTextDocument(doc);
    const pos = new vscode.Position(Math.max(0, line), 0);
    editor.selection = new vscode.Selection(pos, pos);
    editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
}
// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------
function getAllDiagnostics() {
    const result = [];
    for (const [uri, diags] of vscode.languages.getDiagnostics()) {
        for (const d of diags) {
            if (d.severity <= vscode.DiagnosticSeverity.Warning) {
                result.push({
                    file: uri.fsPath,
                    severity: vscode.DiagnosticSeverity[d.severity].toLowerCase(),
                    message: d.message,
                    source: d.source,
                    range: {
                        start: { line: d.range.start.line, char: d.range.start.character },
                        end: { line: d.range.end.line, char: d.range.end.character },
                    },
                });
            }
        }
    }
    return result;
}
//# sourceMappingURL=extension.js.map