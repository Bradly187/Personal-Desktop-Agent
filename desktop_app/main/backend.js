"use strict";
// Backend lifecycle: the shell owns `main.py --chat` when nothing else started
// it, and only attaches when something (watchdog, manual start) already did.
//
// Modes:  down -> starting -> owned      (we spawned it; we kill it on quit)
//         down -> attached               (already healthy; NEVER killed by us)
// An owned child that dies is reported as down — restart policy belongs to the
// watchdog, not the shell (spec R2.4).

const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { ipcMain } = require("electron");

const auth = require("./auth");

const REPO_ROOT = process.env.PDA_REPO_ROOT || path.resolve(__dirname, "..", "..");
const PYTHON = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
const BACKEND_URL = "http://127.0.0.1:8770";
const HEALTH_POLL_MAX_S = 120;
const LOG_TAIL_LINES = 500;

let child = null;
let mode = "down"; // down | starting | owned | attached
let lastError = null;
let sendToRenderer = () => {};
const logTail = [];

function pushLog(chunk) {
  const text = chunk.toString();
  for (const line of text.split(/\r?\n/)) {
    if (!line) continue;
    logTail.push(line);
    if (logTail.length > LOG_TAIL_LINES) logTail.shift();
  }
  sendToRenderer("backend:log", text);
}

function setMode(m) {
  mode = m;
  sendToRenderer("backend:state-changed", getStatus());
}

function getStatus() {
  return {
    mode,
    pid: child ? child.pid : null,
    tokenPresent: auth.tokenPresent(),
    repoRoot: REPO_ROOT,
    pythonFound: fs.existsSync(PYTHON),
    lastError,
    logTail: logTail.slice(-LOG_TAIL_LINES),
  };
}

async function health() {
  try {
    const r = await fetch(`${BACKEND_URL}/health`, { signal: AbortSignal.timeout(2000) });
    return r.ok;
  } catch {
    return false;
  }
}

async function ensureBackend() {
  if (mode === "starting" || mode === "owned") return;
  if (await health()) {
    setMode("attached");
    return;
  }
  if (!fs.existsSync(PYTHON)) {
    lastError = `python not found: ${PYTHON}`;
    setMode("down");
    return;
  }
  lastError = null;
  setMode("starting");

  const logDir = path.join(REPO_ROOT, "logs");
  fs.mkdirSync(logDir, { recursive: true });
  const logStream = fs.createWriteStream(path.join(logDir, "desktop_app_backend.log"), { flags: "a" });

  child = spawn(PYTHON, ["main.py", "--chat", "--chat-no-browser"], {
    cwd: REPO_ROOT,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (b) => { logStream.write(b); pushLog(b); });
  child.stderr.on("data", (b) => { logStream.write(b); pushLog(b); });
  child.on("exit", (code) => {
    logStream.end();
    child = null;
    if (mode === "owned" || mode === "starting") {
      lastError = `backend exited with code ${code}`;
      setMode("down");
    }
  });

  for (let i = 0; i < HEALTH_POLL_MAX_S; i++) {
    if (await health()) {
      auth.loadToken(); // first-ever run creates the token file during startup
      setMode("owned");
      return;
    }
    if (!child) return; // crashed while starting; exit handler already reported
    await new Promise((r) => setTimeout(r, 1000));
  }
  lastError = `backend did not become healthy within ${HEALTH_POLL_MAX_S}s`;
  setMode("down");
}

// Synchronous on purpose: called from app "before-quit" so quit waits for the
// tree to die. taskkill /T is required — the venv python.exe is a launcher
// whose *child* interpreter holds ports 8765/8770; child.kill() would orphan
// it. /F (no graceful signal) is acceptable: agent.db is WAL-journaled.
function stopOwnedBackendSync() {
  if (mode !== "owned" || !child) return;
  const pid = child.pid;
  child.removeAllListeners("exit");
  child = null;
  try {
    execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
  } catch {
    // already gone
  }
  mode = "down";
}

function register(send) {
  sendToRenderer = send;
  ipcMain.handle("backend:status", async () => ({ ...getStatus(), healthy: await health() }));
  ipcMain.handle("backend:start", async () => {
    if (mode === "down") await ensureBackend();
    return getStatus();
  });
  ipcMain.handle("backend:stop", () => {
    stopOwnedBackendSync();
    return getStatus();
  });
}

module.exports = { register, ensureBackend, stopOwnedBackendSync };
