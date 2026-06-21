# Spec: Sandbox Interactive-Hang Hardening

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. A small, self-contained hardening of `inference/sandbox.py`.

---

## 1. Background — the "Why"

`RUN_TERMINAL` runs each command through `run_sandboxed`
([inference/sandbox.py](../../inference/sandbox.py)) → `run_capped`
([core/proc_utils.py:94](../../core/proc_utils.py)). Today the child inherits the
parent's stdin and gets no non-interactive environment. So a command that prompts
— `Do you want to continue? [Y/n]`, a git credential prompt, `apt` confirmation —
**blocks until the 60 s wall timeout**, then the whole process tree is killed.

The behavior is *bounded* (it can't hang "indefinitely" — the timeout + tree-kill
guarantee that), but it is **wasteful and opaque**: the agent burns its full
execution budget on a hang, then gets a generic timeout instead of a clean,
immediate failure it could react to. The agentic-coding literature flags this
exactly — "agents are notoriously bad at handling interactive terminal prompts,
which often causes the agent loop to hang." The fix is to harden at the source so
a prompt fails *fast* instead of *slow*.

This is unconditional **mistake-containment**, the same threat model as the rest
of `sandbox.py` (the LLM hallucinating a command, not an adversary) — so, like the
existing timeout / tree-kill / rlimits, it is not flag-gated; the existing
`DA_SANDBOX=0` escape hatch still disables the whole jail. Scope is deliberately
tiny: ~stdin + a curated env, no behavior change for commands that already ran
non-interactively.

**Status:** Draft
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../dev-agent-critic/` (its Tester loop runs generated tests through
this path and depends on prompts failing fast — R3.7 of that spec). Honors
AGENTS.md #4 (a stdin EOF surfaces as a normal step failure → `_replan`, never a
hang), #7 (path boundaries unchanged), #10 (function-granular: only
`run_sandboxed`/`run_capped` spawn shaping).

---

## 2. Glossary

- **run_sandboxed**: the `inference/sandbox.py` entry that wraps a command in
  bwrap/firejail (POSIX) or runs it directly (Windows/fallback), then spawns via
  `run_capped`.
- **run_capped**: the `core/proc_utils.py` timeout+tree-kill `subprocess.run`
  wrapper. Currently sets `stdout/stderr` PIPE on `capture_output`, but **does not
  set `stdin`**.
- **Non-interactive env**: a curated set of environment variables and flags that
  tell common tools not to prompt — `GIT_TERMINAL_PROMPT=0`,
  `DEBIAN_FRONTEND=noninteractive`, `PIP_DISABLE_PIP_VERSION_CHECK=1`,
  `PIP_NO_INPUT=1`, etc. Additive; never changes the user's command semantics.
- **Curated network ops**: the existing `_NETWORK_OPS` allowlist in
  `inference/sandbox.py` (pip/git/npm/…) — the only commands for which a `--yes`/
  `--no-input`-style flag may be injected, and only where the executable supports
  it.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Close stdin so prompts fail fast

**User Story:** As Brad, I want a command that asks for interactive input to fail
immediately instead of hanging for 60 seconds, so the agent gets a clean error and
moves on.

#### Acceptance Criteria
1. WHEN a command runs via `run_sandboxed` (sandboxed OR the unsandboxed
   fallback), THE child's stdin SHALL be `DEVNULL`, so an interactive read gets
   immediate EOF instead of blocking to the wall timeout.
2. WHEN a command that prompts receives EOF and exits non-zero, THE non-zero exit
   and its output SHALL be returned as the step result (caught by `_replan`),
   NEVER surfaced as a 60 s timeout.
3. THE stdin change SHALL be threaded through `run_capped` (add an explicit `stdin`
   parameter defaulting to `DEVNULL` for these spawn sites) without changing its
   timeout / tree-kill contract.

### Requirement 2: Curated non-interactive environment

**User Story:** As Brad, I want the common tools the agent uses to know not to
prompt, so they proceed non-interactively instead of waiting on a TTY.

#### Acceptance Criteria
1. WHEN spawning any command via `run_sandboxed`, THE environment SHALL include
   `GIT_TERMINAL_PROMPT=0`, `DEBIAN_FRONTEND=noninteractive`,
   `PIP_DISABLE_PIP_VERSION_CHECK=1`, and `PIP_NO_INPUT=1`, merged additively over
   the inherited env (never replacing the user's existing values).
2. WHERE a command matches a curated network op (`_NETWORK_OPS`) AND the executable
   supports a non-interactive flag, THE sandbox MAY inject it (e.g. `pip
   --no-input`, `apt-get -y`, `npm --yes`) — and SHALL NOT inject flags for any
   command outside that allowlist (no blind mutation of arbitrary commands).
3. THE injected env/flags SHALL be additive and idempotent — a command the user
   already wrote non-interactively SHALL behave identically (R3.1).

### Requirement 3: No regressions, fail-safe

**User Story:** As Brad, I want this hardening to never break a command that
already worked.

#### Acceptance Criteria
1. FOR ALL commands that previously completed without reading stdin, THE exit code,
   stdout, and stderr SHALL be unchanged by this feature.
2. IF a command legitimately requires stdin (rare for agent use), THEN its EOF
   failure SHALL surface as a normal non-zero step result for `_replan`, never a
   hang and never a crash of the agent loop (AGENTS.md #4).
3. THE existing `DA_SANDBOX=0` escape hatch and the bwrap/firejail/fallback
   branching SHALL be unchanged; this feature only shapes stdin + env at spawn.

---

## 4. Technical Design

> Touches `inference/sandbox.py` (env + stdin at the spawn site) and
> `core/proc_utils.py` (`run_capped` gains an explicit `stdin` param). Nothing
> else. No model, no schema, no bridge.

- **Entry point / pipeline boundary:** the `run_capped(...)` call inside
  `run_sandboxed` ([inference/sandbox.py](../../inference/sandbox.py)) for both the
  sandboxed (bwrap/firejail) and the fallback branches. Build the non-interactive
  env once (merge over `os.environ`/the passed env) and pass `stdin=DEVNULL`.
- **`run_capped` change:** add `stdin: int | None = subprocess.DEVNULL` (or a
  passthrough param) so the spawn sites opt into DEVNULL explicitly; default keeps
  the timeout/tree-kill contract identical (R1.3). Other callers
  (npm install, code-eval) inherit the safer default or pass through as needed.
- **Flag injection:** a tiny helper keyed off the existing `_NETWORK_OPS` map —
  only the allowlisted executables, only where the flag is known-safe; everything
  else is left byte-identical (R2.2).
- **Models / VRAM / 60 Hz / persistence / cross-platform:** none — pure spawn
  shaping (AGENTS.md #1/#2/#3/#6 all N/A).
- **Not flag-gated:** consistent with the module's existing unconditional
  mistake-containment (timeout, tree-kill, rlimits, output cap). `DA_SANDBOX=0`
  remains the single off-switch.

### Configuration (flat YAML)

```yaml
# No new config block — this is unconditional mistake-containment, like the
# existing 60 s timeout / tree-kill / rlimits. The only switch is the existing
# DA_SANDBOX env flag (default on; '0'/'false'/'off' disables the whole jail).
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** add to `tests/test_sandbox.py` (and
  `tests/test_proc_utils.py` if present):
  - R1.1/R1.2: a command that reads stdin (e.g. `python -c "input()"` /
    `read x`) returns a **prompt** non-zero exit **well under** the wall timeout —
    assert it does NOT raise `TimeoutExpired` and returns fast.
  - R1.3: `run_capped` with the new `stdin` param preserves the timeout/tree-kill
    behavior (existing tree-kill tests still pass).
  - R2.1: the spawned env contains the non-interactive vars; user-set values are
    not clobbered.
  - R2.2: flag injection fires only for `_NETWORK_OPS` executables; an arbitrary
    command is passed through byte-identical.
  - R3.1: a representative previously-passing command (`pytest -q`, `ls`) yields
    identical exit/stdout/stderr.
- No eval-suite change required (this is plumbing, not model behavior), but the
  existing `dev_execution` suite SHOULD stay green as a regression guard.

Each criterion in §3 maps to at least one test above.

---

## 6. Tasks

- [ ] 1. Add explicit `stdin` param to `run_capped` (default `DEVNULL`),
      preserving the timeout/tree-kill contract — satisfies R1.3.
- [ ] 2. In `run_sandboxed`, pass `stdin=DEVNULL` and build+merge the curated
      non-interactive env for both the sandboxed and fallback branches —
      satisfies R1.1, R2.1, R2.3.
- [ ] 3. Curated non-interactive flag injection keyed off `_NETWORK_OPS` (only
      allowlisted execs, only known-safe flags) — satisfies R2.2.
- [ ] 4. Tests in `tests/test_sandbox.py` (and proc_utils) per §5 — satisfies
      R1.1/R1.2, R2.1/R2.2, R3.1.
- [ ] 5. Docs: one line in the `CLAUDE.md` sandbox/Known-Gotchas note (stdin EOF +
      non-interactive env; prompts now fail fast, not at the timeout).
