"use strict";
// File-system IPC for the tree and editor. Whole-filesystem access is the
// feature (Brad's scope decision in the spec) — no root allowlist, but paths
// are normalized and reads are guarded against huge/binary files so Monaco
// never freezes.

const fs = require("fs");
const fsp = require("fs/promises");
const path = require("path");
const { ipcMain, dialog } = require("electron");

const FILE_OPEN_WARN_BYTES = 5 * 1024 * 1024; // 5 MB
const BINARY_SNIFF_BYTES = 8192;

function norm(p) {
  return path.resolve(String(p));
}

async function listDrives() {
  const drives = [];
  for (let c = 65; c <= 90; c++) {
    const root = `${String.fromCharCode(c)}:\\`;
    if (fs.existsSync(root)) drives.push(root);
  }
  return drives;
}

async function listDir(dirPath) {
  const dir = norm(dirPath);
  let entries;
  try {
    entries = await fsp.readdir(dir, { withFileTypes: true });
  } catch (err) {
    return { error: err.code || String(err.message) };
  }
  const items = entries.map((e) => ({
    name: e.name,
    path: path.join(dir, e.name),
    // Symlinks/junctions are treated as leaves — following them can loop.
    isDir: e.isDirectory() && !e.isSymbolicLink(),
  }));
  items.sort((a, b) =>
    a.isDir !== b.isDir ? (a.isDir ? -1 : 1) : a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  return { items };
}

async function readFile(filePath, { force = false } = {}) {
  const file = norm(filePath);
  let st;
  try {
    st = await fsp.stat(file);
  } catch (err) {
    return { error: err.code || String(err.message) };
  }
  if (!st.isFile()) return { error: "not a file" };
  if (st.size > FILE_OPEN_WARN_BYTES && !force) return { tooLarge: st.size };

  let fh;
  try {
    fh = await fsp.open(file, "r");
    const sniffLen = Math.min(BINARY_SNIFF_BYTES, st.size);
    const sniff = Buffer.alloc(sniffLen);
    await fh.read(sniff, 0, sniffLen, 0);
    if (sniff.includes(0)) return { binary: true };
    const content = await fh.readFile({ encoding: "utf8" });
    return { content, mtimeMs: st.mtimeMs, size: st.size };
  } catch (err) {
    return { error: err.code || String(err.message) };
  } finally {
    if (fh) await fh.close();
  }
}

async function writeFile({ path: filePath, content, expectedMtimeMs = null, force = false }) {
  const file = norm(filePath);
  if (expectedMtimeMs !== null && !force) {
    try {
      const st = await fsp.stat(file);
      if (Math.abs(st.mtimeMs - expectedMtimeMs) > 0.001) {
        return { conflict: true, mtimeMs: st.mtimeMs };
      }
    } catch {
      // File deleted since open — treat as writable (recreate).
    }
  }
  try {
    await fsp.writeFile(file, content, "utf8");
    const st = await fsp.stat(file);
    return { ok: true, mtimeMs: st.mtimeMs };
  } catch (err) {
    return { error: err.code || String(err.message) };
  }
}

async function stat(filePath) {
  try {
    const st = await fsp.stat(norm(filePath));
    return { size: st.size, mtimeMs: st.mtimeMs, isDir: st.isDirectory() };
  } catch (err) {
    return { error: err.code || String(err.message) };
  }
}

async function readFileBase64(filePath) {
  const file = norm(filePath);
  try {
    const st = await fsp.stat(file);
    if (!st.isFile()) return { error: "not a file" };
    // Increased size limit slightly for images up to 25MB
    if (st.size > FILE_OPEN_WARN_BYTES * 5) return { error: "file too large for base64" };
    
    const buffer = await fsp.readFile(file);
    let mimeType = "application/octet-stream";
    const ext = path.extname(file).toLowerCase();
    if (ext === ".png") mimeType = "image/png";
    else if (ext === ".jpg" || ext === ".jpeg") mimeType = "image/jpeg";
    else if (ext === ".gif") mimeType = "image/gif";
    else if (ext === ".svg") mimeType = "image/svg+xml";
    else if (ext === ".webp") mimeType = "image/webp";
    
    return { base64: buffer.toString("base64"), mimeType };
  } catch (err) {
    return { error: err.code || String(err.message) };
  }
}

// Flat file index for the fuzzy opener (Ctrl+P). BFS with an ignore set so
// node_modules/.venv can't swamp it; capped, and the cap is reported so the
// palette can say "index truncated" instead of silently missing files.
const INDEX_IGNORE = new Set([
  "node_modules", ".git", ".venv", "venv", "__pycache__", ".mypy_cache",
  ".pytest_cache", "dist", "build", "out", "logs", "chroma_db", ".idea", ".vscode",
]);
const INDEX_MAX_FILES = 20000;

async function listFilesRec(rootPath) {
  const root = norm(rootPath);
  const files = [];
  const queue = [root];
  let truncated = false;
  while (queue.length > 0 && !truncated) {
    const dir = queue.shift();
    let entries;
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      continue; // unreadable subtree — skip, never fail the index
    }
    for (const e of entries) {
      if (e.isSymbolicLink()) continue;
      const full = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (!INDEX_IGNORE.has(e.name)) queue.push(full);
      } else if (e.isFile()) {
        files.push(full);
        if (files.length >= INDEX_MAX_FILES) { truncated = true; break; }
      }
    }
  }
  return { root, files, truncated };
}

function register() {
  ipcMain.handle("fs:listDrives", () => listDrives());
  ipcMain.handle("fs:listFilesRec", (_e, p) => listFilesRec(p));
  ipcMain.handle("fs:listDir", (_e, p) => listDir(p));
  ipcMain.handle("fs:readFile", (_e, p, opts) => readFile(p, opts));
  ipcMain.handle("fs:readFileBase64", (_e, p) => readFileBase64(p));
  ipcMain.handle("fs:writeFile", (_e, args) => writeFile(args));
  ipcMain.handle("fs:stat", (_e, p) => stat(p));
  ipcMain.handle("app:pickFolder", async () => {
    const r = await dialog.showOpenDialog({ properties: ["openDirectory"] });
    return r.canceled ? null : r.filePaths[0];
  });
}

module.exports = { register };
