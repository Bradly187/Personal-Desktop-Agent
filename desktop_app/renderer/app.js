"use strict";
// Bootstrap + tab manager. Tab 0 is the pinned Dashboard iframe; file tabs
// delegate content to Editor (one Monaco instance, one model per file).

const App = (() => {
  const BACKEND = "http://127.0.0.1:8770";
  const DASHBOARD_TAB = "dashboard";

  /** @type {{id: string, title: string}[]} id = "dashboard" | file path */
  let tabs = [{ id: DASHBOARD_TAB, title: "Dashboard" }];
  let activeId = DASHBOARD_TAB;
  let iframesLive = false;

  const tabbar = document.getElementById("tabbar");
  const dashboardHost = document.getElementById("dashboard-host");
  const editorHost = document.getElementById("editor-host");

  // ---- Tabs ----------------------------------------------------------------

  function renderTabs() {
    tabbar.textContent = "";
    
    const toggleBtn = document.createElement("button");
    toggleBtn.className = "icon-button";
    toggleBtn.textContent = "☰";
    toggleBtn.title = "Toggle Sidebar (Ctrl+B)";
    toggleBtn.style.cssText = "background: transparent; border: none; color: var(--fg-dim); cursor: pointer; padding: 0 12px; font-size: 16px; border-right: 1px solid var(--border);";
    toggleBtn.onclick = () => document.getElementById("shell").classList.toggle("tree-hidden");
    tabbar.appendChild(toggleBtn);

    for (const tab of tabs) {
      const btn = document.createElement("button");
      btn.className = "tab" + (tab.id === activeId ? " active" : "");
      btn.setAttribute("role", "tab");
      btn.title = tab.id === DASHBOARD_TAB ? "Dashboard" : tab.id;

      const dot = document.createElement("span");
      dot.className = "dirty-dot";
      dot.textContent = "•";
      const label = document.createElement("span");
      label.textContent = tab.title;
      btn.append(dot, label);

      if (tab.id !== DASHBOARD_TAB) {
        if (Editor.isDirty(tab.id)) btn.classList.add("dirty");
        const close = document.createElement("button");
        close.className = "close";
        close.textContent = "✕";
        close.title = "Close tab";
        close.addEventListener("click", (e) => {
          e.stopPropagation();
          closeTab(tab.id);
        });
        btn.appendChild(close);
      }
      btn.addEventListener("click", () => selectTab(tab.id));
      tabbar.appendChild(btn);
    }
  }

  function selectTab(id) {
    if (!tabs.some((t) => t.id === id)) return;
    activeId = id;
    const tab = tabs.find(t => t.id === id);
    
    // Hide, never unmount: the dashboard iframe keeps its WebSocket alive.
    dashboardHost.hidden = id !== DASHBOARD_TAB;
    
    const imageHost = document.getElementById("image-host");
    if (imageHost) imageHost.hidden = !tab.isImage;
    
    editorHost.hidden = (id === DASHBOARD_TAB) || tab.isImage;
    
    if (tab.isImage && imageHost) {
      const img = document.getElementById("image-preview");
      img.src = `data:${tab.mimeType};base64,${tab.base64}`;
    } else if (id !== DASHBOARD_TAB) {
      Editor.activate(id);
    }
    renderTabs();
    persist();
  }

  function selectTabIndex(n) {
    if (n >= 1 && n <= tabs.length) selectTab(tabs[n - 1].id);
  }

  function cycleTab(dir) {
    const i = tabs.findIndex((t) => t.id === activeId);
    selectTab(tabs[(i + dir + tabs.length) % tabs.length].id);
  }

  async function openFile(path, opts = {}) {
    const existing = tabs.find((t) => t.id === path);
    if (existing && !opts.force) {
      selectTab(path);
      return;
    }
    const result = await Editor.open(path, opts);
    if (result.tooLarge) {
      const mb = (result.tooLarge / 1048576).toFixed(1);
      if (confirm(`${path}\n\n${mb} MB — large files can be slow to edit. Open anyway?`)) {
        return openFile(path, { force: true });
      }
      return;
    }
    if (result.binary) {
      const ext = path.toLowerCase().split('.').pop();
      if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext)) {
        const b64Result = await window.agent.fs.readFileBase64(path);
        if (b64Result.error) {
          alert(`Could not open image ${path}\n\n${b64Result.error}`);
          return;
        }
        if (!existing) {
          tabs.push({ id: path, title: path.split(/[\\/]/).pop(), isImage: true, base64: b64Result.base64, mimeType: b64Result.mimeType });
        } else {
          existing.isImage = true;
          existing.base64 = b64Result.base64;
          existing.mimeType = b64Result.mimeType;
        }
        selectTab(path);
        return;
      }
      alert(`${path}\n\nBinary file — not opening in the editor.`);
      return;
    }
    if (result.error) {
      alert(`Could not open ${path}\n\n${result.error}`);
      return;
    }
    if (!existing) {
      tabs.push({ id: path, title: path.split(/[\\/]/).pop() });
    }
    selectTab(path);
  }

  function closeTab(id) {
    if (id === DASHBOARD_TAB) return;
    if (Editor.isDirty(id) && !confirm(`${id} has unsaved changes.\n\nClose without saving?`)) {
      return;
    }
    Editor.close(id);
    tabs = tabs.filter((t) => t.id !== id);
    if (activeId === id) selectTab(DASHBOARD_TAB);
    else renderTabs();
    persist();
  }

  async function saveActive() {
    if (activeId === DASHBOARD_TAB) return;
    const result = await Editor.save(activeId);
    if (result && result.conflict) {
      if (confirm(`${activeId}\n\nChanged on disk since you opened it. Overwrite?`)) {
        await Editor.save(activeId, { force: true });
      }
    } else if (result && result.error) {
      alert(`Save failed: ${result.error}`);
    }
    renderTabs();
  }

  // ---- Persistence ---------------------------------------------------------

  function persist() {
    const files = tabs.filter((t) => t.id !== DASHBOARD_TAB).map((t) => t.id);
    localStorage.setItem("openFiles", JSON.stringify(files));
    localStorage.setItem("activeTab", activeId);
  }

  async function restore() {
    let files = [];
    try { files = JSON.parse(localStorage.getItem("openFiles")) || []; } catch { /* fresh */ }
    for (const f of files) {
      const st = await window.agent.fs.stat(f);
      if (!st.error && !st.isDir) await openFile(f);
    }
    const last = localStorage.getItem("activeTab");
    selectTab(last && tabs.some((t) => t.id === last) ? last : DASHBOARD_TAB);
  }

  // ---- Iframes: src only once the backend is healthy (spec R1.6) -----------

  async function gateIframes() {
    if (iframesLive) return;
    const status = await window.agent.backend.status();
    if (status.healthy) {
      iframesLive = true;
      const chat = document.getElementById("chat-frame");
      const dash = document.getElementById("dashboard-frame");
      chat.addEventListener("load", () => {
        document.getElementById("chat-placeholder").hidden = true;
      }, { once: true });
      dash.addEventListener("load", () => {
        document.getElementById("dashboard-placeholder").hidden = true;
      }, { once: true });
      chat.src = `${BACKEND}/`;
      dash.src = `${BACKEND}/dashboard`;
      return;
    }
    setTimeout(gateIframes, 1500);
  }

  // ---- Shortcuts (arrive via Menu accelerators — see main.js) --------------

  function handleShortcut({ action, arg }) {
    switch (action) {
      case "tab": selectTabIndex(arg); break;
      case "tabNext": cycleTab(1); break;
      case "tabPrev": cycleTab(-1); break;
      case "closeTab": closeTab(activeId); break;
      case "save": saveActive(); break;
      case "toggleTree": document.getElementById("shell").classList.toggle("tree-hidden"); break;
      case "focusTerminal": Term.focus(); break;
    }
  }

  // ---- Boot ----------------------------------------------------------------

  async function init() {
    Splitters.initAll();
    BackendPanel.init();
    FsTree.init((path) => openFile(path));
    Term.init();
    await Editor.boot();
    window.agent.app.onShortcut(handleShortcut);
    renderTabs();
    gateIframes();
    restore();
  }

  window.addEventListener("DOMContentLoaded", init);

  return { openFile, renderTabs };
})();
