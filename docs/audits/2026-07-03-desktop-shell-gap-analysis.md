# Desktop Shell Gap Analysis — PDA Electron Shell vs Commercial AI Desktop Apps

**Date:** 2026-07-03
**Subject:** `desktop_app/` Electron shell (shipped PR #160) vs commercial AI desktop apps / agentic IDEs
**Method:** Code + spec inventory of `desktop_app/` and `specs/desktop-app-shell/`; web research on Claude Code Desktop, Cursor 2.x, Windsurf, Warp, Google Antigravity, ChatGPT desktop, local-LLM shells (Open WebUI / AnythingLLM / LM Studio / Msty), and voice-coding accessibility tools (Talon, Serenade).
**Companion doc:** `2026-07-02-coding-agent-gap-analysis.md` covered the *agent*; this covers the *shell/UI*.

---

## 1. Framing: what the shell is competing with

The PDA shell is not an IDE — it is an **agent cockpit**: embedded chat + dashboard, file tree, Monaco spot-editor, one pty terminal, and backend lifecycle ownership. The correct commercial comparison set is therefore split into three tiers:

1. **Agent-first desktop apps** — Claude Code Desktop (Code tab), Warp ADE, Google Antigravity Manager view. Closest analogs.
2. **AI-native IDEs** — Cursor 2.x, Windsurf. Full editors with agents added; deeper than PDA needs to be.
3. **Local-LLM desktop shells** — Open WebUI, AnythingLLM, LM Studio, Msty. Local-first like PDA, but chat/RAG-only — none execute desktop actions.

Market context (2026): the AI coding-tool market crossed ~$7B ARR; Cursor >$500M ARR; Windsurf acquired by Cognition ($3B, 2025). Every tier-1/2 product converged during 2025–26 on the same four surfaces: **multi-session/parallel-agent management, visual diff review, embedded preview/browser, and background/scheduled automations.** That convergence is the yardstick below.

---

## 2. Competitor snapshots

### Claude Code Desktop (Anthropic) — closest analog
- Three tabs (Chat / Cowork / Code). April 2026 redesign: **multi-session sidebar** (filter by status/project/environment), drag-and-drop pane layout, integrated terminal, in-app file editor (spot edits, deliberately shallow), **rebuilt diff viewer** for large changesets, HTML/PDF/app-server preview pane, PR monitoring, side chat (Cmd+;) to branch questions without polluting main context.
- Sessions run with **git isolation** (worktrees); local, cloud, or SSH environments; **Dispatch** hands sessions to/from phone; **Routines** = saved automations on Anthropic cloud (research preview).
- Computer use built in (app-level permissions). Enterprise config, managed settings, auto-update, signed installers for macOS/Windows/Linux.
- Notably: its in-app editor is *also* minimal (no LSP story emphasized) — validates PDA's "spot edit" posture.

### Cursor 2.x (Anysphere)
- Agent-first redesign: **up to 8 parallel agents**, each in its own git worktree or remote machine; **multi-agent judging** (auto-evaluates parallel runs, recommends best with rationale).
- Plan Mode: plan with one model / build with another, parallel plans, inline Mermaid, send selected to-dos to new agents. Debug mode.
- **Native embedded browser**: agent tests its own work; element-level visual editing wired back to code.
- Full VS Code inheritance: LSP, extensions, debugger, git UI, command palette. $20/mo pro, usage-based on top.

### Windsurf (Cognition)
- Same feature family (Cascade agent, parallelism), differentiator is **plugin-first**: first-party plugins for JetBrains, Neovim, Sublime, Visual Studio, Xcode rather than requiring its own fork. Strong enterprise compliance (HIPAA/FedRAMP). $15→$20/mo.

### Warp (open-sourced April 2026, MIT/AGPL dual)
- Terminal-born ADE, Rust/GPU client. **Configurable UI density**: pure terminal → minimal agent view (diff + file tree) → full ADE. Multi-agent management, model routing incl. open-weight models (Kimi, Qwen, MiniMax), **bring-your-own CLI agent** (can host Claude Code/Codex/Gemini CLI inside it).
- **Oz** cloud agents: webhook/cron/Slack-triggered, containerized, fully audited. MCP first-class.
- Most relevant to PDA as an open-source reference implementation of "agent cockpit" UI patterns.

### Google Antigravity
- **Manager Surface**: spawn/observe up to 5 async agents across workspaces, each with its own model. **Artifacts** (task lists, plans, screenshots, browser recordings) as verifiable deliverables with inline feedback that doesn't stop execution. Scheduled tasks (cron). Multi-repo workspaces. Free tier drove adoption.
- PDA's dashboard + walkthrough cards are a partial artifact analog.

### ChatGPT desktop (OpenAI)
- Being merged into a "super app" (ChatGPT + browser + Codex). Companion window (Alt+Space), voice, canvas, in-chat third-party apps (Figma, Jira, Canva…). Consumer-facing; relevant mainly as the pattern for "OS-level companion" UX, not dev workflow.

### Local-LLM shells (Open WebUI / AnythingLLM / LM Studio / Msty)
- Local-first chat/RAG/document workspaces; Msty adds multi-model side-by-side and an agent beta. **None execute desktop actions or own an editor/terminal.** PDA's backend is strictly ahead of this tier in capability; their relevance is UI polish (settings UIs, model pickers, theming, installers) on local-first budgets.

### Accessibility voice-coding (Talon, Serenade)
- Talon: hands-free everything via phonetic grammar; steep configuration curve. Serenade: open-source voice-to-code, local processing, founded out of an RSI diagnosis.
- Both are **input layers only** — no agent, no planning, no execution beyond dictation. No commercial agentic dev tool has an accessibility-first mode.

---

## 3. Comparison matrix — agent cockpit surfaces

✅ strong · ⚠️ partial · ❌ absent

| Surface | PDA shell | Claude Code Desktop | Cursor 2.x | Warp | Antigravity |
|---|---|---|---|---|---|
| Embedded agent chat w/ approvals | ✅ (diff-carrying cards, plan preview, fail-safe deny) | ✅ | ✅ | ✅ | ✅ |
| Multi-session / parallel-agent UI | ❌ (single chat; backend WorkflowRunner unsurfaced) | ✅ sidebar + git isolation | ✅ 8 agents + judging | ✅ | ✅ Manager, 5 agents |
| Visual diff review | ⚠️ unified-text approval cards only | ✅ rebuilt diff viewer | ✅ | ✅ | ✅ |
| Editor | ⚠️ Monaco spot-edit (no LSP/find-in-files) | ⚠️ spot-edit by design | ✅ full VS Code | ⚠️ minimal | ✅ VS Code base |
| File tree | ⚠️ whole-FS, lazy, no git decoration/context menu/watch | ✅ | ✅ | ✅ | ✅ |
| Terminal | ⚠️ single pty, PowerShell only | ✅ integrated | ✅ | ✅ (is one) | ✅ |
| App/HTML preview | ❌ images only | ✅ HTML/PDF/app servers | ✅ native browser | ⚠️ | ✅ artifacts + recordings |
| Command palette / fuzzy open | ❌ | ✅ | ✅ | ✅ | ✅ |
| OS notifications (agent needs attention) | ❌ (TTS only) | ✅ + PR monitoring | ✅ | ✅ | ✅ |
| Background/scheduled automations UI | ❌ (goal queue exists, unsurfaced) | ✅ Routines | ⚠️ background agents | ✅ Oz cron/webhooks | ✅ scheduled tasks |
| Settings/preferences UI | ❌ localStorage only | ✅ | ✅ | ✅ | ✅ |
| Packaging / auto-update | ❌ npm start (explicit non-goal) | ✅ | ✅ | ✅ | ✅ |
| **Local-first inference** | ✅ **fully local** | ❌ | ❌ | ⚠️ open-weight via routing | ❌ |
| **Backend lifecycle ownership (attach/own/kill)** | ✅ | n/a (self-contained) | n/a | n/a | n/a |
| **Accessibility-first design** | ✅ RA hit targets, keyboard splitters, voice/tilt/touch pipeline | ❌ | ❌ | ❌ | ❌ |
| **Undo/rewind of agent runs** | ✅ saga checkpoints + voice rewind | ⚠️ per-session git isolation | ✅ checkpoints | ⚠️ | ⚠️ |
| Observability (traces, cost, replay) | ✅ embedded dashboard | ⚠️ usage view | ⚠️ | ✅ Oz audit logs | ✅ artifacts |

---

## 4. Gap register (SG-1 … SG-12)

Ordered by (impact for this user) × (cost, favoring activation of existing backend capability over new builds — same principle as the 2026-07-02 coding-agent analysis).

| # | Gap | Commercial norm | PDA state | Cost | Priority |
|---|---|---|---|---|---|
| SG-1 | **Multi-session / parallel-run surface** | Universal (sidebar/manager) | Backend has WorkflowRunner (pipeline shipped), goal queue, per-trace turns; shell shows one chat | Medium — UI over existing data | **HIGH** |
| SG-2 | **Side-by-side diff viewer** | Universal, heavily invested | Unified-text diff in approval cards only; Monaco diff editor already vendored, unwired | **Low** | **HIGH** |
| SG-3 | **Command palette + fuzzy file open (Ctrl+P/Ctrl+Shift+P)** | Universal | Absent; tree is mouse-first | Low-Medium | **HIGH** (RA-weighted: keyboard beats pointing) |
| SG-4 | **OS toast notifications** — approval pending, run finished, backend down | Universal | TTS only; silent if muted/window buried. Electron `Notification` is trivial | **Low** | **HIGH** |
| SG-5 | **HTML/app preview tab** | Claude Code (HTML/PDF/servers), Cursor (browser) | Images only; backend already has `preview_*` browser tooling (PR #136) | Medium | MEDIUM |
| SG-6 | **Git surfacing** — tree status decorations, branch in status bar, commit/diff view | Universal | None in shell (backend has GIT_* verbs) | Low (decorations) → Medium (commit UI) | MEDIUM |
| SG-7 | **Find**: editor find/replace + find-in-files | Universal | Monaco's built-in find widget may already work (Ctrl+F — verify, likely free); find-in-files absent though backend has GREP | Low | MEDIUM |
| SG-8 | **Automations surface** (Routines/Oz/Scheduled-tasks analog) | Emerged as table stakes in 2026 | Proactivity + goal queue exist; no shell UI | Medium | MEDIUM |
| SG-9 | **Multiple terminals + shell choice** | Universal | One PowerShell pty, respawn loop | Low-Medium | LOW-MED |
| SG-10 | **Settings UI** (font size, theme, terminal shell) | Universal | Hardcoded; zoom partially compensates. Font-size control is an accessibility item | Low | LOW-MED |
| SG-11 | **Session file watch / tree auto-refresh** | Universal | Manual expand only; stale after agent writes files | Low | LOW-MED |
| SG-12 | **Packaging / auto-update / installer** | Universal | `npm start`; explicit v1 non-goal, single user, watchdog covers launch | Medium | LOW (defer) |

Not gaps, but noted asymmetries:
- **Mobile handoff**: Claude Code has Dispatch-from-phone; PDA's iPad *is* its remote surface (sensor hub + touch). Different design, roughly equivalent coverage for this user.
- **LSP/debugger/extensions**: full-IDE territory. Claude Code Desktop also declines this — validates keeping the editor shallow and letting VS Code/the agent do heavy editing. Recommend explicitly writing this into the spec as a permanent non-goal rather than "deferred."

---

## 5. Where PDA is ahead of every commercial product

1. **Fully local inference with desktop execution.** Tier-1/2 products are cloud-model-first; tier-3 local shells can't act. Nothing commercial combines local models + desktop control + an agent cockpit.
2. **Accessibility-first agentic computing.** RA-friendly hit targets (28px rows, 20px splitter zones), keyboard-resizable panels, and a voice/tilt/touch/gesture command pipeline. Talon/Serenade stop at input; Cursor/Warp assume able-bodied keyboard intensity. **This intersection — accessible agentic development — has zero commercial occupants.**
3. **Safety-gated autonomy**: two-gate approval, voice confirmation with fail-safe DENY, diff-carrying approval cards, saga rollback + voice rewind. Commercial checkpoint systems (Cursor) are comparable but none have a voice-gated, deny-by-default posture.
4. **Backend lifecycle ownership** with attach/own/starting/down state machine — no commercial analog because none manage an external inference stack.
5. **Embedded observability** (traces, cost ledger, replay, live DAG) exceeds what Cursor/Claude Code expose in-app.

---

## 6. Intersection & convergence analysis

**Accessibility × agentic dev environment — white space.** Assistive tools are observation/input-only; ADEs are accessibility-blind. Academic signals point the same way: 2025–26 HCI work ("Terminal Is All You Need", arXiv 2603.10664; "Design Principles for Human-Agent Interaction", arXiv 2606.20630) emphasizes transparency and *low barriers to entry* as open problems, and agentic tools' convergence on dense text-terminal interaction is actively hostile to motor-impaired users. PDA is a working existence proof at this intersection — directly relevant to the AIOS/grad-school research framing (`project/research_vision.md`).

**Convergence signals to watch:**
- Warp's open-sourcing makes tier-1 cockpit UI patterns (session manager, diff-first minimal view) free to study/borrow.
- Claude Code Routines / Antigravity scheduled tasks / Warp Oz: "agent automations with a UI" became table stakes within ~6 months — PDA's goal queue is the substrate, only the surface is missing.
- MCP is the cross-domain protocol everywhere; PDA already speaks it.

**Crowded zones (don't compete):** full-IDE depth (LSP, debuggers, extensions), cloud parallel-agent fleets, model marketplaces, enterprise compliance.

---

## 7. Recommendations (ranked by leverage)

1. **SG-2 Monaco diff editor** — vendored dependency, unwired. Render approval diffs and file diffs side-by-side. Days, not weeks; largest review-quality jump per unit effort.
2. **SG-4 OS toasts** — Electron `Notification` for approval-pending / run-complete / backend-down. Trivial; complements TTS when muted.
3. **SG-3 Command palette + fuzzy opener** — highest accessibility ROI; reduces pointer dependence across the whole shell.
4. **SG-1 Session list** — surface WorkflowRunner runs + goal queue + recent traces in a sidebar; data already in agent.db/dashboard. This is the single biggest "looks like 2026" gap.
5. **SG-6 (lite) + SG-11** — git status decorations from `git status --porcelain` + fs.watch tree refresh. Cheap freshness wins.
6. **SG-5 HTML preview tab** — reuse `preview_*` plumbing when agent work is web-shaped.
7. Declare **LSP/extensions/debugger and packaging (SG-12)** permanent non-goals in `specs/desktop-app-shell/requirements.md` to keep scope honest.

---

## 8. Sources

- Claude Code Desktop docs — https://code.claude.com/docs/en/desktop — accessed 2026-07-03
- Claude Code desktop redesign — https://claude.com/blog/claude-code-desktop-redesign — 2026-07-03
- VentureBeat on redesign + Routines — https://venturebeat.com/orchestration/we-tested-anthropics-redesigned-claude-code-desktop-app-and-routines-heres-what-enterprises-should-know — 2026-07-03
- Cursor 2.0 changelog/blog — https://cursor.com/changelog/2-0, https://cursor.com/blog/2-0, https://cursor.com/changelog/2-2 — 2026-07-03
- Warp open-source announcement — https://www.warp.dev/newsroom/2026/4/28/warp-open-sources-its-agentic-development-environment; repo https://github.com/warpdotdev/warp — 2026-07-03
- Google Antigravity — https://developers.googleblog.com/build-with-google-antigravity-our-new-agentic-development-platform/ — 2026-07-03
- Windsurf vs Cursor 2026 comparisons — https://windsurf.com/compare/windsurf-vs-cursor; https://tech-insider.org/windsurf-vs-cursor-2026/ — 2026-07-03
- JetBrains Jan 2026 dev survey adoption figures — via https://agentmarketcap.ai/blog/2026/04/08/ai-ide-acquisition-wave-windsurf-google-cursor-sourcegraph — 2026-07-03
- OpenAI desktop super app — https://www.cnbc.com/2026/03/19/openai-desktop-super-app-chatgpt-browser-codex.html — 2026-07-03
- Local platform comparisons — https://modelpiper.com/blog/local-ai-platforms-compared-mac; https://www.iunera.com/kraken/enterprise-ai/top-20-tools-to-run-llms-locally-in-2026-ollama-anythingllm-open-webui-lm-studio-vllm-and-every-real-alternative-compared/ — 2026-07-03
- Talon/Serenade accessibility — https://willowvoice.com/blog/voice-to-text-tools-developers-coding; https://github.com/trillium/awesome-talon — 2026-07-03
- [academic] Terminal Is All You Need — https://arxiv.org/html/2603.10664v1 — 2026-07-03
- [academic] Design Principles for Human-Agent Interaction — https://arxiv.org/html/2606.20630v1 — 2026-07-03
