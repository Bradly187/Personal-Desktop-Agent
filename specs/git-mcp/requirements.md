# Spec: Dedicated VCS Tools (Git MCP)

---

## 1. Background — the "Why"

Currently, the DevAgent interacts with Git exclusively via the generic `RUN_TERMINAL` tool. This is error-prone: git output is formatted for humans (e.g., pagers, color codes, conflict markers), and the LLM frequently mishandles or hallucinates parsing this unstructured text. State-of-the-art coding agents expose native Version Control System (VCS) APIs to interact with Git programmatically. Creating dedicated Git MCP tools will drastically improve the agent's ability to safely branch, commit, and interpret repository state.

**Status:** Draft
**Owner / author session:** Antigravity

---

## 2. Glossary

- **VCS MCP Tools**: A suite of tools exposed by `desktop_mcp_server.py` that execute Git operations programmatically (via `GitPython` or structured `git` CLI wrappers).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Structured Branch Management
**User Story:** As the DevAgent, I want to create and switch branches programmatically, so that I can isolate my feature work safely.

#### Acceptance Criteria
1. THE `VCS MCP Tools` SHALL expose `git_create_branch(branch_name)` and `git_checkout(branch_name)` tools.
2. WHEN `git_create_branch` is called, THE system SHALL create the branch and return a structured JSON success/failure status.

### Requirement 2: Structured Commits
**User Story:** As the DevAgent, I want to commit my changes with structured metadata, so that I don't get stuck in vim/nano interactive prompts.

#### Acceptance Criteria
1. THE `VCS MCP Tools` SHALL expose a `git_commit(message, add_all=True)` tool.
2. WHEN `git_commit` is called, THE system SHALL stage changes and commit them, returning the new commit hash or an error if there's nothing to commit.

### Requirement 3: Structured Diffs
**User Story:** As the DevAgent, I want to read differences between branches or commits in a structured format, so that I can understand changes without parsing terminal pagers.

#### Acceptance Criteria
1. THE `VCS MCP Tools` SHALL expose a `git_diff(target)` tool.
2. WHEN `git_diff` is called, THE system SHALL return the patch as a raw string without pagination constraints.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `mcp_server/desktop_mcp_server.py` and `mcp_server/tools/vcs.py`.
- **New `Command` fields (if any):** None.
- **Models / VRAM:** No new models required.
- **Persistence:** Direct interaction with `.git/`.

### Configuration (flat YAML)

```yaml
vcs_mcp_tools:
  enabled: true
  auto_stage: true
```

---

## 5. Behavior Verification (executable, not prose)

- **Eval suite:** Add cases to `evals/suites/tools_vcs.jsonl` testing branch creation, modification, and commit success.
- **Unit/integration tests:** `tests/test_vcs_tools.py` executing against a temporary git repository fixture.

---

## 6. Tasks

- [ ] 1. Implement `vcs.py` tool wrappers (R1, R2, R3)
- [ ] 2. Ensure tools respect the `writable_roots` boundary
- [ ] 3. Add unit tests with a temporary git fixture
- [ ] 4. Register tools in `desktop_mcp_server.py`
- [ ] 5. Update `CLAUDE.md` Action Vocabulary
