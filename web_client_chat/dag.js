/* dag.js — render the live execution DAG into #dag using mermaid.
 *
 * Two graph kinds:
 *   - Plan DAG (dev queries): nodes = plan steps, edges = deps. Steps update
 *     running -> success/failed live.
 *   - Gate flow (simple commands): a fixed Gate0..Gate4 -> Action chain with the
 *     deciding gate highlighted.
 *
 * The agent's plan steps and gate labels are server-supplied; everything here is
 * presentation only.
 */
(function () {
  "use strict";

  let mermaidReady = false;
  let pending = null; // last graph definition requested before mermaid loaded
  let renderSeq = 0;

  window.addEventListener("mermaid-ready", function () {
    mermaidReady = true;
    if (pending) { draw(pending); pending = null; }
  });

  const el = () => document.getElementById("dag");
  const sub = (t) => { const s = document.getElementById("dag-sub"); if (s) s.textContent = t; };

  function setEmpty(text) {
    const d = el();
    if (d) d.innerHTML = '<div class="empty">' + (text || "idle") + "</div>";
  }

  async function draw(def) {
    if (!mermaidReady || !window.__mermaid) { pending = def; return; }
    const id = "dag-svg-" + (++renderSeq);
    try {
      const { svg } = await window.__mermaid.render(id, def);
      // Ignore a stale render that finished after a newer one was requested.
      if (id === "dag-svg-" + renderSeq) el().innerHTML = svg;
    } catch (e) {
      console.warn("mermaid render failed", e);
    }
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/[<>]/g, "")
      .replace(/"/g, "'")
      .replace(/\n/g, " ")
      .slice(0, 60);
  }

  // ── Plan DAG ──────────────────────────────────────────────────────────────
  // state.steps: [{n, action, args, deps}]; state.status: {n -> running|success|failed}
  const plan = { steps: [], status: {} };

  function buildMermaid(steps, status) {
    const lines = ["graph TD"];
    steps.forEach(function (s) {
      const label = s.n + ": " + esc(s.action) + (s.args ? "<br/>" + esc(s.args) : "");
      lines.push('  n' + s.n + '["' + label + '"]');
    });
    steps.forEach(function (s) {
      (s.deps || []).forEach(function (d) { lines.push("  n" + d + " --> n" + s.n); });
    });
    steps.forEach(function (s) {
      const st = status[s.n];
      if (st) lines.push("  class n" + s.n + " " + st + ";");
    });
    lines.push("classDef running fill:#3a2c0a,stroke:#f5a623,color:#fff,stroke-width:2px;");
    lines.push("classDef success fill:#0e2e1b,stroke:#2ecc71,color:#fff;");
    lines.push("classDef failed fill:#3a1414,stroke:#ff5c5c,color:#fff,stroke-width:2px;");
    return lines.join("\n");
  }

  function renderPlan() {
    if (!plan.steps.length) { setEmpty("waiting for a plan…"); return; }
    const done = Object.values(plan.status).filter((v) => v !== "running").length;
    sub(done + "/" + plan.steps.length + " steps");
    draw(buildMermaid(plan.steps, plan.status));
  }

  window.DAG = {
    reset: function () { plan.steps = []; plan.status = {}; setEmpty("idle"); sub("idle"); },
    setPlan: function (steps) {
      plan.steps = Array.isArray(steps) ? steps : [];
      plan.status = {};
      renderPlan();
    },
    setNode: function (n, st) { if (n != null) { plan.status[n] = st; renderPlan(); } },

    // ── Gate flow (simple commands) ─────────────────────────────────────────
    gateFlow: function (gate, action) {
      const gates = [
        { id: "G0", label: "Gate 0<br/>Privacy", key: ["0", "privacy"] },
        { id: "G1", label: "Gate 1<br/>Confidence", key: ["1", "conf"] },
        { id: "G2", label: "Gate 2<br/>Complexity", key: ["2", "complex"] },
        { id: "G3", label: "Gate 3<br/>VRAM", key: ["3", "vram"] },
        { id: "G4", label: "Gate 4<br/>Latency", key: ["4", "latency", "lat"] },
      ];
      const g = String(gate || "").toLowerCase();
      let hit = gates.find((x) => x.key.some((k) => g.includes(k)));
      const lines = ["graph LR"];
      gates.forEach((x) => lines.push('  ' + x.id + '["' + x.label + '"]'));
      lines.push('  ACT["' + esc(action || "action") + '"]');
      lines.push("  G0 --> G1 --> G2 --> G3 --> G4 --> ACT");
      // No gate matched (e.g. "all_pass") → the action node is the decision.
      const target = hit ? hit.id : "ACT";
      lines.push("  class " + target + " decide;");
      lines.push("classDef decide fill:#10314f,stroke:#0a84ff,color:#fff,stroke-width:2px;");
      sub(action ? esc(action) : "routed");
      draw(lines.join("\n"));
    },
  };

  setEmpty("idle");
})();
