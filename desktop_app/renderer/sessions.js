"use strict";
// Recent agent runs (SG-1, gap analysis 2026-07-03), read from the backend's
// existing /api/recent-traces — the shell adds a surface, not a store. Polls
// while the backend is healthy; goes quiet (with a note) when it isn't.
// A run that completes after >10s also raises an OS toast: quick commands
// finish while you watch, long ones are the ones you walked away from.

const Sessions = (() => {
  const BACKEND = "http://127.0.0.1:8770";
  const POLL_MS = 5000;
  const LIMIT = 25;
  const TOAST_MIN_LATENCY_MS = 10000;
  const COLLAPSE_KEY = "runsCollapsed";

  let listEl = null;
  let countEl = null;
  let seeded = false;
  const known = new Set(); // trace_ids already seen (no toast on backlog)

  function relTime(ts) {
    const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
    if (s < 60) return `${Math.floor(s)}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function render(rows) {
    countEl.textContent = rows.length ? String(rows.length) : "";
    listEl.textContent = "";
    if (rows.length === 0) {
      const empty = document.createElement("div");
      empty.className = "runs-empty";
      empty.textContent = "No runs yet";
      listEl.appendChild(empty);
      return;
    }
    for (const r of rows) {
      const row = document.createElement("div");
      row.className = "runs-row";
      row.title = r.error_msg
        ? `${r.trace_id}\n${r.error_msg}`
        : `${r.trace_id}\n${r.tokens_in}→${r.tokens_out} tokens`;

      const dot = document.createElement("span");
      dot.className = "runs-dot " + (r.success ? "ok" : "fail");
      const label = document.createElement("span");
      label.className = "runs-label";
      label.textContent = `${r.action || "?"} · ${r.source || "?"}`;
      const meta = document.createElement("span");
      meta.className = "runs-meta";
      const secs = r.latency_ms != null ? `${(r.latency_ms / 1000).toFixed(1)}s · ` : "";
      meta.textContent = `${secs}${relTime(r.ts)}`;

      row.append(dot, label, meta);
      // Replay/detail lives in the dashboard — the run row just takes you there.
      row.addEventListener("click", () => App.command("tabId", "dashboard"));
      listEl.appendChild(row);
    }
  }

  function maybeToast(rows) {
    for (const r of rows) {
      if (known.has(r.trace_id)) continue;
      known.add(r.trace_id);
      if (!seeded) continue; // backlog on first poll, not news
      if ((r.latency_ms || 0) >= TOAST_MIN_LATENCY_MS) {
        window.agent.notify.toast(
          r.success ? "Run finished" : "Run failed",
          `${r.action || "run"} · ${(r.latency_ms / 1000).toFixed(0)}s${r.error_msg ? ` — ${r.error_msg}` : ""}`,
        );
      }
    }
    seeded = true;
  }

  async function poll() {
    try {
      const resp = await fetch(`${BACKEND}/api/recent-traces?limit=${LIMIT}`, {
        signal: AbortSignal.timeout(3000),
      });
      if (!resp.ok) throw new Error(String(resp.status));
      const rows = await resp.json();
      maybeToast(rows);
      render(rows);
    } catch {
      countEl.textContent = "";
      listEl.textContent = "";
      const off = document.createElement("div");
      off.className = "runs-empty";
      off.textContent = "Backend not reachable";
      listEl.appendChild(off);
    } finally {
      setTimeout(poll, POLL_MS);
    }
  }

  function init() {
    listEl = document.getElementById("runs-list");
    countEl = document.getElementById("runs-count");
    const panel = document.getElementById("runs-panel");
    const header = document.getElementById("runs-header");
    if (localStorage.getItem(COLLAPSE_KEY) === "1") panel.classList.add("collapsed");
    header.addEventListener("click", () => {
      panel.classList.toggle("collapsed");
      localStorage.setItem(COLLAPSE_KEY, panel.classList.contains("collapsed") ? "1" : "0");
    });
    poll();
  }

  return { init };
})();
