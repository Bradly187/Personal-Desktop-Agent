/* chat.js — chat transcript + WebSocket client for the desktop agent UI.
 *
 * Receives frames from core/chat_server.py and renders a Claude-Code-style
 * workbench transcript (specs/chat-workbench-parity):
 *   - turns keyed by trace_id (R1) — concurrent requests never cross streams
 *   - sanitized markdown bubbles with copy buttons (R2; vendored marked+DOMPurify)
 *   - collapsible step cards that update in place (R3)
 *   - Stop button + Esc to cancel an in-flight turn (R4)
 *   - approval cards carrying the pending diff / command / plan steps (R5/R6)
 *   - transcript persisted to localStorage across reloads (R7)
 *   - walkthrough artifact cards, "Undo this run", per-turn usage badge (R8)
 * Drives the DAG pane via window.DAG.
 */
(function () {
  "use strict";

  const transcript = document.getElementById("transcript");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const connDot = document.getElementById("conn-dot");
  const connLabel = document.getElementById("conn-label");
  const clearBtn = document.getElementById("clear-history");
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
  let allowDestructive = true;
  let attachments = [];  // [{id, name, kind}] pending for the next message

  // ── turns (R1) ────────────────────────────────────────────────────────────
  // trace_id → turn. Locally created turns queue in `pendingTurns` until the
  // server's `accepted` frame binds them to a trace (FIFO — WS frames are
  // ordered). Frames for unknown trace_ids are dropped, never misattributed.
  const turns = new Map();
  const pendingTurns = [];

  const HISTORY_KEY = "da_chat_history";
  const HISTORY_MAX_TURNS = 100;
  const HISTORY_MAX_CHARS = 2000000;
  const DIFF_COLLAPSE_LINES = 20;

  // ── markdown (R2) ─────────────────────────────────────────────────────────
  function mdAvailable() {
    return !!(window.marked && window.DOMPurify);
  }

  function renderMarkdown(el, text) {
    // Sanitized markdown render with plain-text fallback (R2.2, R2.4).
    if (!text) { el.textContent = ""; return; }
    if (!mdAvailable()) { el.textContent = text; return; }
    try {
      const html = window.DOMPurify.sanitize(
        window.marked.parse(text, { async: false, breaks: true })
      );
      el.innerHTML = html;
      el.classList.add("md");
    } catch (_) {
      el.textContent = text;
    }
  }

  function addCopyButtons(el) {
    el.querySelectorAll("pre").forEach((pre) => {
      if (pre.querySelector(".copy-btn")) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-btn";
      btn.textContent = "copy";
      btn.onclick = () => {
        const code = pre.querySelector("code");
        navigator.clipboard.writeText(code ? code.textContent : pre.textContent)
          .then(() => { btn.textContent = "copied"; setTimeout(() => { btn.textContent = "copy"; }, 1200); })
          .catch(() => {});
      };
      pre.appendChild(btn);
    });
  }

  // ── connection ────────────────────────────────────────────────────────────
  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/chat");
    ws.onopen = () => setConn(true, "connected");
    ws.onclose = () => {
      setConn(false, "reconnecting…");
      // The server drops trace→socket bindings on disconnect: in-flight turns
      // can never finish, so resolve them instead of leaving a stuck cursor.
      turns.forEach((t) => { if (!t.finished) finishTurn(t, null, { error: "connection lost" }); });
      pendingTurns.length = 0;
      setTimeout(connect, 1500);
    };
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

  function newTurn(userLabel) {
    const wrap = document.createElement("div");
    wrap.className = "turn";
    const activity = document.createElement("div");
    activity.className = "activity";
    wrap.appendChild(activity);
    const bubble = document.createElement("div");
    bubble.className = "msg assistant streaming";
    wrap.appendChild(bubble);
    const foot = document.createElement("div");
    foot.className = "turn-foot";
    wrap.appendChild(foot);
    transcript.appendChild(wrap);
    const t = {
      tid: null, wrap, activity, bubble, foot,
      md: "", streamed: false, finished: false, rewindable: false,
      renderQueued: false, stepCards: new Map(), approvalCards: [],
      userText: userLabel || "", stopBtn: null,
    };
    addStopBtn(t);
    pendingTurns.push(t);
    scroll();
    return t;
  }

  function addStopBtn(t) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "stop-btn";
    btn.textContent = "◼ stop";
    btn.title = "Cancel this request (Esc cancels the newest)";
    btn.onclick = () => cancelTurn(t);
    t.activity.appendChild(btn);
    t.stopBtn = btn;
  }

  function cancelTurn(t) {
    if (!t || t.finished || !t.tid) return;
    send({ type: "cancel", trace_id: t.tid });
    activityLine(t, "stopping…", "fail");
  }

  function newestActiveTurn() {
    let latest = null;
    turns.forEach((t) => { if (!t.finished) latest = t; });
    return latest;
  }

  function activityLine(t, text, kind) {
    if (!t) return;
    const stick = atBottom();
    const line = document.createElement("div");
    line.className = "line" + (kind ? " " + kind : "");
    line.textContent = text;
    t.activity.appendChild(line);
    if (stick) scroll();
  }

  // ── step cards (R3) ───────────────────────────────────────────────────────
  function stepCard(t, f) {
    const stick = atBottom();
    let card = t.stepCards.get(f.n);
    if (!card) {
      card = document.createElement("details");
      card.className = "step";
      card.innerHTML = '<summary></summary><div class="step-body"></div>';
      t.activity.appendChild(card);
      t.stepCards.set(f.n, card);
    }
    const status = f.status || "running";
    card.className = "step " + status;
    const summary = card.querySelector("summary");
    summary.textContent =
      "step " + f.n + " · " + (f.action || "") +
      (status === "running" ? " …" : "") + fmtMs(f.latency_ms);
    const body = card.querySelector(".step-body");
    body.textContent = "";
    if (f.args) {
      const args = document.createElement("div");
      args.className = "step-args";
      args.textContent = f.args;
      body.appendChild(args);
    }
    if (f.result) {
      const res = document.createElement("pre");
      res.className = "step-result";
      res.textContent = f.result;
      body.appendChild(res);
    }
    if (status === "failed") card.open = true;   // R3.4
    if (stick) scroll();
  }

  // ── approval cards (R5/R6) ────────────────────────────────────────────────
  function renderDiff(container, diffText) {
    const lines = String(diffText).split("\n");
    const pre = document.createElement("pre");
    pre.className = "diff";
    lines.forEach((ln) => {
      const span = document.createElement("span");
      span.className = "dl" +
        (ln.startsWith("+") && !ln.startsWith("+++") ? " add" :
         ln.startsWith("-") && !ln.startsWith("---") ? " del" :
         ln.startsWith("@@") ? " hunk" : "");
      span.textContent = ln + "\n";
      pre.appendChild(span);
    });
    if (lines.length > DIFF_COLLAPSE_LINES) {
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.textContent = "diff (" + lines.length + " lines)";
      det.appendChild(sum);
      det.appendChild(pre);
      container.appendChild(det);
    } else {
      container.appendChild(pre);
    }
  }

  function approvalCard(t, f) {
    const stick = atBottom();
    const card = document.createElement("div");
    card.className = "approval" + (f.destructive ? " destructive" : "");
    if (f.destructive) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "destructive";
      card.appendChild(tag);
    }
    const p = document.createElement("p");
    p.textContent = f.message || "The agent is requesting approval to proceed.";
    card.appendChild(p);
    // Informed-approval context (R5): exact file/diff/command being approved.
    if (f.file_path) {
      const fp = document.createElement("div");
      fp.className = "appr-file";
      fp.textContent = f.file_path;
      card.appendChild(fp);
    }
    if (f.diff) renderDiff(card, f.diff);
    if (f.command) {
      const cmd = document.createElement("pre");
      cmd.className = "appr-cmd";
      cmd.textContent = f.command;
      card.appendChild(cmd);
    }
    // Plan preview (R6): the proposed steps, reviewable before execution.
    if (Array.isArray(f.steps) && f.steps.length) {
      const ol = document.createElement("ol");
      ol.className = "appr-steps";
      f.steps.forEach((s) => {
        const li = document.createElement("li");
        li.textContent = s.action + (s.args ? " — " + s.args : "");
        ol.appendChild(li);
      });
      card.appendChild(ol);
    }
    const row = document.createElement("div");
    row.className = "row";
    const yes = document.createElement("button");
    yes.className = "yes"; yes.textContent = "Approve";
    const no = document.createElement("button");
    no.className = "no"; no.textContent = "Deny";
    row.appendChild(yes); row.appendChild(no);
    card.appendChild(row);
    function answer(approve) {
      send({ type: "approval_response", approve: approve });
      card.classList.add("answered");
      activityLine(t, approve ? "approved" : "denied", approve ? "ok" : "fail");
    }
    yes.onclick = () => answer(true);
    no.onclick = () => answer(false);
    (t ? t.activity : transcript).appendChild(card);
    if (t) t.approvalCards.push(card);
    if (stick) scroll();
  }

  // ── walkthrough artifact (R8.1) ───────────────────────────────────────────
  function walkthroughCard(t, markdown) {
    const stick = atBottom();
    const det = document.createElement("details");
    det.className = "artifact";
    const sum = document.createElement("summary");
    sum.textContent = "📝 Walkthrough";
    det.appendChild(sum);
    const body = document.createElement("div");
    body.className = "artifact-body";
    renderMarkdown(body, markdown);
    addCopyButtons(body);
    det.appendChild(body);
    (t ? t.wrap : transcript).appendChild(det);
    if (stick) scroll();
  }

  // ── frame handling ────────────────────────────────────────────────────────
  function handle(f) {
    // Global frames (no turn binding).
    switch (f.type) {
      case "ready":
        allowDestructive = !(f.config && f.config.allow_destructive === false);
        send({ type: "list_dirs" });
        return;
      case "dirs":
        renderDirs(f);
        return;
      case "active_dir":
        onActiveDir(f);
        return;
      case "accepted": {
        // Bind the oldest unbound local turn to its trace (R1) — FIFO matches
        // the server's per-message accept order on this ordered socket.
        const t = pendingTurns.shift();
        if (t) { t.tid = f.trace_id; turns.set(f.trace_id, t); }
        return;
      }
    }

    // Trace-targeted frames: route to the owning turn; drop unknowns (R1.2).
    const t = f.trace_id ? turns.get(f.trace_id) : null;
    if (!t) return;

    switch (f.type) {
      case "token":
        t.md += f.text || "";
        t.streamed = true;
        scheduleRender(t);
        break;
      case "gate":
        activityLine(t, "gate · " + (f.gate || "?") + (f.domain ? " [" + f.domain + "]" : "") + fmtMs(f.latency_ms));
        window.DAG.gateFlow(f.gate, null);
        break;
      case "executed":
        activityLine(t, (f.action || "action") + " · " + (f.route || "") + fmtMs(f.latency_ms),
          f.success === false ? "fail" : "ok");
        window.DAG.gateFlow(f.gate, f.action);
        break;
      case "plan":
        activityLine(t, "plan · " + (f.steps ? f.steps.length : 0) + " steps", "run");
        window.DAG.setPlan(f.steps || []);
        break;
      case "node":
        stepCard(t, f);
        window.DAG.setNode(f.n, f.status);
        break;
      case "approval":
        approvalCard(t, f);
        break;
      case "walkthrough":
        walkthroughCard(t, f.markdown || "");
        t.walkthrough = f.markdown || "";
        break;
      case "run_finalized":
        t.rewindable = !!f.rewindable;
        break;
      case "activity":
        activityLine(t, f.text || "", "fail");
        break;
      case "final":
        finishTurn(t, f.result || {}, { cancelled: !!f.cancelled, usage: f.usage });
        break;
      case "error":
        activityLine(t, "error: " + (f.error || "unknown"), "fail");
        finishTurn(t, null, { error: f.error || "unknown" });
        break;
    }
  }

  function fmtMs(ms) { return (ms || ms === 0) ? "  (" + Math.round(ms) + " ms)" : ""; }

  function scheduleRender(t) {
    // Re-parse the accumulated buffer at most once per animation frame (R2.4).
    if (t.renderQueued) return;
    t.renderQueued = true;
    requestAnimationFrame(() => {
      t.renderQueued = false;
      const stick = atBottom();
      renderMarkdown(t.bubble, t.md);
      if (stick) scroll();
    });
  }

  function usageBadge(t, usage) {
    if (!usage) return;
    const bits = [];
    if (usage.models && usage.models.length) bits.push(usage.models.join(", "));
    if (usage.tokens_in || usage.tokens_out) {
      bits.push((usage.tokens_in || 0) + "→" + (usage.tokens_out || 0) + " tok");
    }
    if (usage.cost_usd) bits.push("$" + usage.cost_usd.toFixed(4));
    if (!bits.length) return;
    const b = document.createElement("span");
    b.className = "usage";
    b.textContent = bits.join(" · ");
    t.foot.appendChild(b);
  }

  function undoButton(t) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "undo-btn";
    btn.textContent = "⟲ Undo this run";
    btn.title = "Roll back this run's file changes (asks for confirmation)";
    btn.onclick = () => {
      btn.disabled = true;
      const rt = newTurn("Undo this run");
      addMsg("user", "⟲ Undo this run");
      transcript.appendChild(rt.wrap);   // keep the turn after its user msg
      send({ type: "rewind" });
      scroll();
    };
    t.foot.appendChild(btn);
  }

  function finishTurn(t, result, opts) {
    if (!t || t.finished) return;
    opts = opts || {};
    t.finished = true;
    t.bubble.classList.remove("streaming");
    if (t.stopBtn) t.stopBtn.remove();
    // Approval cards left unanswered are moot once the turn resolves (the gate
    // timed out to DENY, or the run was stopped) — freeze them.
    t.approvalCards.forEach((c) => c.classList.add("answered"));
    if (!t.streamed) {
      const text = result && result.response != null ? String(result.response) : "";
      t.md = text;
      renderMarkdown(t.bubble, t.md);
    } else {
      renderMarkdown(t.bubble, t.md);
    }
    addCopyButtons(t.bubble);
    if (opts.error) activityLine(t, "error: " + opts.error, "fail");
    if (!t.bubble.textContent) t.bubble.remove();
    if (!t.activity.childElementCount) t.activity.remove();
    if (opts.usage) usageBadge(t, opts.usage);
    if (t.rewindable && !opts.cancelled && !opts.error) undoButton(t);
    if (t.tid) turns.delete(t.tid);
    persistTurn(t, result, opts);
    scroll();
  }

  // ── history persistence (R7) ──────────────────────────────────────────────
  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      if (!raw) return [];
      const h = JSON.parse(raw);
      return Array.isArray(h) ? h : [];
    } catch (_) { return []; }
  }

  function saveHistory(h) {
    try {
      while (h.length > HISTORY_MAX_TURNS) h.shift();
      let raw = JSON.stringify(h);
      while (raw.length > HISTORY_MAX_CHARS && h.length) {
        h.shift();
        raw = JSON.stringify(h);
      }
      localStorage.setItem(HISTORY_KEY, raw);
    } catch (_) { /* quota/corrupt storage never blocks the chat (R7.4) */ }
  }

  function persistTurn(t, result, opts) {
    if (opts.error && !t.md) return;      // nothing worth restoring
    const h = loadHistory();
    h.push({
      user: t.userText || "",
      md: t.md || "",
      steps: Array.from(t.stepCards.values()).map((c) => c.querySelector("summary").textContent),
      walkthrough: t.walkthrough || "",
      cancelled: !!opts.cancelled,
      ts: Date.now(),
    });
    saveHistory(h);
  }

  function rehydrate() {
    const h = loadHistory();
    if (!h.length) return;
    h.forEach((entry) => {
      if (entry.user) addMsg("user", entry.user);
      const wrap = document.createElement("div");
      wrap.className = "turn restored";
      if (entry.steps && entry.steps.length) {
        const act = document.createElement("div");
        act.className = "activity";
        entry.steps.forEach((s) => {
          const line = document.createElement("div");
          line.className = "line";
          line.textContent = s;
          act.appendChild(line);
        });
        wrap.appendChild(act);
      }
      if (entry.md) {
        const bubble = document.createElement("div");
        bubble.className = "msg assistant";
        renderMarkdown(bubble, entry.md);
        addCopyButtons(bubble);
        wrap.appendChild(bubble);
      }
      if (entry.walkthrough) walkthroughCard({ wrap }, entry.walkthrough);
      transcript.appendChild(wrap);
    });
    const sep = document.createElement("div");
    sep.className = "session-sep";
    sep.textContent = "— restored transcript · new session —";
    transcript.appendChild(sep);
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
          addMsg("assistant", "attach failed: " + (body.error || resp.status));
        }
      } catch (err) {
        addMsg("assistant", "attach failed: " + err);
      }
    }
  }

  // Paste-to-attach (R8.5): a pasted image uploads through the existing
  // /upload endpoint as a .png and joins the pending chips.
  input.addEventListener("paste", (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const files = [];
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const blob = item.getAsFile();
        if (blob) {
          files.push(new File([blob], "pasted-" + Date.now() + ".png", { type: "image/png" }));
        }
      }
    }
    if (files.length) { e.preventDefault(); uploadFiles(files); }
  });

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
    newTurn(label);
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
  // Esc cancels the newest in-flight turn (R4.5).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const t = newestActiveTurn();
      if (t) { e.preventDefault(); cancelTurn(t); }
    }
  });
  function autogrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
  input.addEventListener("input", autogrow);

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      try { localStorage.removeItem(HISTORY_KEY); } catch (_) {}
      transcript.innerHTML = "";
    });
  }

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

  rehydrate();
  setConn(false, "connecting…");
  connect();
})();
