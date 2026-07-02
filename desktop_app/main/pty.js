"use strict";
// node-pty session registry. PowerShell over ConPTY (Win11 native).
// node-pty is a native module rebuilt against Electron's ABI by
// scripts/rebuild-pty.js — if that failed, spawn returns {error} and the
// terminal panel shows it instead of the shell crashing at require time.

const path = require("path");
const { ipcMain } = require("electron");

const REPO_ROOT = process.env.PDA_REPO_ROOT || path.resolve(__dirname, "..", "..");

let pty = null;
let ptyLoadError = null;
try {
  pty = require("node-pty");
} catch (err) {
  ptyLoadError = String(err && err.message ? err.message : err);
}

const sessions = new Map();
let nextId = 1;

function register() {
  ipcMain.handle("pty:spawn", (event, opts) => {
    if (!pty) return { error: `node-pty unavailable: ${ptyLoadError}` };
    const { cols, rows, cwd } = opts || {};
    let proc;
    try {
      proc = pty.spawn("powershell.exe", ["-NoLogo"], {
        name: "xterm-256color",
        cols: cols || 120,
        rows: rows || 30,
        cwd: cwd || REPO_ROOT,
        env: process.env,
      });
    } catch (err) {
      return { error: String(err && err.message ? err.message : err) };
    }
    const id = nextId++;
    const target = event.sender;
    proc.onData((data) => {
      if (!target.isDestroyed()) target.send("pty:data", { ptyId: id, data });
    });
    proc.onExit(({ exitCode }) => {
      sessions.delete(id);
      if (!target.isDestroyed()) target.send("pty:exit", { ptyId: id, exitCode });
    });
    sessions.set(id, proc);
    return { ptyId: id };
  });

  ipcMain.on("pty:write", (_e, { ptyId, data }) => {
    const p = sessions.get(ptyId);
    if (p) p.write(data);
  });

  ipcMain.on("pty:resize", (_e, { ptyId, cols, rows }) => {
    const p = sessions.get(ptyId);
    if (p && cols > 0 && rows > 0) p.resize(cols, rows);
  });

  ipcMain.on("pty:kill", (_e, { ptyId }) => {
    const p = sessions.get(ptyId);
    if (p) {
      sessions.delete(ptyId);
      p.kill();
    }
  });
}

function killAll() {
  for (const [, p] of sessions) {
    try { p.kill(); } catch { /* already dead */ }
  }
  sessions.clear();
}

module.exports = { register, killAll };
