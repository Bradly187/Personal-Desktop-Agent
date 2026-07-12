/* Desktop Agent — unified observability dashboard.
 *
 * Live panels (Now KPIs, Activity feed) ride the same /chat WebSocket the chat UI
 * uses: the server broadcasts {type:"dash_event"} frames for ops topics. Historical
 * panels (Traces/replay, Trends, Cost) poll the read-only /api/* JSON endpoints.
 * Vanilla JS, no build step — matches the chat client. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
  const pct = (v) => v == null ? "—" : (v * 100).toFixed(0) + "%";
  const ms = (v) => v == null ? "—" : Math.round(v) + " ms";
  const fixed = (v, d=2) => v == null ? "—" : Number(v).toFixed(d);
  const clock = (t) => { try { return new Date((t || 0) * 1000).toLocaleTimeString(); } catch { return ""; } };
  const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function getJSON(url) {
    try { const r = await fetch(url); if (!r.ok) return null; return await r.json(); }
    catch { return null; }
  }

  // ── Now: KPI cards (poll /api/metrics) ──────────────────────────────────────
  function sparkline(data, color) {
    if (!data || data.length < 2) return null;
    const max = Math.max(...data) || 1;
    const min = Math.min(...data);
    const range = max - min || 1;
    const pts = data.map((d, i) => `${(i / (data.length - 1) * 100).toFixed(1)},${(100 - (d - min) / range * 100).toFixed(1)}`).join(" ");
    
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 -5 100 110");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.width = "40px";
    svg.style.height = "16px";
    svg.style.marginLeft = "8px";
    
    const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", pts);
    polyline.setAttribute("fill", "none");
    polyline.setAttribute("stroke", color);
    polyline.setAttribute("stroke-width", "8");
    polyline.setAttribute("stroke-linecap", "round");
    polyline.setAttribute("stroke-linejoin", "round");
    
    svg.appendChild(polyline);
    return svg;
  }

  function kpi(label, value, sub, spark = null) {
    const c = el("div", "kpi");
    const v = el("div", "kpi-val", value);
    if (spark) {
      v.style.display = "flex";
      v.style.alignItems = "center";
      v.appendChild(spark);
    }
    c.appendChild(v);
    c.appendChild(el("div", "kpi-label", label));
    if (sub != null) c.appendChild(el("div", "kpi-sub", sub));
    return c;
  }
  async function refreshMetrics() {
    // Command-scoped KPIs prefer the live-session rollup (/api/session-live) so the
    // panel isn't empty after a restart; process-lifetime counters (/api/metrics,
    // reset every start) are the fallback. VRAM/pain-day/EMA stay process-wide.
    const [m, sess] = await Promise.all([getJSON("/api/metrics?series=1"), getJSON("/api/session-live")]);
    const box = $("kpis");
    if (!m) { box.innerHTML = ""; box.appendChild(el("div", "empty", "metrics endpoint unavailable")); return; }
    const g = m.gauges || {}, c = m.counters || {}, h = (m.histograms || {}).latency_ms || {};
    const live = (sess && sess.total_commands > 0) ? sess : null;
    const win = live ? "this session" : "lifetime";
    const series = m.series || [];
    const color = "var(--text-dim)";
    box.innerHTML = "";
    box.appendChild(kpi("commands", String(live ? live.total_commands : (c.commands_total ?? 0)), win, sparkline(series.map(s => s.total_commands || 0), color)));
    box.appendChild(kpi("success", live ? pct(live.success_rate) : pct(g.success_rate_1m), live ? "session" : "1m", sparkline(series.map(s => s.success_rate || 0), color)));
    box.appendChild(kpi("cloud", live ? pct(live.cloud_escalation_rate) : pct(g.cloud_rate_1m), live ? "session" : "1m", sparkline(series.map(s => s.cloud_escalation_rate || 0), color)));
    box.appendChild(kpi("latency p50", live ? ms(live.latency_p50_ms) : ms(h.p50), win, sparkline(series.map(s => s.latency_p50_ms || 0), color)));
    box.appendChild(kpi("latency p95", live ? ms(live.latency_p95_ms) : ms(h.p95), win, sparkline(series.map(s => s.latency_p95_ms || 0), color)));
    const pd = g.pain_day_score;
    document.body.classList.toggle("flare-mode", pd != null && pd >= 0.6);
    const pdc = kpi("pain-day", fixed(pd), undefined, sparkline(series.map(s => s.pain_day_pct || 0), color)); if (pd != null && pd >= 0.6) pdc.classList.add("warn");
    box.appendChild(pdc);
    box.appendChild(kpi("VRAM free", g.vram_free_gb == null ? "—" : fixed(g.vram_free_gb, 1) + " GB"));
    // Accessibility + backpressure KPIs (R5). Absent gauge → "—", never 0.
    const ct = c.commands_total ?? 0;
    box.appendChild(kpi("clarify", ct ? pct((c.commands_clarify ?? 0) / ct) : "—", "asked Brad"));
    box.appendChild(kpi("voice q", g.whisper_logprob_ema == null ? "—" : fixed(g.whisper_logprob_ema), "logprob"));
    box.appendChild(kpi("gesture q", g.gesture_conf_ema == null ? "—" : fixed(g.gesture_conf_ema), "conf"));
    const qd = g.scheduler_queue_depth;
    const qdc = kpi("queue", qd == null ? "—" : String(Math.round(qd)), "scheduler");
    if (qd != null && qd > 20) qdc.classList.add("warn");
    box.appendChild(qdc);
    if (m.uptime_s != null) {
      $("now-sub").textContent = "uptime " + Math.round(m.uptime_s / 60) + "m" +
        (live ? " · session " + live.session_id : "");
    }
  }

  // ── Activity: live feed (WS dash_event) ─────────────────────────────────────
  const FEED_MAX = 200;
  function pushFeed(ev) {
    const feed = $("feed");
    const empty = feed.querySelector(".empty"); if (empty) empty.remove();
    const kind = ev.kind || "event";
    const row = el("div", "feed-row kind-" + kind + (ev.severity === "warn" ? " warn" : ""));
    row.appendChild(el("span", "feed-time", clock(ev.ts)));
    row.appendChild(el("span", "feed-kind", kind));
    // "command" frames carry structured fields; every other kind (incl. unknown
    // ones from future topics) falls back to the server-rendered ev.text.
    let text = ev.text;
    if (kind === "command") {
      row.classList.add(ev.success ? "ok" : "fail");
      text = `${ev.action || "?"} · ${ev.route || "?"} · ${ms(ev.latency_ms)}`;
    }
    row.appendChild(el("span", "feed-text", text || ""));
    feed.insertBefore(row, feed.firstChild);
    while (feed.childElementCount > FEED_MAX) feed.removeChild(feed.lastChild);
  }

  // ── Alerts (poll /api/alerts) ───────────────────────────────────────────────
  async function refreshAlerts() {
    const res = await getJSON("/api/alerts?limit=50");
    const box = $("alerts"); box.innerHTML = "";
    const alerts = (res && res.alerts) || [];
    if (!alerts.length) { box.appendChild(el("div", "empty", "no alerts 🎉")); $("alerts-sub").textContent = ""; return; }
    const active = alerts.filter(a => a.active).length;
    $("alerts-sub").textContent = active ? `${active} active` : `${alerts.length} recent`;
    for (const a of alerts) {
      const row = el("div", "alert-row" + (a.active ? " active" : " recovered"));
      row.appendChild(el("span", "alert-time", clock(a.ts)));
      row.appendChild(el("span", "alert-src", a.metric || a.source || ""));
      row.appendChild(el("span", "alert-text", a.text || ""));
      box.appendChild(row);
    }
  }

  // ── Backend health strip (poll /api/health-backends) ────────────────────────
  async function refreshHealth() {
    const res = await getJSON("/api/health-backends");
    const box = $("health"); box.innerHTML = "";
    const items = (res && res.backends) || [];
    for (const b of items) {
      const up = b.status === "up" || b.status === "configured";
      const unknown = b.status === "unknown";
      const cls = unknown ? "unknown" : (up ? "up" : "down");
      const chip = el("span", "health-chip " + cls);
      chip.appendChild(el("span", "health-dot"));
      chip.appendChild(el("span", "health-name", b.name));
      chip.appendChild(el("span", "health-status", b.status));
      chip.title = b.detail || "";
      box.appendChild(chip);
    }
  }

  // ── Traces + replay (poll + on-demand) ──────────────────────────────────────
  const _seenSources = new Set();
  function _traceQuery() {
    const q = ["limit=25"];
    const src = $("tf-source").value, suc = $("tf-success").value;
    if (src) q.push("source=" + encodeURIComponent(src));
    if (suc !== "") q.push("success=" + suc);
    return "/api/recent-traces?" + q.join("&");
  }
  async function refreshTraces() {
    const rows = await getJSON(_traceQuery());
    const box = $("traces"); box.innerHTML = "";
    if (!rows || !rows.length) { box.appendChild(el("div", "empty", "no traced commands")); $("traces-sub").textContent = ""; return; }
    $("traces-sub").textContent = rows.length + " recent";
    for (const r of rows) {
      // Grow the source filter as new sources appear in the data.
      if (r.source && !_seenSources.has(r.source)) {
        _seenSources.add(r.source);
        const opt = el("option", null, r.source); opt.value = r.source;
        $("tf-source").appendChild(opt);
      }
      const item = el("button", "trace-item" + (r.success ? "" : " failrow"));
      item.appendChild(el("span", "t-id", (r.trace_id || "").slice(0, 8)));
      item.appendChild(el("span", "t-src", r.source || ""));
      item.appendChild(el("span", "t-act", r.action || ""));
      const tok = (r.tokens_in || 0) + (r.tokens_out || 0);
      item.appendChild(el("span", "t-tok", tok ? tok.toLocaleString() + "t" : ""));
      item.appendChild(el("span", "t-lat", ms(r.latency_ms)));
      item.appendChild(el("span", "t-ok " + (r.success ? "ok" : "fail"), r.success ? "✓" : "✗"));
      // Failed traces show their reason inline (no replay needed).
      if (!r.success && r.error_msg) {
        const err = el("div", "t-err", r.error_msg); err.title = r.error_msg;
        item.appendChild(err);
      }
      item.onclick = () => replay(r.trace_id);
      box.appendChild(item);
    }
  }
  async function replay(tid) {
    const out = $("replay"); out.innerHTML = ""; out.appendChild(el("div", "empty", "loading…"));
    const res = await getJSON("/api/replay/" + encodeURIComponent(tid));
    out.innerHTML = "";
    if (!res || !res.summary || !res.summary.found) { out.appendChild(el("div", "empty", "no data for " + tid)); return; }
    const s = res.summary;
    const head = el("div", "replay-head",
      `trace ${tid.slice(0,8)} · ${s.route || "?"} · ${s.gate || "?"} · ${ms(s.latency_ms)} · ` +
      `tokens ${s.tokens_in}/${s.tokens_out}`);
    out.appendChild(head);
    
    if (!res.timeline.length) return;
    const tl = el("div", "waterfall");
    tl.style.padding = "6px 0";
    
    const t0 = res.timeline[0].t;
    let tEnd = t0;
    for (const e of res.timeline) {
      const dur = e.detail?.dur_ms || e.detail?.latency_ms || 0;
      tEnd = Math.max(tEnd, e.t + dur / 1000);
    }
    const totalMs = Math.max(1, (tEnd - t0) * 1000);
    
    for (const e of res.timeline) {
      const dur = e.detail?.dur_ms || e.detail?.latency_ms || 0;
      const relMs = (e.t - t0) * 1000;
      
      const r = el("div", "wf-row tl-" + e.kind);
      r.style.cursor = "pointer";
      r.style.padding = "4px 14px";
      r.style.borderBottom = "1px solid rgba(255,255,255,0.03)";
      r.style.font = "12px/1.4 var(--mono)";
      r.style.display = "flex";
      r.style.flexDirection = "column";
      
      const header = el("div", "wf-header");
      header.style.display = "flex";
      header.style.gap = "10px";
      header.style.alignItems = "center";
      
      const leftW = Math.max(0, relMs / totalMs * 100);
      const barW = Math.max(0.5, dur / totalMs * 100);
      
      const barContainer = el("div", "wf-bar-wrap");
      barContainer.style.flex = "1";
      barContainer.style.height = "14px";
      barContainer.style.position = "relative";
      barContainer.style.background = "rgba(255,255,255,0.05)";
      barContainer.style.borderRadius = "4px";
      
      const bar = el("div", "wf-bar");
      bar.style.position = "absolute";
      bar.style.left = leftW + "%";
      bar.style.width = barW + "%";
      bar.style.height = "100%";
      bar.style.borderRadius = "4px";
      bar.style.background = e.kind === "span" ? "var(--accent)" : 
                             e.kind === "inference" ? "var(--run)" : 
                             e.kind === "audit" ? "var(--fail)" : "var(--text-dim)";
                             
      barContainer.appendChild(bar);
      
      const lbl = el("div", "wf-lbl");
      lbl.style.minWidth = "140px";
      let lblText = e.kind + " " + e.label;
      if (e.kind === "inference" && e.detail?.cost != null) lblText += ` ($${e.detail.cost.toFixed(4)})`;
      lbl.textContent = lblText;
      
      const timeLbl = el("div", "wf-time");
      timeLbl.style.minWidth = "60px";
      timeLbl.style.textAlign = "right";
      timeLbl.style.color = "var(--text-dim)";
      timeLbl.textContent = dur > 0 ? ms(dur) : `+${Math.round(relMs)}ms`;
      
      header.appendChild(lbl);
      header.appendChild(barContainer);
      header.appendChild(timeLbl);
      r.appendChild(header);
      
      const det = el("pre", "wf-detail");
      det.style.display = "none";
      det.style.margin = "6px 0 0";
      det.style.padding = "8px";
      det.style.background = "var(--bg)";
      det.style.borderRadius = "4px";
      det.style.color = "var(--text-dim)";
      det.style.fontSize = "11px";
      det.style.whiteSpace = "pre-wrap";
      det.style.wordBreak = "break-all";
      det.textContent = JSON.stringify(e.detail, null, 2);
      r.appendChild(det);
      
      r.onclick = () => { det.style.display = det.style.display === "none" ? "block" : "none"; };
      tl.appendChild(r);
    }
    out.appendChild(tl);
  }

  // ── Trends (poll) ───────────────────────────────────────────────────────────
  const ARROW = { improving: "▲", worsening: "▼", flat: "=" };
  async function refreshTrends() {
    const res = await getJSON("/api/trends?limit=20");
    const box = $("trends"); box.innerHTML = "";
    if (!res || !res.n_sessions) { box.appendChild(el("div", "empty", "no session summaries yet")); return; }
    $("trends-sub").textContent = res.n_sessions + " sessions";
    // deltas strip
    const strip = el("div", "deltas");
    for (const k of Object.keys(res.deltas)) {
      const d = res.deltas[k];
      const chip = el("span", "delta " + d.verdict);
      chip.textContent = `${d.label} ${ARROW[d.verdict] || ""}`;
      chip.title = `${d.older?.toFixed?.(2)} → ${d.recent?.toFixed?.(2)}`;
      strip.appendChild(chip);
    }
    box.appendChild(strip);
    // recent sessions table
    const tbl = el("table", "tbl");
    tbl.innerHTML = "<thead><tr><th>session</th><th>cmds</th><th>ok</th><th>cloud</th>" +
      "<th>p50</th><th>p95</th><th>corr</th><th>pain</th></tr></thead>";
    const tb = el("tbody");
    for (const s of res.sessions.slice().reverse()) {
      const tr = el("tr");
      tr.innerHTML = `<td>${s.session_id}</td><td>${s.total_commands ?? "—"}</td>` +
        `<td>${pct(s.success_rate)}</td><td>${pct(s.cloud_escalation_rate)}</td>` +
        `<td>${ms(s.latency_p50_ms)}</td><td>${ms(s.latency_p95_ms)}</td>` +
        `<td>${s.corrections_count ?? "—"}</td><td>${pct(s.pain_day_pct)}</td>`;
      tb.appendChild(tr);
    }
    tbl.appendChild(tb); box.appendChild(tbl);
  }

  // ── Model usage (poll) — every model, local + cloud ─────────────────────────
  async function refreshModels() {
    const res = await getJSON("/api/models?days=30");
    const box = $("models"); box.innerHTML = "";
    if (!res) { box.appendChild(el("div", "empty", "model usage unavailable")); return; }
    const t = res.totals || {};
    $("models-sub").textContent = (res.days ? `last ${res.days}d` : "all time");
    box.appendChild(el("div", "cost-total",
      `${(t.calls||0).toLocaleString()} calls · ${(t.tokens_in||0).toLocaleString()}/${(t.tokens_out||0).toLocaleString()} tok · ` +
      `$${(t.cost ?? 0).toFixed(4)} cloud`));
    const models = res.models || [];
    if (!models.length) { box.appendChild(el("div", "empty", "no inferences recorded")); return; }
    const tbl = el("table", "tbl");
    tbl.innerHTML = "<thead><tr><th>model</th><th>route</th><th>calls</th><th>tok in</th>" +
      "<th>tok out</th><th>avg</th><th>cost</th></tr></thead>";
    const tb = el("tbody");
    for (const m of models) {
      const tr = el("tr");
      const where = m.local ? "local" : "cloud";
      const tag = `<span class="tag ${where}">${where}</span>`;
      const cost = m.local ? "—" : `$${(m.cost || 0).toFixed(4)}`;
      const errs = m.errors ? ` <span class="tag err" title="${m.errors} error(s)">⚠${m.errors}</span>` : "";
      tr.innerHTML = `<td>${m.model}${errs}</td><td>${tag}</td><td>${(m.calls||0).toLocaleString()}</td>` +
        `<td>${(m.tokens_in||0).toLocaleString()}</td><td>${(m.tokens_out||0).toLocaleString()}</td>` +
        `<td>${ms(m.avg_latency_ms)}</td><td>${cost}</td>`;
      tb.appendChild(tr);
    }
    tbl.appendChild(tb); box.appendChild(tbl);
  }

  // ── Routing + errors (poll) ─────────────────────────────────────────────────
  async function refreshRouting() {
    const res = await getJSON("/api/routing?days=30");
    const rbox = $("routing"); rbox.innerHTML = "";
    const ebox = $("errors"); ebox.innerHTML = "";
    if (!res) { rbox.appendChild(el("div", "empty", "routing unavailable")); return; }
    const rt = res.routes || {};
    const local = rt.local || 0, cloud = rt.cloud || 0;
    $("routing-sub").textContent = `local ${local.toLocaleString()} · cloud ${cloud.toLocaleString()}`;
    const ok = res.success || {};
    if (ok.rate != null) $("errors-sub").textContent = `${pct(ok.rate)} ok · ${ok.fail || 0} failed`;

    const gates = res.gates || [];
    if (!gates.length) { rbox.appendChild(el("div", "empty", "no routed commands yet")); }
    const max = gates.reduce((m, g) => Math.max(m, g.count), 0) || 1;
    for (const g of gates) {
      const cls = g.gate === "bypass" ? "bypass" : (g.route === "cloud" ? "cloud" : "local");
      const row = el("div", "gaterow");
      row.title = g.desc || "";
      row.appendChild(el("span", "g-name", g.gate));
      const bar = el("div", "g-bar");
      const fill = el("div", "g-fill " + cls);
      fill.style.width = Math.max(2, Math.round(g.count / max * 100)) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("span", "g-ct", g.count.toLocaleString()));
      rbox.appendChild(row);
    }

    const errs = res.errors || [];
    if (!errs.length) { ebox.appendChild(el("div", "empty", "no inference errors 🎉")); return; }
    for (const e of errs) {
      const row = el("div", "err-row");
      const tag = el("span", "tag " + (e.local ? "local" : "cloud"), e.model);
      row.appendChild(tag);
      const msg = el("span", "err-msg", e.error); msg.title = e.error;
      row.appendChild(msg);
      row.appendChild(el("span", "err-ct", "×" + e.count));
      ebox.appendChild(row);
    }
  }

  // ── Cloud cost per day (poll /api/cost) ─────────────────────────────────────
  async function refreshCost() {
    const res = await getJSON("/api/cost?days=30");
    const box = $("cost"); box.innerHTML = "";
    const byDay = (res && res.by_day) || {};
    const days = Object.entries(byDay);
    if (!days.length) { box.appendChild(el("div", "empty", "no cloud spend in window")); $("cost-sub").textContent = ""; return; }
    const tot = res.totals || {};
    $("cost-sub").textContent = `$${(tot.cost || 0).toFixed(4)} · ${res.days || "all"}d`;
    const max = days.reduce((m, [, d]) => Math.max(m, d.cost), 0) || 1;
    const wrap = el("div", "costbars");
    for (const [day, d] of days.slice(-14)) {
      const row = el("div", "cost-row");
      row.appendChild(el("span", "cost-day", day.slice(5)));
      const bar = el("div", "cost-bar");
      const fill = el("div", "cost-fill"); fill.style.width = Math.max(2, Math.round(d.cost / max * 100)) + "%";
      bar.appendChild(fill); row.appendChild(bar);
      row.appendChild(el("span", "cost-amt", "$" + d.cost.toFixed(4)));
      wrap.appendChild(row);
    }
    box.appendChild(wrap);
  }

  // ── Goal queue (poll /api/goals) ────────────────────────────────────────────
  async function refreshGoals() {
    const res = await getJSON("/api/goals?limit=25");
    const box = $("goals"); box.innerHTML = "";
    const goals = (res && res.goals) || [];
    if (!goals.length) { box.appendChild(el("div", "empty", "no goals queued")); $("goals-sub").textContent = ""; return; }
    const active = goals.filter(g => g.status === "queued" || g.status === "running" || g.status === "scheduled").length;
    $("goals-sub").textContent = active ? `${active} pending` : `${goals.length} recent`;
    const tbl = el("table", "tbl");
    tbl.innerHTML = "<thead><tr><th>status</th><th>goal</th><th>src</th><th>try</th></tr></thead>";
    const tb = el("tbody");
    for (const g of goals) {
      const tr = el("tr");
      tr.innerHTML = `<td><span class="badge ${esc(g.status)}">${esc(g.status)}</span></td>` +
        `<td title="${esc(g.last_error || "")}">${esc((g.goal || "").slice(0, 60))}</td>` +
        `<td>${esc(g.source_trigger || "")}</td><td>${g.attempts}/${g.max_attempts}</td>`;
      tb.appendChild(tr);
    }
    tbl.appendChild(tb); box.appendChild(tbl);
  }

  // ── Dev escalations — READ-ONLY (poll /api/escalations) ─────────────────────
  async function refreshEscalations() {
    const res = await getJSON("/api/escalations?limit=25");
    const box = $("escalations"); box.innerHTML = "";
    const items = (res && res.escalations) || [];
    if (!items.length) { box.appendChild(el("div", "empty", "no escalations 🎉")); $("escalations-sub").textContent = ""; return; }
    const pending = items.filter(e => e.status === "pending").length;
    $("escalations-sub").textContent = pending ? `${pending} pending` : `${items.length} recent`;
    const tbl = el("table", "tbl");
    tbl.innerHTML = "<thead><tr><th>status</th><th>goal</th><th>reason</th><th>replans</th></tr></thead>";
    const tb = el("tbody");
    for (const e of items) {
      const tr = el("tr");
      tr.innerHTML = `<td><span class="badge ${esc(e.status)}">${esc(e.status)}</span></td>` +
        `<td>${esc((e.goal || "").slice(0, 60))}</td><td>${esc(e.reason || "")}</td><td>${e.replans}</td>`;
      tb.appendChild(tr);
    }
    tbl.appendChild(tb); box.appendChild(tbl);
  }

  // ── Recent corrections (poll /api/corrections) ──────────────────────────────
  async function refreshCorrections() {
    const res = await getJSON("/api/corrections?limit=25");
    const box = $("corrections"); box.innerHTML = "";
    const items = (res && res.corrections) || [];
    if (!items.length) { box.appendChild(el("div", "empty", "no corrections")); $("corrections-sub").textContent = ""; return; }
    $("corrections-sub").textContent = items.length + " recent";
    const tbl = el("table", "tbl");
    tbl.innerHTML = "<thead><tr><th>said</th><th>did</th><th>→ corrected</th></tr></thead>";
    const tb = el("tbody");
    for (const c of items) {
      const tr = el("tr");
      tr.innerHTML = `<td title="${esc(c.text || "")}">${esc((c.text || "").slice(0, 40))}</td>` +
        `<td>${esc(c.action || "")}</td><td>${esc(c.corrected_to || "")}</td>`;
      tb.appendChild(tr);
    }
    tbl.appendChild(tb); box.appendChild(tbl);
  }

  // ── WebSocket (live feed) ───────────────────────────────────────────────────
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/chat`);
    ws.onopen = () => { $("conn-dot").classList.add("on"); $("conn-label").textContent = "live"; };
    ws.onclose = () => { $("conn-dot").classList.remove("on"); $("conn-label").textContent = "reconnecting…"; setTimeout(connect, 1500); };
    ws.onmessage = (e) => {
      let f; try { f = JSON.parse(e.data); } catch { return; }
      if (f.type === "dash_event") {
        if (f.kind === "file_written") {
          $("text-editor-panel").style.display = "block";
          $("editor-file-path").textContent = f.path || "new file";
          $("editor-textarea").value = f.content || "";
          f.text = `file created: ${f.path}`;
        }
        pushFeed(f);
        if (f.kind === "command") refreshMetricsSoon();
        if (f.kind === "alert") refreshAlerts();
      }
    };
  }
  let _mt = null;
  function refreshMetricsSoon() { clearTimeout(_mt); _mt = setTimeout(refreshMetrics, 400); }

  // ── boot ────────────────────────────────────────────────────────────────────
  function pollSlow() {
    refreshAlerts(); refreshHealth();
    refreshTraces(); refreshTrends(); refreshModels(); refreshRouting();
    refreshCost(); refreshGoals(); refreshEscalations(); refreshCorrections();
  }
  function pollAll() { refreshMetrics(); pollSlow(); }
  $("tf-source").onchange = refreshTraces;
  $("tf-success").onchange = refreshTraces;
  pollAll();
  connect();
  setInterval(refreshMetrics, 3000);
  setInterval(pollSlow, 15000);
})();
