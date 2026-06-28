# Spec: Chat Active-Directory Switching + File-Context Attachments

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design + Tasks kept inline (§4–§6) until they outgrow the file.

---

## 1. Background — the "Why"

The desktop chat UI (`python main.py --chat` → `core/chat_server.py` :8770 +
`web_client_chat/`) today routes a bare `{type:"user_message", text}` to a
`Command(source="chat")` and nothing else. Two capabilities are missing that the
user (Brad) needs to drive real multi-project dev work from chat:

1. **Active-directory switching.** The DevAgent's writable scope
   (`CommandExecutor._writable_roots`) and its repo/RAG/git context
   (`DevAgent._repo_root`, default `os.getcwd()`) are fixed at process start. There
   is no way to point the agent at a *different* project (e.g. the clinic-scheduler
   roots in the workspace) without restarting. Brad works across several repos.

2. **File-context attachments.** A chat turn can carry only text. Brad wants to
   attach `.pdf`, `.png`, and `.svg` files so the agent can read a spec PDF, look at
   a screenshot, or reason about a diagram. The vision plumbing already exists
   (`DevAgent.handle(screenshot_b64=…)` → qwen3-vl) but the **chat path never feeds
   it**, and there is no PDF/SVG ingestion at all.

**Decisions (Brad, 2026-06-28):** directory switching uses **browse + confirm** — a
picker may choose ANY directory, but activating a not-yet-allowed root requires an
explicit confirmation that appends it to the session's `writable_roots` (preserving
the deny-by-default sandbox, AGENTS.md #7). Build **both** halves end-to-end + tests.

**Status:** In progress (2026-06-28). **Owner / author session:** Claude Code (Opus 4.8).
**Related:** `../accessibility-agent/` (DevAgent, CommandExecutor, HybridCoordinator),
`../resume-working-memory/` + `../repo-context-ingestion/` (the repo-context the
active-root re-scopes). Honors AGENTS.md #2 (extraction is off the 60 Hz path — chat
only), #6 (no new model — images ride the resident qwen3-vl), #7 (path boundaries —
browse-confirm appends to the allowlist, never bypasses `_path_in_scope`).

---

## 2. Glossary

- **Active root:** the directory the chat session currently targets — the default
  cwd for relative WRITE_FILE/RUN_TERMINAL paths AND the `DevAgent._repo_root` used
  for workspace/git/RAG context. Always a member of `writable_roots`.
- **Attachment:** an uploaded file (`.pdf` | `.png` | `.svg`) extracted to either
  **text** (pdf text, svg XML) injected as context, or an **image** (png, or svg
  rasterized) handed to the vision model as `screenshot_b64`.
- **`/upload`:** new HTTP multipart endpoint; stores a validated file under the
  active root's scratch dir and returns an `attachment_id`.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Switch the active directory (browse + confirm)

1. THE chat server SHALL track a per-session `active_root`, defaulting to the
   process cwd (today's behavior — byte-identical when the feature is unused).
2. WHEN the client sends `{type:"set_active_dir", path}` AND `path` is already in
   `writable_roots`, THE server SHALL activate it immediately: set the
   `CommandExecutor` active root (relative-path base) and `DevAgent._repo_root`,
   and reply `{type:"active_dir", path, confirmed:false}`.
3. WHEN `path` is NOT yet in `writable_roots`, THE server SHALL NOT activate it;
   it SHALL reply `{type:"active_dir_confirm", path}` and activate ONLY after a
   subsequent `{type:"set_active_dir", path, confirm:true}` — which appends `path`
   to the session `writable_roots` (AGENTS.md #7) and then activates as in R1.2.
4. THE server SHALL reject a non-existent / non-directory `path` with
   `{type:"error", error:…}` and SHALL NOT mutate any scope.
5. WHEN `{type:"list_dirs"}` is received, THE server SHALL return the current
   `writable_roots` + `active_root` so the UI can render the picker.

### Requirement 2: Attach files as context (.pdf, .png, .svg)

1. THE server SHALL expose `POST /upload` (multipart). It SHALL accept ONLY
   `.pdf` / `.png` / `.svg` (by extension AND sniffed content) up to a configured
   size cap (default 25 MB), store the file under `<active_root>/.chat_uploads/`,
   and return `{attachment_id, name, kind}`. Any other type/oversize → 4xx, no store.
2. THE stored path SHALL be inside the active root (AGENTS.md #7) — an upload that
   would escape the root (path traversal in the filename) SHALL be rejected.
3. WHEN a `user_message` carries `attachment_ids`, THE server SHALL extract each via
   `inference/attachments.extract_attachment` and thread the result into the
   `Command`: concatenated **text** extractions (pdf text, svg XML) become
   `params["attachment_context"]`; the FIRST **image** (png, or svg rasterized via
   PyMuPDF) becomes `params["attachment_image_b64"]`; names → `params["attachment_names"]`.
3a. `extract_attachment` SHALL be pure/deterministic, MUST NOT raise (returns an
    error-marker Attachment on any failure), and SHALL degrade: a `.svg` that can't
    rasterize falls back to its XML text; a `.pdf` with no extractable text returns
    an empty-text marker, never a crash.
4. THE `HybridCoordinator` dev pre-gate SHALL forward `attachment_context` (prepended
   to the planner/answer context, ahead of RAG) and `attachment_image_b64` (as
   `screenshot_b64`) into `DevAgent.handle(...)`. WHEN absent, `handle` SHALL be
   byte-identical to today (defaults: `""` / `None`).
5. Extraction SHALL load NO new model (AGENTS.md #6) and run only on the chat path
   (never the 60 Hz sensor loop, AGENTS.md #2). NO new `agent.db` schema (AGENTS.md
   #1) — attachments are transient files + in-memory context.

### Requirement 3: UI

1. THE composer SHALL show a directory control (current active root + a picker that
   lists `writable_roots` and accepts a typed/browsed path) and a paperclip control
   that uploads files and renders removable attachment chips.
2. A pending confirmation (R1.3) SHALL surface an inline confirm affordance; the UI
   SHALL NOT silently activate an unapproved root.
3. WHEN assets are absent the existing chat SHALL keep working (progressive
   enhancement — the new controls are additive).

---

## 4. Technical Design

- **New module `inference/attachments.py`** — pure extractor:
  ```python
  @dataclass
  class Attachment:
      name: str
      kind: str           # "text" | "image" | "error"
      text: str = ""      # for kind="text"
      image_b64: str = "" # for kind="image"
      error: str = ""
  def extract_attachment(path: str, *, max_text_chars: int = 20000) -> Attachment
  ```
  `.pdf` → `pypdf`/`pdfplumber` text (kind="text"); `.png` → base64 (kind="image");
  `.svg` → `fitz` (PyMuPDF) rasterize to PNG b64 (kind="image"), fallback raw XML
  (kind="text"). Never raises (R2.3a).
- **`core/command_executor.py`** — add `CommandExecutor.set_active_root(path)`
  (appends to `_writable_roots` if absent + records the active cwd base) and
  `add_writable_root(path)`. `Command` unchanged structurally — attachments ride the
  existing `params` dict.
- **`inference/dev_agent.py`** — `handle(text, screenshot_b64=None, trace_id="",
  attachment_context="")` (new optional arg, prepended to `extra_ctx`); add
  `set_repo_root(path)` (sets `self._repo_root`). Plan path already injects
  `extra_ctx`/`screenshot_b64`; the single-turn path too.
- **`core/hybrid_coordinator.py`** — in the dev pre-gate, read
  `cmd.params.get("attachment_context")` / `("attachment_image_b64")` and pass to
  `handle(...)`.
- **`core/chat_server.py`** — `set_active_dir` / `list_dirs` WS handlers, `/upload`
  HTTP handler, attachment extraction in `_run_request`, active-root setters wired to
  the coordinator's executor + dev agent.
- **`web_client_chat/{index.html,chat.js,style.css}`** — dir control + paperclip +
  chips + confirm affordance.
- **Persistence:** none (AGENTS.md #1). **VRAM:** none new (#6). **Cross-platform:**
  N/A (chat is PC-local; no iPad protocol change, AGENTS.md #3).

### Deferred (noted, not built)
- Cloud-DevAgent attachment path (the `route_cloud` branch) — v1 wires the LOCAL
  `handle()` only.
- Multi-image attachments (vision takes one image in v1; extras ride as text names).
- OCR of image-only PDFs (text-layer extraction only in v1).

---

## 5. Behavior Verification (executable)

- `tests/test_attachments.py` — `extract_attachment`: pdf→text, png→image_b64,
  svg→image_b64 (and XML fallback when raster unavailable), unknown ext→error
  Attachment, missing file→error (never raises), text clip at `max_text_chars`.
- `tests/test_chat_attachments_server.py` — `/upload` accepts png/pdf/svg + rejects
  `.exe`/oversize/traversal; `set_active_dir` immediate (in-allowlist) vs
  confirm-required (new root) vs reject (missing dir); `list_dirs` shape;
  `user_message` with `attachment_ids` builds a Command carrying
  `attachment_context`/`attachment_image_b64`.
- `tests/test_dev_agent_attachment_context.py` — `handle(attachment_context=…)`
  prepends to the planner context and is byte-identical when empty;
  `set_repo_root` re-points `_repo_root`.

---

## 6. Tasks

- [x] 1. `inference/attachments.py` + `tests/test_attachments.py` (10) — R2.3/2.3a.
- [x] 2. `CommandExecutor.set_active_root`/`add_writable_root`;
      `DevAgent.handle(attachment_context=…)` + `set_repo_root`; `plan_and_run`
      gains `extra_context` threaded into `_plan_and_run_locked` — R1.2/R2.4.
- [x] 3. `HybridCoordinator` dev pre-gate forwards `attachment_context`/
      `attachment_image_b64` into `handle()`; `set_active_directory`/
      `list_writable_roots` helpers (browse + confirm) — R2.4/R1.
- [x] 4. `chat_server`: `set_active_dir`/`list_dirs` WS + `POST /upload`
      (type/size/traversal guarded) + extraction in `_run_request` + active-root
      delegation — R1, R2.1/2.2/2.4.
- [x] 5. `web_client_chat` UI — dir control + picker + paperclip + chips + confirm
      affordance (progressive enhancement). Verified live via `chat_demo.py` stub
      coordinator: WS connects, 📁 picker populates roots, panel opens — R3.
- [x] 6. Tests: `tests/test_chat_attachments.py` (9) + `tests/test_chat_attachments_server.py`
      (9); `scripts/chat_demo.py` extended with the active-dir stub. 28 feature
      tests + 212 regression green. CLAUDE.md Known Gotchas updated.

**Status: built 2026-06-28** (local DevAgent path). Deferred per §4: cloud-DevAgent
attachment path, multi-image, image-only-PDF OCR.
