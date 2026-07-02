"use strict";
// Transparent auth for the embedded chat/dashboard iframes.
//
// The ChatServer token cookie is SameSite=Strict, so it is never sent from a
// cross-origin iframe — the chat WebSocket and dashboard fetch()es would 401.
// Instead the main process injects X-Agent-Token on every request to the
// backend (the ws:// pattern covers the WebSocket handshake). The token never
// reaches the renderer or the embedded pages.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { session } = require("electron");

const TOKEN_DIR = path.join(os.homedir(), ".claude", "chat_server");
const TOKEN_PATH = path.join(TOKEN_DIR, "token");
const BACKEND_FILTER = { urls: ["http://127.0.0.1:8770/*", "ws://127.0.0.1:8770/*"] };

let token = null;

function loadToken() {
  try {
    token = fs.readFileSync(TOKEN_PATH, "utf8").trim() || null;
  } catch {
    token = null;
  }
  return token;
}

function tokenPresent() {
  if (token === null) loadToken(); // dir may appear after backend's first run
  return token !== null;
}

function installAuthInjection() {
  loadToken();
  try {
    fs.watch(TOKEN_DIR, { persistent: false }, () => loadToken());
  } catch {
    // Token dir not created yet — tokenPresent() re-reads on demand.
  }
  session.defaultSession.webRequest.onBeforeSendHeaders(BACKEND_FILTER, (details, callback) => {
    if (token) details.requestHeaders["X-Agent-Token"] = token;
    callback({ requestHeaders: details.requestHeaders });
  });
}

module.exports = { installAuthInjection, loadToken, tokenPresent };
