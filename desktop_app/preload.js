"use strict";
// The renderer's only privileged surface. Everything else runs with
// contextIsolation on and no Node access.

const { contextBridge, ipcRenderer } = require("electron");

function subscribe(channel) {
  return (handler) => {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  };
}

contextBridge.exposeInMainWorld("agent", {
  fs: {
    listDrives: () => ipcRenderer.invoke("fs:listDrives"),
    listDir: (path) => ipcRenderer.invoke("fs:listDir", path),
    readFile: (path, opts) => ipcRenderer.invoke("fs:readFile", path, opts),
    readFileBase64: (path) => ipcRenderer.invoke("fs:readFileBase64", path),
    writeFile: (args) => ipcRenderer.invoke("fs:writeFile", args),
    stat: (path) => ipcRenderer.invoke("fs:stat", path),
    showContextMenu: (path) => ipcRenderer.send("fs:showContextMenu", path),
  },
  pty: {
    spawn: (opts) => ipcRenderer.invoke("pty:spawn", opts),
    write: (ptyId, data) => ipcRenderer.send("pty:write", { ptyId, data }),
    resize: (ptyId, cols, rows) => ipcRenderer.send("pty:resize", { ptyId, cols, rows }),
    kill: (ptyId) => ipcRenderer.send("pty:kill", { ptyId }),
    onData: subscribe("pty:data"),
    onExit: subscribe("pty:exit"),
  },
  backend: {
    status: () => ipcRenderer.invoke("backend:status"),
    start: () => ipcRenderer.invoke("backend:start"),
    stop: () => ipcRenderer.invoke("backend:stop"),
    onLog: subscribe("backend:log"),
    onStateChanged: subscribe("backend:state-changed"),
  },
  app: {
    pickFolder: () => ipcRenderer.invoke("app:pickFolder"),
    onShortcut: subscribe("shortcut"),
  },
});
