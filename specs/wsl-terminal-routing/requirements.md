# Spec: WSL Terminal Routing — make the namespace jail apply on the Windows host

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks inline (§4–§6) until they outgrow the file.

---

## 1. Background — the "Why"

The agent's real host is **Windows native** (RTX 5090 PC). But the RUN_TERMINAL
sandbox — bubblewrap / firejail namespace jail in
[inference/sandbox.py](../../inference/sandbox.py) — is **Linux-only**. On Windows,
`sandbox_tool()` returns `None`, so every RUN_TERMINAL falls through to the
*unsandboxed* branch: it runs with `shell=True` in the project cwd, protected only
by the goal-session Bash allowlist + the `run_capped` timeout/tree-kill + (now,
from `../sandbox-interactive-hardening/`) stdin/non-interactive hardening. There is
**no filesystem jail and no network unshare** on the path the agent actually runs.

The allowlist is a strong *first* layer (deny-by-default, high-risk patterns
blocked, slopsquat-verified installs), but the "isolated execution" guarantee the
agentic-coding literature assumes — a command physically unable to touch files
outside the project or reach the network unless whitelisted — simply isn't present
on Windows. The gap analysis flagged this as the one real isolation miss.

**The fix chosen (decision: route through WSL by default):** WSL2 is already where
bwrap/firejail live. By executing RUN_TERMINAL commands *inside WSL2* (`wsl.exe -e
…`), the existing jail applies on the Windows host with no new sandbox technology —
we reuse `build_sandbox_argv` verbatim, just launched via WSL. This turns the
Windows-native "allowlist-only" path into "allowlist **+** namespace jail," closing
the gap with infrastructure that already ships on the box.

**The honest tension this spec must resolve:** RUN_TERMINAL today runs in the
*Windows* shell, so commands can be Windows-native (`.exe`, PowerShell, Windows
paths). Inside WSL they run in *Linux* bash with `/mnt/<drive>` paths. So routing
is **not** a blanket redirect — it needs (a) **path translation** of the project
dir / writable roots (`E:\…` ↔ `/mnt/e/…`), and (b) a **compatibility boundary**
that keeps genuinely Windows-only commands on the native path. Desktop control
(OPEN/CLICK/HOTKEY) is unaffected — those are separate verbs; only the dev-agent
shell verb moves. (Note `core/command_executor.py` already has a
`WSL_ACTION_PROXY` seam for the *reverse* direction — WSL→Windows GUI — so the
two-world topology is already part of the system.)

Rollout honors the repo convention: **ship behind a flag, default OFF**, validate
on the eval suite + real dev commands, **then** flip the default to ON (the
"by default" goal). Graceful degrade: if WSL or a jail tool is absent, fall back
to today's Windows-native path with the existing one-time WARNING — never block.

**Status:** Draft
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../sandbox-interactive-hardening/` (its stdin/env hardening applies
*inside* WSL too — land it first), `../dev-agent-critic/` (the Tester loop runs
through this path; isolation makes generated-test execution safer),
`../edit-format-aci/` (WRITE_FILE path/scoping unchanged). Honors AGENTS.md #4
(degrade-gracefully, never block), #7 (path boundaries — translation must NOT
widen `writable_roots`), #6 (no new model), #1 (no schema change).

---

## 2. Glossary

- **WSL routing**: executing a RUN_TERMINAL command inside WSL2 via `wsl.exe -e
  <jail-argv>` so bubblewrap/firejail apply, instead of the Windows-native
  `shell=True` fallback.
- **Path translation**: deterministic mapping between a Windows path and its WSL
  mount (`E:\proj` ↔ `/mnt/e/proj`), used to translate `project_dir` and the
  writable-root scope when crossing into WSL. Translation MUST preserve the scope
  boundary — it may never broaden what `_path_in_scope` would allow.
- **Compatibility boundary**: the classifier deciding whether a command is
  WSL-safe (POSIX dev tooling — `pytest`/`git`/`pip`/`npm`/`ls`/…) or must stay
  native (PowerShell cmdlets, `*.exe`, `where`, drive-letter-dependent invocations).
  Conservative: unknown → configurable (default: native, to avoid breaking a
  working command).
- **wsl_available()**: detection helper (`wsl.exe -l -q` succeeds AND a jail tool
  is present inside the chosen distro). Cached per process.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Route WSL-safe commands through the jail

**User Story:** As Brad, I want my agent's dev commands to run inside the WSL
namespace jail on my Windows PC, so a hallucinated command can't escape the
project dir or reach the network unless whitelisted.

#### Acceptance Criteria
1. WHILE WSL routing is enabled AND `wsl_available()` AND the command passes the
   compatibility boundary, THE sandbox SHALL execute it inside WSL via `wsl.exe`
   wrapping the existing `build_sandbox_argv` jail (bwrap/firejail), with
   `--unshare-net` unless `command_needs_network` (unchanged network policy).
2. THE `project_dir` (and any cwd) SHALL be path-translated to its `/mnt/<drive>`
   form for the WSL invocation, and the jail SHALL bind-mount **only** the
   translated project dir read-write (the rest read-only — same as the Linux path).
3. THE returned `SandboxResult.sandboxed` SHALL be `True` for a WSL-routed run, and
   the output-cap / timeout / tree-kill behavior SHALL be identical to the native
   `run_capped` contract.

### Requirement 2: Preserve scope and the native path for Windows-only commands

**User Story:** As Brad, I want Windows-specific commands to keep working, and I
never want WSL translation to widen what the agent can touch.

#### Acceptance Criteria
1. WHEN a command fails the compatibility boundary (Windows-only), THE sandbox
   SHALL run it on the existing Windows-native path unchanged (no WSL), retaining
   the allowlist + `run_capped` + interactive-hardening protections.
2. THE path translation SHALL NOT broaden scope: a path outside `writable_roots`
   on Windows SHALL remain out of scope after translation; `_path_in_scope`
   (`core/goal_session.py`) remains the authority and is evaluated on the Windows
   path BEFORE translation (AGENTS.md #7).
3. IF translation cannot map a path unambiguously (UNC path, non-drive root,
   network share), THEN THE sandbox SHALL refuse WSL routing for that command and
   fall back to native rather than guess (fail-safe, AGENTS.md #4).

### Requirement 3: Graceful detection and degrade

**User Story:** As Brad, I want this to silently do the right thing whether or not
WSL is set up, never blocking my command.

#### Acceptance Criteria
1. IF WSL routing is enabled but `wsl_available()` is false (no WSL, no distro, no
   in-distro jail tool), THEN THE sandbox SHALL fall back to the Windows-native
   path with the existing one-time WARNING and SHALL NOT block (AGENTS.md
   degrade-gracefully).
2. THE `wsl_available()` probe SHALL be cached per process and SHALL NOT run on the
   60 Hz path (it is invoked lazily from the async DevAgent execution path only,
   AGENTS.md #2).
3. WHEN WSL routing is disabled (default), THE behavior SHALL be byte-identical to
   today's Windows-native fallback.

### Requirement 4: Flag-gated rollout, eval-gated default flip

**User Story:** As Brad, I want this proven before it becomes the default, then
on by default — the stated goal.

#### Acceptance Criteria
1. THE feature SHALL ship behind config (`wsl_terminal_routing.enabled`),
   **default OFF**; the default flip to ON SHALL happen only after the eval suite
   + a real dev-command smoke pass with routing ON show no regression.
2. THE compatibility-boundary allow/deny lists SHALL be config-overridable so a
   command wrongly classified can be corrected without a code change.
3. A WSL-routed run and a native run of the same command SHALL be observable
   (logged with which path was taken + why) so the rollout can be audited.

---

## 4. Technical Design

> Touches `inference/sandbox.py` only (a new WSL branch + translation + detection)
> plus config plumbing. No `CommandExecutor` verb change, no `ipad_bridge`, no
> schema. Reuses `build_sandbox_argv`, `command_needs_network`, `run_capped`,
> and the goal-session scope check verbatim.

- **Entry point / pipeline boundary:** `run_sandboxed`
  ([inference/sandbox.py](../../inference/sandbox.py)). Add a WSL branch ahead of
  the current `if tool:` / `else:` so the decision order is: WSL-routed (if enabled
  + available + compatible + translatable) → Linux-native jail (when the process
  itself runs under Linux/WSL already) → Windows-native fallback.
- **New helpers (all in `inference/sandbox.py`):**
  - `wsl_available() -> bool` — cached `wsl.exe -l -q` + in-distro `which bwrap`.
  - `to_wsl_path(win_path) -> Optional[str]` — `E:\a\b` → `/mnt/e/a/b`; returns
    `None` for UNC / non-drive paths (drives R2.3 fallback).
  - `command_is_wsl_safe(command) -> bool` — the compatibility boundary
    (allow POSIX dev execs; deny `.exe`/PowerShell/`where`/etc.; config override).
  - `build_wsl_argv(jail_argv, distro) -> list[str]` — `["wsl.exe", "-d", distro,
    "-e", *jail_argv]`.
- **Reused, unchanged:** `build_sandbox_argv` (the jail argv that WSL launches),
  `command_needs_network` (network policy), `run_capped` (timeout/tree-kill, now
  with `stdin=DEVNULL` + non-interactive env from
  `../sandbox-interactive-hardening/` — applies inside WSL too), `_path_in_scope`
  (scope authority, evaluated on the Windows path first).
- **Models / VRAM / 60 Hz / schema / bridge:** none (AGENTS.md #1/#2/#3/#6 N/A) —
  except the `wsl_available` probe, which is cached and off the hot path (R3.2).
- **Security note (AGENTS.md #7):** translation is a *narrowing-preserving* map. The
  Windows-side `writable_roots` check runs BEFORE translation; the WSL jail then
  binds only the translated project dir. Net scope is the intersection, never a
  superset.

### Configuration (flat YAML)

```yaml
wsl_terminal_routing:
  enabled: false              # default OFF; flip to true after the eval gate
  distro: ""                  # "" → WSL default distro; else an explicit name
  unknown_command_policy: native   # native | wsl — where to send unclassified cmds
  compatibility:
    force_native:             # always run on Windows (never WSL)
      - powershell
      - pwsh
      - cmd
      - where
    # *.exe and drive-letter-anchored invocations are force-native implicitly.
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_wsl_routing.py` — pure helpers run on any OS:
  - R1.2/R2.2/R2.3: `to_wsl_path` maps `E:\proj\x` → `/mnt/e/proj/x`; returns
    `None` for `\\unc\share` and non-drive roots (→ native fallback).
  - R1.1: with routing enabled + mocked `wsl_available=True`, a WSL-safe command
    builds a `wsl.exe … bwrap …` argv binding only the translated project dir, with
    `--unshare-net` toggled by `command_needs_network`.
  - R2.1: a force-native command (`powershell …`, `foo.exe`) takes the native path
    even with routing enabled.
  - R3.1/R3.3: routing enabled but `wsl_available=False` → native fallback + WARNING;
    routing disabled → byte-identical to today's native path.
  - R2.2: a path outside `writable_roots` stays denied after translation (scope is
    not widened).
- **Eval/smoke gate (before default flip):** run the `dev_execution` suite with
  `wsl_terminal_routing.enabled=true` on a WSL-equipped box; assert parity with the
  native baseline on a real dev-command set (`pytest`, `git status`, `pip list`,
  `ls`), and confirm a hallucinated out-of-scope write is jailed. Lock the baseline;
  **do NOT flip the default until green** (see `running-the-eval-harness` skill).

Each criterion in §3 maps to at least one test/eval above.

---

## 6. Tasks

- [x] 1. `wsl_available()` (cached probe via `command -v bwrap` in-distro) +
      `to_wsl_path()` (drive→`/mnt`, None for UNC/non-drive) + `build_wsl_argv()` +
      `command_is_wsl_safe()` (POSIX-safe allowlist + force-native + `.exe`/drive
      deny + `unknown_command_policy`) with config-driven force-native list — R1.2,
      R2.3, R4.2.
- [x] 2. `_maybe_run_wsl` branch wired into `run_sandboxed` ahead of the native
      path (decision order: WSL → native), binding only the translated project dir
      via the existing `build_sandbox_argv("bwrap", …)`, network policy unchanged
      (`allow_network` honored); returns None → native fallback for disabled /
      non-Windows / force-native / untranslatable / WSL-absent — R1.1, R1.3, R2.1,
      R3.1.
- [x] 3. Scope guard: `_path_in_scope` already runs upstream on the Windows path
      (goal_session, unchanged); `to_wsl_path` is a 1:1 map that REFUSES
      untranslatable paths (UNC/non-drive → None → native), so translation can only
      narrow, never widen — R2.2. (Covered by `test_to_wsl_path_*`.)
- [x] 4. Config plumbing (`wsl_terminal_routing.*` in `~/.claude/ipad_bridge/
      config.json`, default OFF — no file → `{}` → disabled) + which-path-taken
      `log.info` (WSL vs native + reason) — R3.3, R4.1, R4.3.
- [x] 5. `tests/test_wsl_routing.py` — 34 tests: path translation, compat boundary,
      argv build, decision order (disabled/posix/unsafe/untranslatable/unavailable →
      native; safe → wsl+bwrap binding the translated dir, network honored), and
      run_sandboxed integration + default-disabled.
- [ ] 6. Eval/smoke with routing ON on a WSL box; lock baseline. **Gate the default
      flip** — R4.1. **(pending — needs a WSL-equipped box; the unit suite mocks
      `wsl.exe`.)**
- [x] 7. Docs: `CLAUDE.md` sandbox gotcha updated (Windows jails via WSL when
      enabled). **Merge note:** WS-1 (`feat/sandbox-interactive-hardening`) also
      edits `run_sandboxed`; when both land, thread WS-1's `stdin=DEVNULL` +
      non-interactive env into the `_maybe_run_wsl` `run_capped` call too (a small
      integration, not a conflict of intent). `docs/file-map.md` row pending.
