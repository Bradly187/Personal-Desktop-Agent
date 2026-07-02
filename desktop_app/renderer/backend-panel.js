"use strict";
// Backend status pill + log-tail overlay. Stop is only enabled in `owned`
// mode — an attached backend (started by the watchdog or manually) is never
// the shell's to kill (spec R2.1, R2.5).

const BackendPanel = (() => {
  const LOG_LIMIT = 500;
  let logLines = [];
  let mode = "down";

  const pill = () => document.getElementById("backend-pill");
  const overlay = () => document.getElementById("backend-overlay");
  const logEl = () => document.getElementById("backend-log");

  function renderStatus(status) {
    mode = status.mode;
    const p = pill();
    p.className = `pill ${mode}`;
    p.textContent = `backend: ${mode}`;
    document.getElementById("token-warning").hidden = status.tokenPresent;
    document.getElementById("backend-stop").disabled = mode !== "owned";
    document.getElementById("backend-start").disabled = mode !== "down";
    let detail = mode;
    if (status.lastError) detail += ` — ${status.lastError}`;
    document.getElementById("overlay-mode").textContent = detail;
    if (mode === "attached") {
      document.getElementById("backend-stop").title =
        "Started outside the shell (watchdog/manual) — not owned by this app";
    }
  }

  function appendLog(text) {
    for (const line of text.split(/\r?\n/)) {
      if (!line) continue;
      logLines.push(line);
    }
    if (logLines.length > LOG_LIMIT) logLines = logLines.slice(-LOG_LIMIT);
    if (!overlay().hidden) {
      logEl().textContent = logLines.join("\n");
      logEl().scrollTop = logEl().scrollHeight;
    }
  }

  async function refresh() {
    renderStatus(await window.agent.backend.status());
  }

  function init() {
    pill().addEventListener("click", () => {
      overlay().hidden = !overlay().hidden;
      if (!overlay().hidden) {
        logEl().textContent = logLines.join("\n");
        logEl().scrollTop = logEl().scrollHeight;
      }
    });
    document.getElementById("overlay-close").addEventListener("click", () => {
      overlay().hidden = true;
    });
    document.getElementById("backend-start").addEventListener("click", async () => {
      renderStatus(await window.agent.backend.start());
    });
    document.getElementById("backend-stop").addEventListener("click", async () => {
      if (confirm("Stop the backend? The chat, dashboard, and iPad bridge go down with it.")) {
        renderStatus(await window.agent.backend.stop());
      }
    });

    window.agent.backend.onStateChanged(renderStatus);
    window.agent.backend.onLog(appendLog);

    // Seed the tail with whatever main captured before this window loaded.
    window.agent.backend.status().then((status) => {
      logLines = status.logTail || [];
      renderStatus(status);
    });
  }

  return { init };
})();
