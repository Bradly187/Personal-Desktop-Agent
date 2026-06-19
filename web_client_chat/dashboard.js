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
    const m = await getJSON("/api/metrics");
    const box = $("kpis");
    if (!m) { box.innerHTML = ""; box.appendChild(el("div", "empty", "metrics endpoint unavailable")); return; }
    const g = m.gauges || {}, c = m.counters || {}, h = (m.histograms || {}).latency_ms || {};
    box.innerHTML = "";
    box.appendChild(kpi("commands", String(c.commands_total ?? 0)));
    box.appendChild(kpi("success (1m)", pct(g.success_rate_1m)));
    box.appendChild(kpi("cloud (1m)", pct(g.cloud_rate_1m)));
    box.appendChild(kpi("latency p50", ms(h.p50)));
    box.appendChild(kpi("latency p95", ms(h.p95)));
    const pd = g.pain_day_score;
    const pdc = kpi("pain-day", fixed(pd)); if (pd != null && pd >= 0.6) pdc.classList.add("warn");
    box.appendChild(pdc);
    box.appendChild(kpi("VRAM free", g.vram_free_gb == null ? "—" : fixed(g.vram_free_gb, 1) + " GB"));
    if (m.uptime_s != null) $("now-sub").textContent = "uptime " + Math.round(m.uptime_s / 60) + "m";
  }

  // ── Activity: live feed (WS dash_event) ─────────────────────────────────────
  const FEED_MAX = 200;
  function pushFeed(ev) {
    const feed = $("feed");
    const empty = feed.querySelector(".empty"); if (empty) empty.remove();
    const row = el("div", "feed-row " + (ev.severity === "warn" ? "warn" : ""));
    row.appendChild(el("span", "feed-time", clock(ev.ts)));
    row.appendChild(el("span", "feed-kind", ev.kind || ""));
    let text = ev.text;
    if (ev.kind === "command") {
      const okc = ev.success ? "ok" : "fail";
      text = `${ev.action || "?"} · ${ev.route || "?"} · ${ms(ev.latency_ms)}`;
      row.classList.add(okc);
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

  // ── Cost (poll) ─────────────────────────────────────────────────────────────
  async function refreshCost() {
    const res = await getJSON("/api/cost?days=30");
    const box = $("cost"); box.innerHTML = "";
    if (!res) { box.appendChild(el("div", "empty", "cost endpoint unavailable")); return; }
    const t = res.totals || {};
    $("cost-sub").textContent = (res.days ? `last ${res.days}d` : "all time");
    box.appendChild(el("div", "cost-total", `$${(t.cost ?? 0).toFixed(4)} · ${res.n_cloud_inferences || 0} calls · ${(t.tokens_in||0).toLocaleString()}/${(t.tokens_out||0).toLocaleString()} tok`));
    const models = res.by_model || {};
    const keys = Object.keys(models).sort((a, b) => models[b].cost - models[a].cost);
    if (!keys.length) { box.appendChild(el("div", "empty", "no cloud spend recorded")); return; }
    const tbl = el("table", "tbl");
    tbl.innerHTML = "<thead><tr><th>model</th><th>calls</th><th>tok in</th><th>tok out</th><th>cost</th></tr></thead>";
    const tb = el("tbody");
    for (const k of keys) {
      const m = models[k];
      const tr = el("tr");
      tr.innerHTML = `<td>${k}</td><td>${m.calls}</td><td>${m.tokens_in.toLocaleString()}</td>` +
        `<td>${m.tokens_out.toLocaleString()}</td><td>$${m.cost.toFixed(4)}</td>`;
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
        pushFeed(f);
        if (f.kind === "command") refreshMetricsSoon();
      }
    };
  }
  let _mt = null;
  function refreshMetricsSoon() { clearTimeout(_mt); _mt = setTimeout(refreshMetrics, 400); }

  // ── boot ────────────────────────────────────────────────────────────────────
  function pollAll() { refreshMetrics(); refreshTraces(); refreshTrends(); refreshCost(); }
  pollAll();
  connect();
  setInterval(refreshMetrics, 3000);
  setInterval(() => { refreshTraces(); refreshTrends(); refreshCost(); }, 15000);
})();
