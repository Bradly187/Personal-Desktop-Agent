"use strict";
// OS toast notifications (SG-4, gap analysis 2026-07-03). The shell toasts only
// when the window isn't focused — when it is, the chat card / status pill / TTS
// already carry the signal. Voice approvals fail safe to DENY on timeout, so a
// buried window plus a muted PC used to mean silently denied actions; the
// approval toast closes that hole.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { Notification, ipcMain } = require("electron");

// Must match _APPROVAL_DIR in approval_hook.py / whisper_stream.py.
const APPROVAL_DIR = path.join(os.homedir(), ".claude", "approval");
const PENDING_FILE = path.join(APPROVAL_DIR, "pending");
const PROMPT_FILE = path.join(APPROVAL_DIR, "prompt");

let getWin = () => null;
let prevMode = null;
let approvalToasted = false;

function focusWin() {
  const w = getWin();
  if (!w || w.isDestroyed()) return;
  if (w.isMinimized()) w.restore();
  w.show();
  w.focus();
}

function windowNeedsToast() {
  const w = getWin();
  return !w || w.isDestroyed() || w.isMinimized() || !w.isFocused();
}

function toast(title, body) {
  if (!Notification.isSupported() || !windowNeedsToast()) return;
  const n = new Notification({ title, body: body || "" });
  n.on("click", focusWin);
  n.show();
}

// Approval requests appear as ~/.claude/approval/pending (prompt is written
// first, so it is readable the moment pending exists) and are deleted when
// answered or timed out.
function watchApprovals() {
  fs.mkdirSync(APPROVAL_DIR, { recursive: true }); // backend mkdirs this too
  fs.watch(APPROVAL_DIR, (_event, filename) => {
    if (filename !== "pending") return;
    const pending = fs.existsSync(PENDING_FILE);
    if (pending && !approvalToasted) {
      approvalToasted = true;
      let prompt = "";
      try { prompt = fs.readFileSync(PROMPT_FILE, "utf8").trim(); } catch { /* raced away */ }
      toast("Approval needed", prompt || "The agent is waiting for a yes/no. Silence denies.");
    } else if (!pending) {
      approvalToasted = false;
    }
  });
}

// Fed every backend:state-changed payload from main.js. Deliberate stops
// (Stop button, quit) bypass setMode and never arrive here, so a "down" seen
// after a live mode is always an unexpected death.
function onBackendState(status) {
  const mode = status.mode;
  if (mode === "down" && ["starting", "owned", "attached"].includes(prevMode)) {
    toast("Backend down", status.lastError || "The agent backend stopped.");
  }
  prevMode = mode;
}

function init(getWindow) {
  getWin = getWindow;
  // Renderer-raised toasts (run finished/failed — see renderer/sessions.js).
  ipcMain.on("notify:toast", (_e, { title, body }) => toast(String(title || ""), String(body || "")));
  try {
    watchApprovals();
  } catch (err) {
    // Notifications are best-effort; never take the shell down over a watcher.
    console.error("notify: approval watcher failed:", err.message);
  }
}

module.exports = { init, onBackendState, toast };
