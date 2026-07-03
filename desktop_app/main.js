"use strict";
// Desktop Agent Shell — Electron main process.
// Spec: specs/desktop-app-shell/requirements.md

const path = require("path");
const { app, BrowserWindow, Menu, shell, nativeTheme } = require("electron");

const auth = require("./main/auth");
const backend = require("./main/backend");
const ptyIpc = require("./main/pty");
const fsIpc = require("./main/fs-ipc");

let win = null;

function sendToRenderer(channel, payload) {
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
        { label: "Close Tab", accelerator: "CmdOrCtrl+W", click: fwd("closeTab") },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "View",
      submenu: [
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
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(() => {
  nativeTheme.themeSource = "dark";
  auth.installAuthInjection();
  fsIpc.register();
  ptyIpc.register();
  backend.register(sendToRenderer);
  buildMenu();
  createWindow();
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
