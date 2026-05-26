/**
 * Desktop Agent Bridge — VS Code / Kiro extension
 *
 * Exposes a WebSocket server on localhost:8767 so the Personal Desktop Agent
 * Python backend can read IDE state and send edit commands without screen-scraping.
 *
 * Supported actions (JSON request → JSON response):
 *   get_editor_context  → file, language, cursor, selection, context_above/below, diagnostics
 *   get_git_context     → branch, ahead, behind, staged, unstaged, last_commit
 *   apply_edit          → replace a range in a file; saves; triggers LSP re-check
 *   run_terminal        → send a command to the active integrated terminal
 *   open_file           → open a file and jump to a line
 *   get_diagnostics     → all workspace diagnostics (errors/warnings)
 *   ping                → { pong: true }
 *
 * Install: npm install && npm run compile → copy to ~/.vscode/extensions/ or use 'code --install-extension'
 */

import * as vscode from 'vscode';
import * as WebSocket from 'ws';
import * as http from 'http';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface BridgeRequest {
  id?: string | number;
  action: string;
  [key: string]: unknown;
}

interface BridgeResponse {
  id?: string | number;
  ok: boolean;
  data?: unknown;
  error?: string;
}

// ---------------------------------------------------------------------------
// Extension state
// ---------------------------------------------------------------------------

let _server: WebSocket.Server | null = null;
let _statusBar: vscode.StatusBarItem;
let _clientCount = 0;
let _log: vscode.OutputChannel;

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
  _log = vscode.window.createOutputChannel('Desktop Agent Bridge');
  _log.appendLine('[Desktop Agent Bridge] Activating...');

  _statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  _statusBar.command = 'desktopAgent.showStatus';
  context.subscriptions.push(_statusBar);

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand('desktopAgent.restartBridge', () => {
      stopServer();
      startServer(context);
    }),
    vscode.commands.registerCommand('desktopAgent.showStatus', () => {
      const port = getPort();
      vscode.window.showInformationMessage(
        `Desktop Agent Bridge: ${_clientCount} client(s) connected on port ${port}`
      );
    })
  );

  startServer(context);
}

export function deactivate(): void {
  stopServer();
  _log.dispose();
}

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------

function getPort(): number {
  return vscode.workspace.getConfiguration('desktopAgent').get<number>('port', 8767);
}

function startServer(context: vscode.ExtensionContext): void {
  const port = getPort();

  try {
    _server = new WebSocket.Server({ host: '127.0.0.1', port });

    _server.on('listening', () => {
      _log.appendLine(`[Bridge] WebSocket server listening on ws://127.0.0.1:${port}`);
      updateStatusBar();
    });

    _server.on('connection', (ws: WebSocket.WebSocket) => {
      _clientCount++;
      _log.appendLine(`[Bridge] Client connected (total: ${_clientCount})`);
      updateStatusBar();

      ws.on('message', async (raw: WebSocket.Data) => {
        let req: BridgeRequest;
        try {
          req = JSON.parse(raw.toString()) as BridgeRequest;
        } catch {
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

      ws.on('error', (err: Error) => {
        _log.appendLine(`[Bridge] Client error: ${err.message}`);
      });
    });

    _server.on('error', (err: Error) => {
      _log.appendLine(`[Bridge] Server error: ${err.message}`);
      vscode.window.showErrorMessage(`Desktop Agent Bridge error: ${err.message}`);
      updateStatusBar(true);
    });

  } catch (err) {
    _log.appendLine(`[Bridge] Failed to start: ${err}`);
    updateStatusBar(true);
  }
}

function stopServer(): void {
  if (_server) {
    _server.close();
    _server = null;
    _clientCount = 0;
    _log.appendLine('[Bridge] Server stopped');
    updateStatusBar();
  }
}

function updateStatusBar(error = false): void {
  if (error) {
    _statusBar.text = '$(error) Agent Bridge: ERROR';
    _statusBar.tooltip = 'Desktop Agent Bridge failed to start. Check Output > Desktop Agent Bridge.';
    _statusBar.color = new vscode.ThemeColor('errorForeground');
  } else if (_server) {
    const icon = _clientCount > 0 ? '$(plug)' : '$(circle-outline)';
    _statusBar.text = `${icon} Agent: ${_clientCount}`;
    _statusBar.tooltip = `Desktop Agent Bridge — ${_clientCount} client(s) on port ${getPort()}`;
    _statusBar.color = undefined;
  } else {
    _statusBar.text = '$(circle-slash) Agent Bridge: OFF';
    _statusBar.tooltip = 'Desktop Agent Bridge is stopped.';
    _statusBar.color = undefined;
  }
  _statusBar.show();
}

// ---------------------------------------------------------------------------
// Request dispatcher
// ---------------------------------------------------------------------------

async function handleRequest(req: BridgeRequest): Promise<BridgeResponse> {
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
        await applyEdit(
          req.file as string,
          req.range as EditRange,
          req.text as string
        );
        return { id, ok: true, data: { applied: true } };

      case 'run_terminal': {
        const cmd = req.command as string;
        const cwd = req.cwd as string | undefined;
        runInTerminal(cmd, cwd);
        return { id, ok: true, data: { sent: true } };
      }

      case 'open_file': {
        const filePath = req.file as string;
        const line = (req.line as number) ?? 0;
        await openFile(filePath, line);
        return { id, ok: true, data: { opened: true } };
      }

      case 'get_diagnostics':
        return { id, ok: true, data: getAllDiagnostics() };

      default:
        return { id, ok: false, error: `Unknown action: ${req.action}` };
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    _log.appendLine(`[Bridge] Error handling ${req.action}: ${msg}`);
    return { id, ok: false, error: msg };
  }
}

// ---------------------------------------------------------------------------
// Editor context
// ---------------------------------------------------------------------------

async function getEditorContext(): Promise<unknown> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    return null;
  }

  const doc = editor.document;
  const sel = editor.selection;
  const contextLines = vscode.workspace
    .getConfiguration('desktopAgent')
    .get<number>('contextLines', 50);

  const aboveStart = new vscode.Position(Math.max(0, sel.active.line - contextLines), 0);
  const aboveEnd = new vscode.Position(sel.active.line, 0);
  const belowStart = new vscode.Position(sel.active.line + 1, 0);
  const belowEnd = new vscode.Position(
    Math.min(doc.lineCount - 1, sel.active.line + contextLines + 1),
    0
  );

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

async function getGitContext(): Promise<unknown> {
  try {
    // Access VS Code's built-in git extension
    const gitExtension = vscode.extensions.getExtension<GitExtension>('vscode.git');
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
  } catch (err) {
    return { error: String(err) };
  }
}

function statusToString(status: number): string {
  // VS Code Status enum: INDEX_MODIFIED=0, INDEX_ADDED=1, INDEX_DELETED=2,
  // MODIFIED=5, UNTRACKED=7, DELETED=6, etc.
  const map: Record<number, string> = {
    0: 'M', 1: 'A', 2: 'D', 3: 'R', 4: 'C', 5: 'M', 6: 'D', 7: '?',
  };
  return map[status] ?? '?';
}

// Minimal type stubs for vscode.git
interface GitExtension {
  getAPI(version: 1): GitAPI;
}
interface GitAPI {
  repositories: GitRepository[];
}
interface GitRepository {
  state: {
    HEAD: { name?: string; commit?: string; ahead?: number; behind?: number } | undefined;
    indexChanges: GitChange[];
    workingTreeChanges: GitChange[];
    mergeChanges: GitChange[];
  };
}
interface GitChange {
  uri: vscode.Uri;
  status: number;
}

// ---------------------------------------------------------------------------
// Edit application
// ---------------------------------------------------------------------------

interface EditRange {
  start: { line: number; char: number };
  end: { line: number; char: number };
}

async function applyEdit(file: string, range: EditRange, text: string): Promise<void> {
  const uri = vscode.Uri.file(file);
  const doc = await vscode.workspace.openTextDocument(uri);

  const vsRange = new vscode.Range(
    new vscode.Position(range.start.line, range.start.char),
    new vscode.Position(range.end.line, range.end.char)
  );

  const edit = new vscode.WorkspaceEdit();
  edit.replace(uri, vsRange, text);
  const applied = await vscode.workspace.applyEdit(edit);
  if (!applied) {
    throw new Error(`applyEdit rejected for ${file}`);
  }
  await doc.save();
  _log.appendLine(`[Bridge] Applied edit to ${file} lines ${range.start.line}–${range.end.line}`);
}

// ---------------------------------------------------------------------------
// Terminal
// ---------------------------------------------------------------------------

function runInTerminal(command: string, cwd?: string): void {
  let terminal = vscode.window.activeTerminal;
  if (!terminal) {
    terminal = vscode.window.createTerminal({
      name: 'Desktop Agent',
      cwd: cwd ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
    });
  }
  terminal.show(true);    // preserveFocus=true so editor stays active
  terminal.sendText(command);
  _log.appendLine(`[Bridge] Sent to terminal: ${command.substring(0, 80)}`);
}

// ---------------------------------------------------------------------------
// Open file
// ---------------------------------------------------------------------------

async function openFile(filePath: string, line: number): Promise<void> {
  const uri = vscode.Uri.file(filePath);
  const doc = await vscode.workspace.openTextDocument(uri);
  const editor = await vscode.window.showTextDocument(doc);
  const pos = new vscode.Position(Math.max(0, line), 0);
  editor.selection = new vscode.Selection(pos, pos);
  editor.revealRange(
    new vscode.Range(pos, pos),
    vscode.TextEditorRevealType.InCenter
  );
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

function getAllDiagnostics(): unknown[] {
  const result: unknown[] = [];
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
