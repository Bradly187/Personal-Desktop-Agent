"use strict";
// Command palette (Ctrl+Shift+P) + fuzzy file opener (Ctrl+P). Keyboard-first
// on purpose: pointing is the expensive input here (SG-3, gap analysis
// 2026-07-03), so everything is reachable with arrows + Enter, and rows use
// the same RA-friendly ≥28px targets as the tree.

const Palette = (() => {
  const MAX_RESULTS = 50;
  const RECENTS_KEY = "recentFiles";
  const RECENTS_MAX = 50;

  let overlay = null;
  let input = null;
  let list = null;
  let mode = "commands"; // commands | files
  let entries = [];      // current candidates: {label, detail?, run}
  let filtered = [];
  let sel = 0;
  let fileIndex = null;  // {root, files, truncated} — cached per app run
  let indexPromise = null;

  // ---- Fuzzy matching --------------------------------------------------------
  // Subsequence match; bonuses for consecutive hits and word/path-segment
  // starts, penalty for gaps. Good enough for file paths without a library.

  function fuzzyScore(query, text) {
    const q = query.toLowerCase();
    const t = text.toLowerCase();
    if (!q) return 0;
    let score = 0;
    let ti = 0;
    let lastHit = -2;
    for (let qi = 0; qi < q.length; qi++) {
      const found = t.indexOf(q[qi], ti);
      if (found === -1) return -1;
      score += found === lastHit + 1 ? 8 : 1;                    // consecutive run
      if (found === 0 || "\\/._- ".includes(t[found - 1])) score += 6; // segment start
      score -= Math.min(found - ti, 20) * 0.1;                   // gap penalty
      lastHit = found;
      ti = found + 1;
    }
    score -= t.length * 0.01; // shorter paths win ties
    return score;
  }

  // ---- Candidates ------------------------------------------------------------

  function recents() {
    try { return JSON.parse(localStorage.getItem(RECENTS_KEY)) || []; } catch { return []; }
  }

  function noteRecent(path) {
    const r = [path, ...recents().filter((p) => p !== path)].slice(0, RECENTS_MAX);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(r));
  }

  async function ensureFileIndex() {
    if (fileIndex) return fileIndex;
    if (!indexPromise) {
      indexPromise = (async () => {
        const status = await window.agent.backend.status();
        fileIndex = await window.agent.fs.listFilesRec(status.repoRoot);
        return fileIndex;
      })();
    }
    return indexPromise;
  }

  function fileEntries() {
    const seen = new Set();
    const out = [];
    const add = (path, detail) => {
      if (seen.has(path)) return;
      seen.add(path);
      out.push({
        label: path.split(/[\\/]/).pop(),
        detail,
        path,
        run: () => { noteRecent(path); App.openFile(path); },
      });
    };
    for (const p of recents()) add(p, p);
    if (fileIndex) for (const p of fileIndex.files) add(p, p);
    return out;
  }

  function commandEntries() {
    const cmds = [
      { label: "Go to Dashboard", run: () => App.command("tab", 1) },
      { label: "Save Active File", detail: "Ctrl+S", run: () => App.command("save") },
      { label: "Diff Active File vs Git HEAD", detail: "Ctrl+Shift+D", run: () => App.command("diffActive") },
      { label: "Close Tab", detail: "Ctrl+W", run: () => App.command("closeTab") },
      { label: "Next Tab", detail: "Ctrl+PgDn", run: () => App.command("tabNext") },
      { label: "Previous Tab", detail: "Ctrl+PgUp", run: () => App.command("tabPrev") },
      { label: "Toggle File Tree", detail: "Ctrl+B", run: () => App.command("toggleTree") },
      { label: "Collapse All Folders", run: () => FsTree.collapseAll() },
      { label: "Focus Terminal", detail: "Ctrl+`", run: () => App.command("focusTerminal") },
      { label: "Go to File…", detail: "Ctrl+P", run: () => open("files") },
      { label: "Backend: Show Status & Logs", run: () => BackendPanel.toggleOverlay() },
      { label: "Backend: Start", run: () => window.agent.backend.start() },
    ];
    for (const t of App.listTabs()) {
      cmds.push({ label: `Switch to Tab: ${t.title}`, detail: t.isDiff ? t.path : t.id, run: () => App.command("tabId", t.id) });
    }
    return cmds;
  }

  // ---- Rendering ------------------------------------------------------------

  function render() {
    const q = input.value.trim();
    const scored = [];
    for (const e of entries) {
      const hay = e.detail && e.path ? e.path : e.label;
      const s = fuzzyScore(q, hay);
      if (s >= 0) scored.push([s, e]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    filtered = scored.slice(0, MAX_RESULTS).map(([, e]) => e);
    sel = Math.min(sel, Math.max(0, filtered.length - 1));

    list.textContent = "";
    filtered.forEach((e, i) => {
      const row = document.createElement("div");
      row.className = "palette-row" + (i === sel ? " selected" : "");
      const label = document.createElement("span");
      label.className = "palette-label";
      label.textContent = e.label;
      row.appendChild(label);
      if (e.detail) {
        const detail = document.createElement("span");
        detail.className = "palette-detail";
        detail.textContent = e.detail;
        row.appendChild(detail);
      }
      row.addEventListener("click", () => { close(); e.run(); });
      list.appendChild(row);
    });
    if (filtered.length === 0) {
      const empty = document.createElement("div");
      empty.className = "palette-empty";
      empty.textContent = mode === "files" && !fileIndex ? "Indexing…" : "No matches";
      list.appendChild(empty);
    }
    const selRow = list.children[sel];
    if (selRow) selRow.scrollIntoView({ block: "nearest" });
  }

  // ---- Open / close / keys ---------------------------------------------------

  async function open(m) {
    mode = m;
    sel = 0;
    overlay.hidden = false;
    input.value = "";
    input.placeholder = mode === "files" ? "Type a file name…" : "Type a command…";
    if (mode === "files") {
      entries = fileEntries(); // recents render immediately…
      render();
      await ensureFileIndex(); // …repo index streams in when the walk finishes
      if (!overlay.hidden && mode === "files") {
        entries = fileEntries();
        render();
      }
    } else {
      entries = commandEntries();
      render();
    }
    input.focus();
  }

  function close() {
    overlay.hidden = true;
  }

  function onKeyDown(e) {
    if (e.key === "Escape") { close(); e.preventDefault(); }
    else if (e.key === "ArrowDown") { sel = Math.min(sel + 1, filtered.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter") {
      const hit = filtered[sel];
      if (hit) { close(); hit.run(); }
      e.preventDefault();
    }
  }

  function init() {
    overlay = document.getElementById("palette-overlay");
    input = document.getElementById("palette-input");
    list = document.getElementById("palette-list");
    input.addEventListener("input", () => { sel = 0; render(); });
    input.addEventListener("keydown", onKeyDown);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
  }

  return { init, open, close, noteRecent };
})();
