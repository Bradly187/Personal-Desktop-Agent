"use strict";
// Desktop Agent Shell — Electron main process.
// Spec: specs/desktop-app-shell/requirements.md

const path = require("path");
const fs = require("fs");
const { app, BrowserWindow, Menu, shell, nativeTheme } = require("electron");

const auth = require("./main/auth");
const backend = require("./main/backend");
const ptyIpc = require("./main/pty");
const fsIpc = require("./main/fs-ipc");
const gitIpc = require("./main/git-ipc");
const notify = require("./main/notify");

let win = null;

function sendToRenderer(channel, payload) {
  if (channel === "backend:state-changed") notify.onBackendState(payload);
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: "#1e1e1e",
    title: "Desktop Agent",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false, // Monaco loads its workers from file:// — see renderer/editor.js
    },
  });

  win.loadFile(path.join(__dirname, "renderer", "index.html"));

  // The chat UI has no external links today, but never let one take over the
  // shell window or spawn an unmanaged child window.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });

  win.on("closed", () => {
    ptyIpc.killAll();
    win = null;
  });
}

// Shortcuts must work while an iframe (chat/dashboard) has keyboard focus, and
// renderer keydown handlers never see those keys — so tab/global shortcuts are
// registered as Menu accelerators and forwarded to the renderer.
function buildMenu() {
  const fwd = (action, arg) => () => sendToRenderer("shortcut", { action, arg });
  const tabItems = [];
  for (let i = 1; i <= 9; i++) {
    tabItems.push({ label: `Tab ${i}`, accelerator: `CmdOrCtrl+${i}`, click: fwd("tab", i) });
  }
  const template = [
    {
      label: "File",
      submenu: [
        { label: "Save", accelerator: "CmdOrCtrl+S", click: fwd("save") },
        { label: "Diff vs Git HEAD", accelerator: "CmdOrCtrl+Shift+D", click: fwd("diffActive") },
        { label: "Close Tab", accelerator: "CmdOrCtrl+W", click: fwd("closeTab") },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "View",
      submenu: [
        { label: "Go to File…", accelerator: "CmdOrCtrl+P", click: fwd("paletteFiles") },
        { label: "Command Palette…", accelerator: "CmdOrCtrl+Shift+P", click: fwd("paletteCommands") },
        { type: "separator" },
        ...tabItems,
        { type: "separator" },
        { label: "Next Tab", accelerator: "CmdOrCtrl+PageDown", click: fwd("tabNext") },
        { label: "Previous Tab", accelerator: "CmdOrCtrl+PageUp", click: fwd("tabPrev") },
        { type: "separator" },
        { label: "Toggle File Tree", accelerator: "CmdOrCtrl+B", click: fwd("toggleTree") },
        { label: "Focus Terminal", accelerator: "CmdOrCtrl+`", click: fwd("focusTerminal") },
        { type: "separator" },
        { role: "reload" },
        { role: "toggleDevTools" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
      ],
    },
    {
      label: "TTS",
      submenu: (() => {
        let cfg = {};
        try {
          cfg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "approval_config.json"), "utf8"));
        } catch(e) {}
        const backend = cfg.tts_backend || "kokoro";
        const voice = cfg.kokoro_voice || "af_bella";
        const speed = cfg.kokoro_speed || 1.0;
        
        const updateTtsConfig = (key, value) => {
          try {
            const cfgPath = path.join(__dirname, "..", "approval_config.json");
            let c = {};
            if (fs.existsSync(cfgPath)) {
              c = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
            }
            c[key] = value;
            fs.writeFileSync(cfgPath, JSON.stringify(c, null, 2), "utf8");
            fwd("reloadTts")();
          } catch (e) {
            console.error("Failed to update TTS config:", e);
          }
        };

        return [
          { label: "Backend", submenu: [
            { label: "Kokoro (AI)", type: "radio", checked: backend === "kokoro", click: () => updateTtsConfig("tts_backend", "kokoro") },
            { label: "AWS Polly", type: "radio", checked: backend === "polly", click: () => updateTtsConfig("tts_backend", "polly") },
            { label: "Windows SAPI", type: "radio", checked: backend === "sapi", click: () => updateTtsConfig("tts_backend", "sapi") }
          ]},
          { label: "Kokoro Voice", submenu: [
            { label: "af_bella", type: "radio", checked: voice === "af_bella", click: () => updateTtsConfig("kokoro_voice", "af_bella") },
            { label: "af_sarah", type: "radio", checked: voice === "af_sarah", click: () => updateTtsConfig("kokoro_voice", "af_sarah") },
            { label: "am_adam", type: "radio", checked: voice === "am_adam", click: () => updateTtsConfig("kokoro_voice", "am_adam") },
            { label: "am_michael", type: "radio", checked: voice === "am_michael", click: () => updateTtsConfig("kokoro_voice", "am_michael") }
          ]},
          { label: "Kokoro Speed", submenu: [
            { label: "0.8x", type: "radio", checked: speed === 0.8, click: () => updateTtsConfig("kokoro_speed", 0.8) },
            { label: "1.0x (Normal)", type: "radio", checked: speed === 1.0, click: () => updateTtsConfig("kokoro_speed", 1.0) },
            { label: "1.2x", type: "radio", checked: speed === 1.2, click: () => updateTtsConfig("kokoro_speed", 1.2) }
          ]}
        ];
      })()
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(() => {
  nativeTheme.themeSource = "dark";
  auth.installAuthInjection();
  fsIpc.register();
  gitIpc.register();
  ptyIpc.register();
  backend.register(sendToRenderer);
  buildMenu();
  createWindow();
  notify.init(() => win);
  backend.ensureBackend();
  if (process.env.SHELL_SMOKE) require("./main/smoke").run(win);
});

// Owned backend must die with the app; an attached one must survive it.
// Synchronous kill so quit waits for the process tree to go down.
app.on("before-quit", () => {
  ptyIpc.killAll();
  backend.stopOwnedBackendSync();
});

app.on("window-all-closed", () => app.quit());
