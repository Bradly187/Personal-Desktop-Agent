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

  let ws = null;
  let cur = null;        // current turn: {activity, bubble, streamed}
  let allowDestructive = true;

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

  // ── send ──────────────────────────────────────────────────────────────────
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

  function submit() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    addMsg("user", text);
    window.DAG.reset();
    newTurn();
    send({ type: "user_message", text: text });
    input.value = "";
    autogrow();
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  function autogrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
  input.addEventListener("input", autogrow);

  setConn(false, "connecting…");
  connect();
})();
