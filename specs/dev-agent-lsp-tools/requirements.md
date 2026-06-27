# Spec: DevAgent LSP Tools

---

## 1. Background — the "Why"

Currently, the DevAgent relies on text-based search (`grep`, `glob_files`) and semantic RAG indexing to navigate code. This is brittle for complex codebases with heavy aliasing and inheritance. State-of-the-art coding agents leverage the Language Server Protocol (LSP) for precise semantic code navigation. By wrapping a Python language server (e.g., Pyright) into MCP tools, we give the agent deterministic `Go-To-Definition` and `Find-References` capabilities, drastically reducing hallucinations when resolving symbols.

**Status:** Draft
**Owner / author session:** Antigravity

---

## 2. Glossary

- **LSPWrapper**: A process manager that spawns and communicates with the underlying language server (e.g., `pyright-langserver`) via standard input/output.
- **LSP MCP Tools**: The tools exposed by `desktop_mcp_server.py` that delegate requests to the `LSPWrapper`.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Semantic Symbol Definition
**User Story:** As the DevAgent, I want to find the exact file and line number where a symbol is defined, so that I can accurately navigate to its source.

#### Acceptance Criteria
1. THE `LSP MCP Tools` SHALL expose a `get_definition(file_path, line, character)` tool.
2. WHEN `get_definition` is called, THE `LSPWrapper` SHALL query the language server and return the absolute path and line/character range of the definition.
3. IF the symbol cannot be resolved, THEN THE `LSP MCP Tools` SHALL return a clear "Not found" response without crashing.

### Requirement 2: Semantic Symbol References
**User Story:** As the DevAgent, I want to find all usages of a symbol across the project, so that I can safely refactor or understand its impact.

#### Acceptance Criteria
1. THE `LSP MCP Tools` SHALL expose a `find_references(file_path, line, character)` tool.
2. WHEN `find_references` is called, THE `LSPWrapper` SHALL return a list of all file paths and line ranges where the symbol is used.

### Requirement 3: Server Lifecycle Management
**User Story:** As the MCP Server, I want to manage the LSP process so that it doesn't leak memory or hang indefinitely.

#### Acceptance Criteria
1. THE `LSPWrapper` SHALL lazily start the language server on the first tool invocation.
2. IF the language server process crashes, THEN THE `LSPWrapper` SHALL automatically restart it on the next request.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `mcp_server/desktop_mcp_server.py` and `mcp_server/tools/lsp.py`.
- **New `Command` fields (if any):** None.
- **Models / VRAM:** No new models required (uses standard Node-based `pyright` or Python-based `jedi-language-server`).
- **Persistence:** None.

### Configuration (flat YAML)

```yaml
dev_agent_lsp_tools:
  enabled: true
  server_command: ["pyright-langserver", "--stdio"]
```

---

## 5. Behavior Verification (executable, not prose)

- **Eval suite:** Add cases to `evals/suites/tools_lsp.jsonl` testing successful definition and reference lookups on known files.
- **Unit/integration tests:** `tests/test_lsp_tools.py` asserting correct JSON-RPC message passing and tool return schemas.

---

## 6. Tasks

- [ ] 1. Implement `LSPWrapper` to manage stdio JSON-RPC communication (R3.1, R3.2)
- [ ] 2. Implement `get_definition` MCP tool (R1.1, R1.2, R1.3)
- [ ] 3. Implement `find_references` MCP tool (R2.1, R2.2)
- [ ] 4. Add unit tests for wrapper and tools
- [ ] 5. Update `CLAUDE.md` Action Vocabulary and Key Files
