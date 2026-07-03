"use strict";
// Git IPC for the diff view: fetch a file's content at HEAD so the renderer
// can show Monaco's side-by-side diff (working copy vs last commit). Read-only
// — no staging/commit surface here (that stays with the agent's GIT_* verbs).

const path = require("path");
const { execFile } = require("child_process");
const { ipcMain } = require("electron");

const GIT_MAX_BUFFER = 32 * 1024 * 1024; // git show of a large file

function git(args, cwd) {
  return new Promise((resolve) => {
    execFile("git", args, { cwd, maxBuffer: GIT_MAX_BUFFER, windowsHide: true }, (err, stdout, stderr) => {
      resolve({ err, stdout, stderr: String(stderr || "") });
    });
  });
}

// HEAD content of the file, or a shaped error the renderer can message on:
//   {content}            — file exists at HEAD
//   {untracked: true}    — in a repo but not in HEAD (new file → all-added diff)
//   {notRepo: true}      — not inside a git work tree
//   {error}              — git missing, permission, etc.
async function headContent(filePath) {
  const file = path.resolve(String(filePath));
  const dir = path.dirname(file);

  const top = await git(["rev-parse", "--show-toplevel"], dir);
  if (top.err) {
    if (/not a git repository/i.test(top.stderr)) return { notRepo: true };
    if (top.err.code === "ENOENT") return { error: "git not found on PATH" };
    return { error: top.stderr.trim() || String(top.err.message) };
  }

  const root = top.stdout.trim();
  // git show wants forward slashes regardless of platform.
  const rel = path.relative(root, file).split(path.sep).join("/");
  const show = await git(["show", `HEAD:${rel}`], root);
  if (show.err) {
    if (/exists on disk, but not in|does not exist in/i.test(show.stderr)) return { untracked: true };
    return { error: show.stderr.trim() || String(show.err.message) };
  }
  return { content: show.stdout };
}

function register() {
  ipcMain.handle("git:headContent", (_e, p) => headContent(p));
}

module.exports = { register };
