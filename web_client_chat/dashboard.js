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

  async function getJSON(url) {
    try { const r = await fetch(url); if (!r.ok) return null; return await r.json(); }
    catch { return null; }
  }

  // ── Now: KPI cards (poll /api/metrics) ──────────────────────────────────────
  function kpi(label, value, sub) {
    const c = el("div", "kpi");
    c.appendChild(el("div", "kpi-val", value));
    c.appendChild(el("div", "kpi-label", label));
    if (sub != null) c.appendChild(el("div", "kpi-sub", sub));
    return c;
  }
  async function refreshMetrics() {
    // Command-scoped KPIs prefer the live-session rollup (/api/session-live) so the
    // panel isn't empty after a restart; process-lifetime counters (/api/metrics,
    // reset every start) are the fallback. VRAM/pain-day/EMA stay process-wide.
    const [m, sess] = await Promise.all([getJSON("/api/metrics"), getJSON("/api/session-live")]);
    const box = $("kpis");
    if (!m) { box.innerHTML = ""; box.appendChild(el("div", "empty", "metrics endpoint unavailable")); return; }
    const g = m.gauges || {}, c = m.counters || {}, h = (m.histograms || {}).latency_ms || {};
    const live = (sess && sess.total_commands != null) ? sess : null;
    const win = live ? "this session" : "lifetime";
    box.innerHTML = "";
    box.appendChild(kpi("commands", String(live ? live.total_commands : (c.commands_total ?? 0)), win));
    box.appendChild(kpi("success", live ? pct(live.success_rate) : pct(g.success_rate_1m), live ? "session" : "1m"));
    box.appendChild(kpi("cloud", live ? pct(live.cloud_escalation_rate) : pct(g.cloud_rate_1m), live ? "session" : "1m"));
    box.appendChild(kpi("latency p50", live ? ms(live.latency_p50_ms) : ms(h.p50), win));
    box.appendChild(kpi("latency p95", live ? ms(live.latency_p95_ms) : ms(h.p95), win));
    const pd = g.pain_day_score;
    const pdc = kpi("pain-day", fixed(pd)); if (pd != null && pd >= 0.6) pdc.classList.add("warn");
    box.appendChild(pdc);
    box.appendChild(kpi("VRAM free", g.vram_free_gb == null ? "—" : fixed(g.vram_free_gb, 1) + " GB"));
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

  // ── Traces + replay (poll + on-demand) ──────────────────────────────────────
  async function refreshTraces() {
    const rows = await getJSON("/api/recent-traces?limit=25");
    const box = $("traces"); box.innerHTML = "";
    if (!rows || !rows.length) { box.appendChild(el("div", "empty", "no traced commands yet")); return; }
    $("traces-sub").textContent = rows.length + " recent";
    for (const r of rows) {
      const item = el("button", "trace-item");
      item.appendChild(el("span", "t-id", (r.trace_id || "").slice(0, 8)));
      item.appendChild(el("span", "t-src", r.source || ""));
      item.appendChild(el("span", "t-act", r.action || ""));
      item.appendChild(el("span", "t-lat", ms(r.latency_ms)));
      const ok = el("span", "t-ok " + (r.success ? "ok" : "fail"), r.success ? "✓" : "✗");
      item.appendChild(ok);
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
    const tl = el("div", "timeline");
    const t0 = res.timeline.length ? res.timeline[0].t : 0;
    for (const e of res.timeline) {
      const r = el("div", "tl-row tl-" + e.kind);
      r.appendChild(el("span", "tl-rel", "+" + Math.round((e.t - t0) * 1000) + "ms"));
      r.appendChild(el("span", "tl-kind", e.kind));
      r.appendChild(el("span", "tl-label", e.label || ""));
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
    tbl.innerHTML = "<thead><tr><th>session</th><th>cmds</th><th>ok</th><th>cloud</th><th>p95</th><th>pain</th></tr></thead>";
    const tb = el("tbody");
    for (const s of res.sessions.slice().reverse()) {
      const tr = el("tr");
      tr.innerHTML = `<td>${s.session_id}</td><td>${s.total_commands ?? "—"}</td>` +
        `<td>${pct(s.success_rate)}</td><td>${pct(s.cloud_escalation_rate)}</td>` +
        `<td>${ms(s.latency_p95_ms)}</td><td>${pct(s.pain_day_pct)}</td>`;
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

  // ── WebSocket (live feed) ───────────────────────────────────────────────────
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/chat`);
    ws.onopen = () => { $("conn-dot").classList.add("on"); $("conn-label").textContent = "live"; };
    ws.onclose = () => { $("conn-dot").classList.remove("on"); $("conn-label").textContent = "reconnecting…"; setTimeout(connect, 1500); };
    ws.onmessage = (e) => {
      let f; try { f = JSON.parse(e.data); } catch { return; }
      if (f.type === "dash_event") {
        pushFeed(f);
        if (f.kind === "command") refreshMetricsSoon();
      }
    };
  }
  let _mt = null;
  function refreshMetricsSoon() { clearTimeout(_mt); _mt = setTimeout(refreshMetrics, 400); }

  // ── boot ────────────────────────────────────────────────────────────────────
  function pollAll() { refreshMetrics(); refreshTraces(); refreshTrends(); refreshModels(); refreshRouting(); }
  pollAll();
  connect();
  setInterval(refreshMetrics, 3000);
  setInterval(() => { refreshTraces(); refreshTrends(); refreshModels(); refreshRouting(); }, 15000);
})();
