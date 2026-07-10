import asyncio
import json
import logging
import re
import webbrowser
from typing import Optional, TYPE_CHECKING
from pathlib import Path

from inference.executors.git_executor import git_status, git_diff, git_commit, git_checkout, github_pr
from inference.executors.terminal_executor import run_terminal
from inference.executors.web_executor import fetch_url, capture_screenshot
from inference.executors.skill_executor import execute_skill_step
from inference.executors.voice_approval_gate import confirm_destructive_op_locked

from inference.plan_parser import AgentStep
from inference.edit_format import render_hashline, HASHLINE, SEARCH_REPLACE
from inference.critic import BLOCK, REVISE
from inference.dev_common import (
    _RAG_OPEN_FENCE, _RAG_CLOSE_FENCE,
    _get_trust_classifier,
)

if TYPE_CHECKING:
    from inference.dev_agent import DevAgent

log = logging.getLogger(__name__)

_CONFIRM_DIFF_MAX_LINES = 400   # spec: chat-workbench-parity R5.1

async def execute_step(agent: "DevAgent", step: AgentStep) -> str:
    action = step.action.upper()

    if action in ("WRITE_FILE", "EDIT_FILE"):
        # Destructive: gated like the git verbs. _confirm_destructive_op
        # short-circuits to approve when the whole plan was explicitly
        # authorized upfront, so an approved plan stays prompt-free.
        # EDIT_FILE forces the SEARCH_REPLACE format (surgical block edit)
        # regardless of the model's per-model WRITE_FILE knob; WRITE_FILE
        # uses the configured format (whole_file / hashline). Both share the
        # same lint gate, Critic, snapshot, and tester path below.
        target = (step.args or "").strip()
        fmt_override = SEARCH_REPLACE if action == "EDIT_FILE" else None

        if agent._critic is None or not agent._critic_enabled:
            # ── Legacy path (Critic OFF) — byte-identical to pre-feature ──
            # (The edit is applied AFTER approval here, so the chat card can
            # only carry the target path - no diff exists yet; R5.4.)
            if not await confirm_destructive_op(
                agent,
                f"Approve writing file {target[:60]}?",
                card={"file_path": target},
            ):
                return f"{action} cancelled by user"
            # Lint-gate + format-aware apply BEFORE snapshot/write so a
            # syntactically broken (or non-matching) edit fails closed (file
            # untouched) and the loop replans with a diagnostic
            # (specs/edit-format-aci R1, R2, R5). An EditError raised here
            # marks the step failed (both verbs are non-retryable) → replan;
            # nothing is snapshotted or written.
            new_text = await asyncio.to_thread(
                apply_edit, step.args, step.body, fmt_override or agent._router.edit_format_for(agent._active_plan_model), agent._edit_applier
            )
            # Snapshot BEFORE writing so a saga rollback restores an
            # overwritten file instead of deleting it. Captured even though
            # we're about to write — if the write fails, no compensation is
            # registered anyway.
            step.comp_args = json.dumps(await asyncio.to_thread(
                agent._saga_manager._snapshot_for_write, step.args
            ))
            result = await asyncio.to_thread(write_file, step.args, new_text)
            return await agent._maybe_run_tester(step, result)

        # ── Critic-enabled path (specs/dev-agent-critic) ────────────────
        # Apply (lint gate) FIRST so the Critic reviews the actual resulting
        # text; an EditError still fails closed → replan (unchanged). The
        # Critic runs BEFORE the approval gate so it can escalate it.
        new_text = await asyncio.to_thread(
            apply_edit, step.args, step.body, fmt_override or agent._router.edit_format_for(agent._active_plan_model), agent._edit_applier
        )
        verdict = await agent._critic_review(step, new_text)
        if verdict.decision in (REVISE, BLOCK):
            # No write, no snapshot/compensation — the diagnostic becomes the
            # step result the replan loop reacts to (R1.4, R1.6, R2.4).
            return agent._critic_reject_message(step, verdict)
        # PASS: a non-pass-confidence verdict forces an explicit confirm even
        # for an upfront-authorized plan; it can never WEAKEN an existing gate
        # (R2.2, R2.3). new_text exists here, so the chat approval card can
        # show the exact pending diff (specs/chat-workbench-parity R5.1).
        if not await confirm_destructive_op(
            agent, f"Approve writing file {target[:60]}?", force=verdict.escalate,
            card={"file_path": target,
                  "diff": await asyncio.to_thread(
                      diff_for_confirm, step.args, new_text)},
        ):
            return f"{action} cancelled by user"
        step.comp_args = json.dumps(await asyncio.to_thread(
            agent._saga_manager._snapshot_for_write, step.args
        ))
        result = await asyncio.to_thread(write_file, step.args, new_text)
        return await agent._maybe_run_tester(step, result)

    if action == "RUN_TERMINAL":
        cmd = step.args or step.body
        if not await confirm_destructive_op(
            agent, f"Approve running command: {cmd.strip()[:60]}?",
            card={"command": cmd.strip()[:500]},
        ):
            return "RUN_TERMINAL cancelled by user"
        return await asyncio.to_thread(run_terminal, cmd)

    if action == "EXPLAIN":
        # Return text to the caller; no desktop action
        return step.body or step.args

    if action == "DELEGATE":
        # Planner-driven read-only investigation sub-agent (Gap D). Always at
        # depth current+1; the child cannot reach a destructive verb (allowlist).
        question = (step.args or step.body or "").strip()
        return await agent._delegate_investigate(question, agent._delegate_depth + 1)

    if action == "SEARCH_WEB":
        query = step.args or step.body
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        await asyncio.to_thread(webbrowser.open, url)
        return f"Opened browser: {url}"

    if action == "READ_SCREEN":
        b64 = await capture_screenshot()
        if b64 and step.args:
            # Ask vision model the question in args
            r = await agent._router.analyse_screen(b64, step.args)
            return r.text
        return "Screenshot captured"

    if action == "READ_FILE":
        path_str = step.args or step.body
        text = await asyncio.to_thread(read_file, path_str.strip())
        # When the plan model edits in hashline, anchor the view with
        # line:hash prefixes so its WRITE_FILE ops can reference them
        # (specs/edit-format-aci R4). Whole_file models see raw text.
        if agent._router.edit_format_for(agent._active_plan_model) == HASHLINE:
            text = render_hashline(text)
        return text

    if action == "GREP":
        # args format: "PATTERN [PATH]"  — path optional, defaults to project root
        parts = step.args.split(None, 1)
        pattern = parts[0] if parts else step.body
        search_path = parts[1].strip() if len(parts) > 1 else "."
        return await asyncio.to_thread(grep, pattern, search_path)

    # ── Git-native verbs (roadmap item #3) ──────────────────────────────

    if action == "GIT_STATUS":
        return await asyncio.to_thread(git_status)

    if action == "GIT_DIFF":
        # args: optional "--staged" or a file path
        flags = (step.args or "").strip()
        return await asyncio.to_thread(git_diff, flags)

    if action == "GIT_COMMIT":
        # args: commit message
        msg = (step.args or step.body or "").strip()
        if not msg:
            raise ValueError("GIT_COMMIT requires a commit message")
        if not await confirm_destructive_op(
            agent, f"Approve git commit: {msg[:60]}?",
            card={"command": f"git commit -m {msg[:200]!r}"},
        ):
            return "GIT_COMMIT cancelled by user"
        return await asyncio.to_thread(git_commit, msg)

    if action == "GIT_CHECKOUT":
        # args: [-b] <branch>
        branch_args = (step.args or "").strip()
        if not await confirm_destructive_op(
            agent, f"Approve git checkout {branch_args[:40]}?",
            card={"command": f"git checkout {branch_args[:200]}"},
        ):
            return "GIT_CHECKOUT cancelled by user"
        return await asyncio.to_thread(git_checkout, branch_args)

    # ── GitHub integration (roadmap item #3) ────────────────────────────

    if action == "GITHUB_PR":
        # args: title  body: PR description
        title = (step.args or "").strip()
        body = (step.body or "").strip()
        if not title:
            raise ValueError("GITHUB_PR requires a title in args")
        if not await confirm_destructive_op(
            agent, f"Approve opening pull request: {title[:60]}?"
        ):
            return "GITHUB_PR cancelled by user"
        return await asyncio.to_thread(github_pr, title, body)

    # ── Web retrieval (roadmap item #3) ─────────────────────────────────

    if action == "FETCH_URL":
        url = (step.args or step.body or "").strip()
        if not url:
            raise ValueError("FETCH_URL requires a URL")
        return await fetch_url(url)

    if action in ("SKILL_QUERY", "SKILL_CALL"):
        return await execute_skill_step(agent, step)

    if action == "SEARCH_PERSONAL":
        # Read-only semantic search over the user's own documents. Results
        # are fenced as retrieved DATA (same convention as RAG context).
        if agent._personal_kb is None or not getattr(agent._personal_kb, "available", False):
            return "Personal knowledge base is not available"
        q = (step.args or step.body or "").strip()
        if not q:
            return "SEARCH_PERSONAL requires a query"
        hits = await agent._personal_kb.query(q, n=4)
        if not hits:
            return "No matches in the personal knowledge base"
        lines = []
        for h in hits:
            lines.append(f"# {h['file']} — {h.get('name', '')} (score={h.get('score', 0):.2f})")
            lines.append((h.get("text") or "")[:600])
            lines.append("")
        body = "\n".join(lines)
        # Defense-depth parity with remote RAG: plan-loop observations steer
        # replanning, and ~/Documents has weaker provenance than the repo
        # (downloaded PDFs, web clippings). HIGH-risk injection → withhold.
        try:
            verdict = _get_trust_classifier().classify_sync("personal_kb", body)
            if verdict.should_block:
                log.warning("SEARCH_PERSONAL result withheld (trust=HIGH)")
                return "[personal search result withheld — flagged as potentially unsafe]"
        except Exception as exc:
            log.debug("SEARCH_PERSONAL taint check failed: %s", exc)
        return f"{_RAG_OPEN_FENCE}\n{body}\n{_RAG_CLOSE_FENCE}"

    # Fall through: accessibility verbs → CommandExecutor
    if agent._coordinator:
        from core.command_executor import Command
        action_cmd = Command(
            text=step.args,
            action=action,
            source="dev_agent",
            params=_parse_accessibility_params(action, step.args),
        )
        result_dict = await agent._coordinator._executor.execute(action_cmd)
        return json.dumps(result_dict)

    return f"No executor for action: {action}"

def apply_edit(
    path_str: str, body: str, edit_format: str, applier
) -> str:
    """Resolve a WRITE_FILE/EDIT_FILE payload to its final file text, lint-gated.

    Reads the current file (if it exists) and runs the payload through the
    EditApplier. ``edit_format`` defaults to the format configured for the
    model that produced the plan (WRITE_FILE; specs/edit-format-aci R3); the
    EDIT_FILE verb passes ``SEARCH_REPLACE`` explicitly to force surgical
    block edits regardless of the per-model WRITE_FILE knob. Raises
    ``EditError`` if the result fails validation — the caller never writes
    on failure (R1). Returns the text to write on success.
    """
    path = Path(path_str.strip().strip("'\""))
    current = (
        path.read_text(encoding="utf-8", errors="replace")
        if path.exists() else ""
    )
    return applier.apply(
        current, body, edit_format=edit_format, path=str(path)
    )

def diff_for_confirm(path_str: str, new_text: str) -> str:
    """Unified diff of a pending WRITE_FILE/EDIT_FILE for the chat approval
    card (specs/chat-workbench-parity R5.1). Presentation only — computed
    from the same resolved path `_apply_edit`/`_write_file` use; truncated;
    never raises (an unreadable file degrades to a message-only card)."""
    import difflib
    try:
        path = Path(path_str.strip().strip("'\""))
        current = (
            path.read_text(encoding="utf-8", errors="replace")
            if path.exists() else ""
        )
        lines = list(difflib.unified_diff(
            current.splitlines(keepends=True),
            (new_text or "").splitlines(keepends=True),
            fromfile=f"a/{path.name}", tofile=f"b/{path.name}",
        ))
        if len(lines) > _CONFIRM_DIFF_MAX_LINES:
            dropped = len(lines) - _CONFIRM_DIFF_MAX_LINES
            lines = lines[: _CONFIRM_DIFF_MAX_LINES]
            lines.append(f"… {dropped} more lines\n")
        return "".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.debug("DevAgent._diff_for_confirm failed: %s", exc)
        return ""

def write_file(path_str: str, content: str) -> str:
    path = Path(path_str.strip().strip("'\""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("DevAgent: wrote %d bytes to %s", len(content), path)
    return f"Written {len(content)} bytes to {path}"

def read_file(path_str: str, max_chars: int = 8000) -> str:
    """Read a file and return its contents (truncated to max_chars)."""
    path = Path(path_str.strip("'\""))
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… [truncated at {max_chars} chars]"
    log.info("DevAgent: read %d chars from %s", len(text), path)
    return text

def grep(pattern: str, search_path: str, max_lines: int = 100) -> str:
    """Search for a regex pattern in files under search_path.

    Returns matching lines as a string (file:line: content format). Delegates
    to the shared ``mcp_server.tools.search`` implementation that also backs
    the first-class ``grep`` MCP tool, so the verb and the tool never drift.
    ``scopes=None`` preserves this in-process verb's repo-wide read (the MCP
    tool passes the writable-root allowlist instead).
    """
    from mcp_server.tools import search as _search
    result = _search.search_text(pattern, search_path, max_lines, scopes=None)
    return _search.format_grep_result(result, pattern, search_path, max_lines)

async def confirm_destructive_op(agent, description: str, *, force: bool = False,
                                  card: Optional[dict] = None) -> bool:
    """Speak the action description and wait for voice confirmation.

    This op is destructive by definition, so it fails SAFE to DENY: only an
    explicit spoken "yes" (or a prior whole-plan authorization) proceeds.
    Silence, an ambiguous reply, or unavailable TTS/microphone all return
    False — the op is skipped rather than run without clear consent. Mirrors
    the hardened voice approval gate (approval_hook.py, timeout→reject).

    ``force`` bypasses the upfront-plan-authorization short-circuit so the
    Critic can ESCALATE a risky edit to an explicit confirm (specs/dev-agent-
    critic R2.2). It only ever ADDS friction — it can never weaken a gate.

    ``card`` (specs/chat-workbench-parity R5) is optional extra context for
    the in-chat approval card — {file_path, diff} for a pending write or
    {command} for a terminal/git op. Only used when a chat request is in
    flight; the yes/no authority is unchanged (signal file / voice).
    """
    # If the user already approved the entire plan upfront, skip per-op
    # confirmation — unless a caller (the Critic) forces an explicit confirm.
    if agent._plan_authorized and not force:
        log.info("DevAgent._confirm: skipping (plan authorized) — %s", description)
        return True

    # Serialize: DAG waves may run two destructive steps concurrently;
    # overlapping TTS prompts + mic captures would garble both answers.
    async with agent._confirm_lock:
        return await confirm_destructive_op_locked(agent, description, card=card)



def _parse_accessibility_params(action: str, args: str) -> dict:
    params: dict = {}
    if action == "SCROLL":
        words = args.lower().split()
        for w in words:
            if w in ("up", "down", "left", "right"):
                params["direction"] = w
                break
        for w in words:
            try:
                params["amount"] = int(w)
                break
            except ValueError:
                pass
    elif action in ("TYPE", "DICTATE"):
        params["text"] = args
    elif action == "OPEN":
        params["target"] = args
    elif action == "HOTKEY":
        keys = [k.strip() for k in re.split(r"[+\s]+", args) if k.strip()]
        params["keys"] = keys
    return params

