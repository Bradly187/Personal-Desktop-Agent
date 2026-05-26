# Desktop Agent Bridge — Kiro/VS Code Extension

Exposes a WebSocket server on `ws://127.0.0.1:8767` so the Personal Desktop Agent
Python backend can read IDE state and send edit commands without screen-scraping.

## What it provides

| Action | Description |
|--------|-------------|
| `get_editor_context` | Active file, language, cursor, selection, 50 lines context above/below, LSP diagnostics |
| `get_git_context` | Branch, ahead/behind, staged/unstaged files via VS Code git extension |
| `apply_edit` | Replace a range in any file — appears in undo history, triggers LSP re-validation |
| `run_terminal` | Send a command to the integrated terminal |
| `open_file` | Open file and jump to line |
| `get_diagnostics` | All workspace errors/warnings from language servers |
| `ping` | Health check |

## Installation

### Option A — Development install (recommended)

```bash
cd kiro-extension
npm install
npm run compile

# Install into Kiro/VS Code
code --install-extension .
# or: copy kiro-extension/ to ~/.vscode/extensions/desktop-agent-bridge-0.1.0/
```

### Option B — VSIX package

```bash
cd kiro-extension
npm install -g @vscode/vsce
npm install
npm run compile
vsce package
# Installs: code --install-extension desktop-agent-bridge-0.1.0.vsix
```

## Usage

The extension activates automatically when VS Code/Kiro starts. Look for the
status bar item in the bottom-right corner: `⊙ Agent: 0`.

Start the Python backend with `--kiro` to connect:

```bash
python main.py --kiro [other flags...]
```

The status bar updates to `🔌 Agent: 1` when the Python backend connects.

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `desktopAgent.port` | `8767` | WebSocket port |
| `desktopAgent.contextLines` | `50` | Lines of surrounding context to send |

## Protocol

All messages are JSON. Request: `{ "id": "optional", "action": "...", ...params }`.
Response: `{ "id": "optional", "ok": true/false, "data": ..., "error": "..." }`.

## Commands

- `Desktop Agent: Restart Bridge Server` — restart the WebSocket server (e.g. after port conflict)
- `Desktop Agent: Show Bridge Status` — show connection count in a notification
