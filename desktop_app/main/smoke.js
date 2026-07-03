"use strict";
// Self-test harness: SHELL_SMOKE=1 npx electron .
// Loads the shell, waits for the panels to come up, then prints a JSON
// diagnostic line (SMOKE_RESULT ...), saves a screenshot to SHELL_SMOKE_OUT,
// and quits. Used for automated verification — harmless to ship.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { app, session } = require("electron");

const SETTLE_MS = parseInt(process.env.SHELL_SMOKE_SETTLE_MS, 10) || 25000;

async function run(win) {
  const consoleErrors = [];
  const responses = [];

  win.webContents.on("console-message", (_e, level, message) => {
    if (level >= 3) consoleErrors.push(message);
  });
  session.defaultSession.webRequest.onCompleted(
    { urls: ["http://127.0.0.1:8770/*", "ws://127.0.0.1:8770/*"] },
    (d) => responses.push({ url: d.url, status: d.statusCode }),
  );

  await new Promise((r) => win.webContents.once("did-finish-load", r));
  await new Promise((r) => setTimeout(r, SETTLE_MS));

  const tmpFile = path.join(os.tmpdir(), "desktop-shell-smoke.txt");
  let evalResult = null;
  let evalError = null;
  try {
    evalResult = await win.webContents.executeJavaScript(`(async () => {
      const out = {};
      out.chatSrc = document.getElementById("chat-frame").src;
      out.dashSrc = document.getElementById("dashboard-frame").src;
      out.chatPlaceholderHidden = document.getElementById("chat-placeholder").hidden;
      out.dashPlaceholderHidden = document.getElementById("dashboard-placeholder").hidden;
      out.monacoLoaded = typeof monaco !== "undefined";
      out.xtermLoaded = typeof Terminal !== "undefined";
      out.treeRows = document.querySelectorAll("#tree .tree-row").length;
      out.termText = Term.text().slice(-400);
      out.drives = await agent.fs.listDrives();
      await agent.fs.writeFile({ path: ${JSON.stringify(tmpFile)}, content: "hello smoke" });
      const rf = await agent.fs.readFile(${JSON.stringify(tmpFile)}, {});
      out.fsRoundTrip = rf.content === "hello smoke";
      await App.openFile(${JSON.stringify(tmpFile)});
      out.tabCount = document.querySelectorAll("#tabbar .tab").length;
      out.backend = await agent.backend.status();

      // v1.1 surfaces (gap analysis 2026-07-03) ------------------------------
      // Diff (R6): a tracked file diffs ok end-to-end (git IPC + Monaco models).
      const diffTry = await Editor.openDiff(${JSON.stringify(path.join(__dirname, "..", "package.json"))});
      out.diffOk = !!diffTry.ok;
      // Palette (R8): opens, lists commands, closes.
      await Palette.open("commands");
      out.paletteRows = document.querySelectorAll("#palette-overlay .palette-row").length;
      out.paletteVisible = !document.getElementById("palette-overlay").hidden;
      Palette.close();
      // Runs panel (R9): rendered something (rows, "No runs yet", or offline note).
      out.runsPanelPopulated = document.getElementById("runs-list").children.length > 0;
      // Toast IPC (R7): reachable without throwing (suppressed while focused).
      agent.notify.toast("smoke", "toast IPC reachable");
      out.toastIpcOk = true;

      localStorage.clear(); // don't leak the smoke tab into real sessions
      return out;
    })()`);
  } catch (err) {
    evalError = String(err && err.message ? err.message : err);
  }

  const outDir = process.env.SHELL_SMOKE_OUT || os.tmpdir();
  let screenshot = null;
  try {
    win.focus(); // force a fresh composite — background windows capture stale frames
    await new Promise((r) => setTimeout(r, 1500));
    const img = await win.webContents.capturePage();
    screenshot = path.join(outDir, "shell-smoke.png");
    fs.writeFileSync(screenshot, img.toPNG());
  } catch { /* diagnostics only */ }

  const statusTally = {};
  for (const r of responses) statusTally[r.status] = (statusTally[r.status] || 0) + 1;
  const non200 = responses.filter((r) => r.status >= 400);

  console.log("SMOKE_RESULT " + JSON.stringify({
    evalResult, evalError, consoleErrors, statusTally, non200, screenshot,
  }, null, 2));
  app.quit();
}

module.exports = { run };
