"use strict";
// Monaco from local node_modules (no CDN, no bundler): the AMD loader is a
// plain <script>, and editor workers are created through a data-URL proxy that
// importScripts() the real worker from file://. If Chromium blocks that in a
// future Electron, Monaco logs a warning and falls back to main-thread
// tokenization — degraded but fully usable.

const Editor = (() => {
  const monacoBase = new URL("../node_modules/monaco-editor/min", location.href).href;

  let editor = null;
  let diffEditor = null;
  /** @type {Map<string, {model: any, savedVersionId: number, mtimeMs: number}>} */
  const files = new Map();
  /** @type {Map<string, {originalModel: any, untracked: boolean}>} keyed by file path */
  const diffs = new Map();

  function boot() {
    return new Promise((resolve) => {
      self.MonacoEnvironment = {
        getWorkerUrl: () => {
          const worker = `${monacoBase}/vs/base/worker/workerMain.js`;
          return `data:text/javascript;charset=utf-8,${encodeURIComponent(
            `self.MonacoEnvironment={baseUrl:'${monacoBase}/'};importScripts('${worker}');`,
          )}`;
        },
      };
      require.config({ paths: { vs: `${monacoBase}/vs` } });
      require(["vs/editor/editor.main"], () => {
        editor = monaco.editor.create(document.getElementById("editor"), {
          theme: "vs-dark",
          fontSize: 15,
          automaticLayout: true,
          model: null,
        });
        editor.onDidChangeModelContent(() => App.renderTabs());
        resolve();
      });
    });
  }

  function languageFor(path) {
    const ext = path.slice(path.lastIndexOf(".")).toLowerCase();
    const langs = monaco.languages.getLanguages();
    const hit = langs.find((l) => (l.extensions || []).includes(ext));
    return hit ? hit.id : "plaintext";
  }

  async function open(path, { force = false } = {}) {
    if (files.has(path)) return { ok: true };
    const result = await window.agent.fs.readFile(path, { force });
    if (!("content" in result)) return result; // tooLarge | binary | error
    const model = monaco.editor.createModel(result.content, languageFor(path));
    files.set(path, {
      model,
      savedVersionId: model.getAlternativeVersionId(),
      mtimeMs: result.mtimeMs,
    });
    return { ok: true };
  }

  function activate(path) {
    const f = files.get(path);
    if (f && editor) {
      editor.setModel(f.model);
      editor.focus();
    }
  }

  function isDirty(path) {
    const f = files.get(path);
    return !!f && f.model.getAlternativeVersionId() !== f.savedVersionId;
  }

  async function save(path, { force = false } = {}) {
    const f = files.get(path);
    if (!f) return null;
    const result = await window.agent.fs.writeFile({
      path,
      content: f.model.getValue(),
      expectedMtimeMs: f.mtimeMs,
      force,
    });
    if (result.ok) {
      f.savedVersionId = f.model.getAlternativeVersionId();
      f.mtimeMs = result.mtimeMs;
    }
    return result;
  }

  function close(path) {
    const f = files.get(path);
    if (!f) return;
    if (editor && editor.getModel() === f.model) editor.setModel(null);
    f.model.dispose();
    files.delete(path);
  }

  // ---- Diff view (working copy vs git HEAD) --------------------------------
  // The modified side IS the file's live model: edits made in the diff view
  // mark the tab dirty and Ctrl+S saves them like any other edit.

  function ensureDiffEditor() {
    if (diffEditor) return diffEditor;
    diffEditor = monaco.editor.createDiffEditor(document.getElementById("diff-editor"), {
      theme: "vs-dark",
      fontSize: 15,
      automaticLayout: true,
      originalEditable: false,
      renderSideBySide: true,
    });
    diffEditor.getModifiedEditor().onDidChangeModelContent(() => App.renderTabs());
    return diffEditor;
  }

  // {ok} | {ok, untracked} | {notRepo} | {error} — caller messages on the rest.
  async function openDiff(path) {
    const opened = await open(path);
    if (!opened.ok) return opened;
    const head = await window.agent.git.headContent(path);
    if (head.notRepo || head.error) return head;

    const old = diffs.get(path);
    if (old) old.originalModel.dispose();
    const originalModel = monaco.editor.createModel(head.content || "", languageFor(path));
    diffs.set(path, { originalModel, untracked: !!head.untracked });
    return { ok: true, untracked: !!head.untracked };
  }

  function activateDiff(path) {
    const d = diffs.get(path);
    const f = files.get(path);
    if (!d || !f) return;
    ensureDiffEditor().setModel({ original: d.originalModel, modified: f.model });
    const banner = document.getElementById("diff-banner");
    banner.hidden = !d.untracked;
    if (d.untracked) banner.textContent = "Not in HEAD yet — showing the whole file as added.";
    diffEditor.getModifiedEditor().focus();
  }

  function closeDiff(path) {
    const d = diffs.get(path);
    if (!d) return;
    if (diffEditor && diffEditor.getModel()?.original === d.originalModel) diffEditor.setModel(null);
    d.originalModel.dispose();
    diffs.delete(path);
  }

  return { boot, open, activate, isDirty, save, close, openDiff, activateDiff, closeDiff };
})();
