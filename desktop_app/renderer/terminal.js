"use strict";
// xterm.js ↔ node-pty wiring. One session; on exit an Enter keypress respawns.

const Term = (() => {
  let term = null;
  let fitAddon = null;
  let ptyId = null;
  let exited = false;

  async function spawn() {
    exited = false;
    const result = await window.agent.pty.spawn({ cols: term.cols, rows: term.rows });
    if (result.error) {
      term.writeln(`\x1b[31mterminal unavailable: ${result.error}\x1b[0m`);
      exited = true;
      return;
    }
    ptyId = result.ptyId;
  }

  function init() {
    term = new Terminal({
      fontSize: 15,
      fontFamily: "Cascadia Mono, Consolas, monospace",
      cursorBlink: true,
      theme: { background: "#181818" },
    });
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById("terminal"));
    fitAddon.fit();

    term.onData((data) => {
      if (exited) {
        if (data === "\r") {
          term.reset();
          spawn();
        }
        return;
      }
      if (ptyId !== null) window.agent.pty.write(ptyId, data);
    });

    window.agent.pty.onData(({ ptyId: id, data }) => {
      if (id === ptyId) term.write(data);
    });

    window.agent.pty.onExit(({ ptyId: id, exitCode }) => {
      if (id !== ptyId) return;
      exited = true;
      ptyId = null;
      term.writeln(`\r\n\x1b[33m[process exited with code ${exitCode} — press Enter to restart]\x1b[0m`);
    });

    const panel = document.getElementById("terminal-panel");
    new ResizeObserver(() => {
      if (!term) return;
      fitAddon.fit();
      if (ptyId !== null) window.agent.pty.resize(ptyId, term.cols, term.rows);
    }).observe(panel);

    spawn();
  }

  function focus() {
    if (term) term.focus();
  }

  // Visible buffer as plain text (smoke test + debugging).
  function text() {
    if (!term) return "";
    const buf = term.buffer.active;
    const lines = [];
    for (let i = 0; i < buf.length; i++) {
      const line = buf.getLine(i);
      if (line) lines.push(line.translateToString(true));
    }
    return lines.join("\n").trimEnd();
  }

  return { init, focus, text };
})();
