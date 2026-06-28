/* chat.js — chat transcript + WebSocket client for the desktop agent UI.
 *
 * Receives frames from core/chat_server.py and renders a Claude-Code-style
 * transcript: streamed assistant text, an activity log (gate decisions, plan
 * steps), and inline Approve/Deny cards. Drives the DAG pane via window.DAG.
 */
(function () {
  "use strict";

  const transcript = document.getElementById("transcript");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const connDot = document.getElementById("conn-dot");
  const connLabel = document.getElementById("conn-label");
  // Active-directory + attachment controls (progressive — absent in old assets).
  const dirBtn = document.getElementById("dir-btn");
  const dirLabel = document.getElementById("dir-label");
  const dirPanel = document.getElementById("dir-panel");
  const dirList = document.getElementById("dir-list");
  const dirInput = document.getElementById("dir-input");
  const dirSet = document.getElementById("dir-set");
  const dirConfirm = document.getElementById("dir-confirm");
  const attachBtn = document.getElementById("attach-btn");
  const fileInput = document.getElementById("file-input");
  const attachChips = document.getElementById("attach-chips");

  let ws = null;
  let cur = null;        // current turn: {activity, bubble, streamed}
  let allowDestructive = true;
  let attachments = [];  // [{id, name, kind}] pending for the next message

  // ── connection ────────────────────────────────────────────────────────────
  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/chat");
    ws.onopen = () => setConn(true, "connected");
    ws.onclose = () => { setConn(false, "reconnecting…"); setTimeout(connect, 1500); };
    ws.onerror = () => setConn(false, "error");
    ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (_) {} };
  }

  function setConn(on, label) {
    connDot.classList.toggle("on", on);
    connDot.title = label;
    connLabel.textContent = label;
    sendBtn.disabled = !on;
  }

  // ── transcript helpers ────────────────────────────────────────────────────
  function atBottom() { return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80; }
  function scroll() { transcript.scrollTop = transcript.scrollHeight; }

  function addMsg(role, text) {
    const d = document.createElement("div");
    d.className = "msg " + role;
    d.textContent = text || "";
    transcript.appendChild(d);
    scroll();
    return d;
  }

  function newTurn() {
    const activity = document.createElement("div");
    activity.className = "activity";
    transcript.appendChild(activity);
    const bubble = document.createElement("div");
    bubble.className = "msg assistant streaming";
    transcript.appendChild(bubble);
    cur = { activity, bubble, streamed: false };
    scroll();
  }

  function activityLine(text, kind) {
    if (!cur) return;
    const stick = atBottom();
    const line = document.createElement("div");
    line.className = "line" + (kind ? " " + kind : "");
    line.textContent = text;
    cur.activity.appendChild(line);
    if (stick) scroll();
  }

  function approvalCard(message, destructive) {
    const stick = atBottom();
    const card = document.createElement("div");
    card.className = "approval" + (destructive ? " destructive" : "");
    const tag = destructive ? '<span class="tag">destructive</span>' : "";
    card.innerHTML =
      tag +
      "<p>" + (message ? escapeHtml(message) : "The agent is requesting approval to proceed.") + "</p>" +
      '<div class="row"><button class="yes">Approve</button><button class="no">Deny</button></div>';
    function answer(approve) {
      send({ type: "approval_response", approve: approve });
      card.classList.add("answered");
      activityLine(approve ? "approved" : "denied", approve ? "ok" : "fail");
    }
    card.querySelector(".yes").onclick = () => answer(true);
    card.querySelector(".no").onclick = () => answer(false);
    transcript.appendChild(card);
    if (stick) scroll();
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // ── frame handling ────────────────────────────────────────────────────────
  function handle(f) {
    switch (f.type) {
      case "ready":
        allowDestructive = !(f.config && f.config.allow_destructive === false);
        send({ type: "list_dirs" });
        break;
      case "dirs":
        renderDirs(f);
        break;
      case "active_dir":
        onActiveDir(f);
        break;
      case "token":
        if (cur) { cur.bubble.textContent += f.text || ""; cur.streamed = true; if (atBottom()) scroll(); }
        break;
      case "gate":
        activityLine("gate · " + (f.gate || "?") + (f.domain ? " [" + f.domain + "]" : "") + fmtMs(f.latency_ms));
        window.DAG.gateFlow(f.gate, null);
        break;
      case "executed":
        activityLine((f.action || "action") + " · " + (f.route || "") + fmtMs(f.latency_ms),
          f.success === false ? "fail" : "ok");
        window.DAG.gateFlow(f.gate, f.action);
        break;
      case "plan":
        activityLine("plan · " + (f.steps ? f.steps.length : 0) + " steps", "run");
        window.DAG.setPlan(f.steps || []);
        break;
      case "node":
        activityLine("step " + f.n + " · " + (f.action || "") +
          (f.status === "running" ? " …" : "") + fmtMs(f.latency_ms),
          f.status === "success" ? "ok" : f.status === "failed" ? "fail" : "run");
        window.DAG.setNode(f.n, f.status);
        break;
      case "approval":
        approvalCard(f.message, f.destructive);
        break;
      case "activity":
        activityLine(f.text || "", "fail");
        break;
      case "final":
        finishTurn(f.result || {});
        break;
      case "error":
        if (cur) { cur.bubble.classList.remove("streaming"); }
        activityLine("error: " + (f.error || "unknown"), "fail");
        finishTurn(null);
        break;
    }
  }

  function fmtMs(ms) { return (ms || ms === 0) ? "  (" + Math.round(ms) + " ms)" : ""; }

  function finishTurn(result) {
    if (!cur) return;
    cur.bubble.classList.remove("streaming");
    if (!cur.streamed) {
      const text = result && (result.response != null ? String(result.response) : "");
      cur.bubble.textContent = text || "(done)";
    }
    if (!cur.bubble.textContent) cur.bubble.remove();
    if (!cur.activity.childElementCount) cur.activity.remove();
    cur = null;
    scroll();
  }

  // ── active directory ──────────────────────────────────────────────────────
  function shortPath(p) {
    if (!p) return "cwd";
    const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
    return parts[parts.length - 1] || p;
  }

  function renderDirs(f) {
    if (!dirList) return;
    if (dirLabel) dirLabel.textContent = shortPath(f.active_root);
    if (dirBtn) dirBtn.title = "Active directory: " + (f.active_root || "cwd");
    dirList.innerHTML = "";
    (f.writable_roots || []).forEach((root) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "dir-item" + (root === f.active_root ? " active" : "");
      item.textContent = root;
      item.onclick = () => send({ type: "set_active_dir", path: root });
      dirList.appendChild(item);
    });
  }

  function onActiveDir(f) {
    if (f.status === "confirm_required") {
      // A folder outside the allowlist — require an explicit confirm (R1.3).
      dirConfirm.classList.remove("hidden");
      dirConfirm.innerHTML = "";
      const msg = document.createElement("span");
      msg.textContent = "Add and switch to " + f.path + " ?";
      const yes = document.createElement("button");
      yes.textContent = "Confirm";
      yes.onclick = () => {
        dirConfirm.classList.add("hidden");
        send({ type: "set_active_dir", path: f.path, confirm: true });
      };
      dirConfirm.appendChild(msg);
      dirConfirm.appendChild(yes);
    } else if (f.status === "activated") {
      dirConfirm.classList.add("hidden");
      if (dirInput) dirInput.value = "";
      renderDirs(f);
      send({ type: "list_dirs" });
    } else if (f.status === "invalid" || f.error) {
      dirConfirm.classList.remove("hidden");
      dirConfirm.textContent = "Not a directory: " + (f.path || f.error || "");
    }
  }

  // ── attachments ───────────────────────────────────────────────────────────
  function renderChips() {
    if (!attachChips) return;
    attachChips.innerHTML = "";
    attachments.forEach((a, i) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = a.name;
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "×";
      x.onclick = () => { attachments.splice(i, 1); renderChips(); };
      chip.appendChild(x);
      attachChips.appendChild(chip);
    });
  }

  async function uploadFiles(files) {
    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file, file.name);
      try {
        const resp = await fetch("/upload", { method: "POST", body: fd });
        const body = await resp.json();
        if (resp.ok && body.attachment_id) {
          attachments.push({ id: body.attachment_id, name: body.name, kind: body.kind });
          renderChips();
        } else {
          activityLine("attach failed: " + (body.error || resp.status), "fail");
        }
      } catch (err) {
        activityLine("attach failed: " + err, "fail");
      }
    }
  }

  // ── send ──────────────────────────────────────────────────────────────────
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

  function submit() {
    const text = input.value.trim();
    if ((!text && !attachments.length) || !ws || ws.readyState !== 1) return;
    const ids = attachments.map((a) => a.id);
    const label = attachments.length
      ? (text ? text + "  " : "") + "📎 " + attachments.map((a) => a.name).join(", ")
      : text;
    addMsg("user", label);
    window.DAG.reset();
    newTurn();
    send({ type: "user_message", text: text, attachment_ids: ids });
    input.value = "";
    attachments = [];
    renderChips();
    autogrow();
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  function autogrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
  input.addEventListener("input", autogrow);

  // Directory + attachment controls (progressive — only wire when present).
  if (dirBtn) {
    dirBtn.addEventListener("click", () => dirPanel.classList.toggle("hidden"));
    dirSet.addEventListener("click", () => {
      const p = dirInput.value.trim();
      if (p) send({ type: "set_active_dir", path: p });
    });
    dirInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); dirSet.click(); }
    });
  }
  if (attachBtn) {
    attachBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files.length) uploadFiles(Array.from(fileInput.files));
      fileInput.value = "";
    });
  }

  setConn(false, "connecting…");
  connect();
})();
