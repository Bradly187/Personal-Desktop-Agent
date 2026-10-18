import asyncio
import json
import logging
import re
import webbrowser
from typing import Any, Mapping, Optional, TYPE_CHECKING
from pathlib import Path

from core.action_lifecycle import ActionIdentity, ActionOutcome
from core.approval_authority import (
    action_digest, content_digest, step_action_digest,
)
from core.atomic_file import atomic_write_text
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


async def prepare_file_candidate(agent: "DevAgent", step: AgentStep) -> tuple[str, str]:
    """Authorize and resolve the exact bytes for a pending file action off-loop."""
    action = step.action.upper()
    target = (step.args or "").strip()
    decision = await asyncio.to_thread(
        agent._execution_scope.authorize_path, target, operation=action
    )
    if not decision.allowed or decision.canonical_path is None:
        raise PermissionError(decision.reason or f"{action} denied")
    target = str(decision.canonical_path)
    step.args = target
    edit_format = (
        SEARCH_REPLACE if action == "EDIT_FILE"
        else agent._router.edit_format_for(agent._active_plan_model)
    )
    new_text = await asyncio.to_thread(
        apply_edit, target, step.body, edit_format, agent._edit_applier
    )
    return target, new_text


def _taint_terminal_work(agent) -> None:
    """Monotonically downgrade the active plan after retrieved/external data."""
    from inference.sandbox import WorkTrust, combine_work_trust

    current = getattr(
        agent, "_active_work_trust", WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    )
    agent._active_work_trust = combine_work_trust(
        current, WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    )


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
        # Resolve the exact candidate before any authority check. Upfront plan
        # approval computed the same material without writing; execution repeats
        # it against current target state, so drift changes the digest and forces
        # a fresh per-operation approval. This candidate is the only text written.
        target, new_text = await prepare_file_candidate(agent, step)

        if agent._critic is None or not agent._critic_enabled:
            if not await confirm_destructive_op(
                agent,
                f"Approve writing file {target[:60]}?",
                card={"file_path": target},
                step=step,
                canonical_path=target,
                content=new_text,
            ):
                return f"{action} cancelled by user"
            step.comp_args = json.dumps(await asyncio.to_thread(
                agent._saga_manager.snapshot_for_write, target
            ))
            result = await asyncio.to_thread(write_file, target, new_text)
            return await agent._maybe_run_tester(step, result)

        # The Critic reviews the already-resolved candidate before authority is
        # claimed. It may block or force its own distinct exact-action request.
        verdict = await agent._critic_review(step, new_text)
        if verdict.decision in (REVISE, BLOCK):
            return agent._critic_reject_message(step, verdict)
        if not await confirm_destructive_op(
            agent, f"Approve writing file {target[:60]}?", force=verdict.escalate,
            card={"file_path": target,
                  "diff": await asyncio.to_thread(
                      diff_for_confirm, target, new_text)},
            step=step,
            canonical_path=target,
            content=new_text,
        ):
            return f"{action} cancelled by user"
        step.comp_args = json.dumps(await asyncio.to_thread(
            agent._saga_manager.snapshot_for_write, target
        ))
        result = await asyncio.to_thread(write_file, target, new_text)
        return await agent._maybe_run_tester(step, result)

    if action == "RUN_TERMINAL":
        cmd = step.args or step.body
        cwd_decision = await asyncio.to_thread(
            agent._execution_scope.authorize_cwd, None
        )
        if not cwd_decision.allowed or cwd_decision.canonical_path is None:
            raise PermissionError(cwd_decision.reason or "RUN_TERMINAL cwd denied")
        cwd = str(cwd_decision.canonical_path)
        if not await confirm_destructive_op(
            agent, f"Approve running command: {cmd.strip()[:60]}?",
            card={"command": cmd.strip()[:500]},
            step=step,
            canonical_cwd=cwd,
        ):
            return "RUN_TERMINAL cancelled by user"
        return await asyncio.to_thread(
            run_terminal,
            cmd,
            cwd=cwd,
            work_trust=getattr(agent, "_active_work_trust", None),
            containment_policy=getattr(agent, "_containment_policy", None),
        )

    if action == "EXPLAIN":
        # Return text to the caller; no desktop action
        return step.body or step.args

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

    async def authorized_git_cwd() -> str:
        decision = await asyncio.to_thread(
            agent._execution_scope.authorize_cwd, None
        )
        if not decision.allowed or decision.canonical_path is None:
            raise PermissionError(decision.reason or f"{action} cwd denied")
        return str(decision.canonical_path)

    if action == "GIT_STATUS":
        cwd = await authorized_git_cwd()
        return await asyncio.to_thread(git_status, cwd=cwd)

    if action == "GIT_DIFF":
        # args: optional "--staged" or a file path
        flags = (step.args or "").strip()
        cwd = await authorized_git_cwd()
        return await asyncio.to_thread(git_diff, flags, cwd=cwd)

    if action == "GIT_COMMIT":
        # args: commit message
        msg = (step.args or step.body or "").strip()
        if not msg:
            raise ValueError("GIT_COMMIT requires a commit message")
        cwd = await authorized_git_cwd()
        if not await confirm_destructive_op(
            agent, f"Approve git commit: {msg[:60]}?",
            card={"command": f"git commit -m {msg[:200]!r}"},
            step=step, canonical_cwd=cwd,
        ):
            return "GIT_COMMIT cancelled by user"
        return await asyncio.to_thread(git_commit, msg, cwd=cwd)

    if action == "GIT_CHECKOUT":
        # args: [-b] <branch>
        branch_args = (step.args or "").strip()
        cwd = await authorized_git_cwd()
        if not await confirm_destructive_op(
            agent, f"Approve git checkout {branch_args[:40]}?",
            card={"command": f"git checkout {branch_args[:200]}"},
            step=step, canonical_cwd=cwd,
        ):
            return "GIT_CHECKOUT cancelled by user"
        return await asyncio.to_thread(git_checkout, branch_args, cwd=cwd)

    # ── GitHub integration (roadmap item #3) ────────────────────────────

    if action == "GITHUB_PR":
        # args: title  body: PR description
        title = (step.args or "").strip()
        body = (step.body or "").strip()
        if not title:
            raise ValueError("GITHUB_PR requires a title in args")
        cwd = await authorized_git_cwd()
        if not await confirm_destructive_op(
            agent, f"Approve opening pull request: {title[:60]}?",
            step=step, canonical_cwd=cwd,
        ):
            return "GITHUB_PR cancelled by user"
        return await asyncio.to_thread(github_pr, title, body, cwd=cwd)

    # ── Web retrieval (roadmap item #3) ─────────────────────────────────

    if action == "FETCH_URL":
        url = (step.args or step.body or "").strip()
        if not url:
            raise ValueError("FETCH_URL requires a URL")
        result = await fetch_url(url)
        _taint_terminal_work(agent)
        return result

    if action in ("SKILL_QUERY", "SKILL_CALL"):
        result = await execute_skill_step(agent, step)
        _taint_terminal_work(agent)
        return result

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
        _taint_terminal_work(agent)
        return f"{_RAG_OPEN_FENCE}\n{body}\n{_RAG_CLOSE_FENCE}"

    # Fall through: accessibility verbs → CommandExecutor
    if agent._coordinator:
        from core.command_executor import Command
        action_cmd = Command(
            text=step.args,
            action=action,
            source="dev_agent",
            params=_parse_accessibility_params(action, step.args),
            trace_id=agent._active_trace_id,
            action_id=step.action_id,
            run_id=step.run_id,
            step_num=step.step_num,
        )
        executor = agent._coordinator._executor
        if hasattr(executor, "execute_with_registry"):
            result_dict = await executor.execute_with_registry(
                action_cmd, agent._action_registry
            )
        else:
            result_dict = await executor.execute(action_cmd)
        return ActionOutcome.from_dict(
            result_dict,
            identity=ActionIdentity(
                step.action_id,
                action,
                agent._active_trace_id,
                step.run_id,
                step.step_num,
            ),
        )

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
    byte_count = atomic_write_text(path, content)
    log.info("DevAgent: wrote %d bytes to %s", byte_count, path)
    return f"Written {byte_count} bytes to {path}"

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

async def confirm_destructive_op(
    agent,
    description: str,
    *,
    force: bool = False,
    card: Optional[dict] = None,
    step: AgentStep | None = None,
    canonical_path: str | None = None,
    canonical_cwd: str | None = None,
    content: str | bytes | None = None,
    approval_action: str | None = None,
    security_args: Mapping[str, Any] | None = None,
    exact_digest: str | None = None,
) -> bool:
    """Authorize one exact destructive action through the typed authority.

    Every responder receives the request nonce and digest and publishes an exact
    ``ApprovalResponse``.  Only ``ApprovalAuthority`` consumes that response and
    issues the one-use grant. Critic force keeps its distinct digest and request.
    """
    trace_id = agent._active_trace_id or ""
    goal = agent._current_goal or ""
    approval_context = "critic_force" if force else "operation"
    if exact_digest is not None:
        digest = exact_digest
    elif step is not None:
        digest = step_action_digest(
            step,
            trace_id=trace_id,
            goal=goal,
            canonical_path=canonical_path,
            canonical_cwd=canonical_cwd,
            content=content,
            approval_context=approval_context if force else "plan",
        )
    else:
        digest = action_digest(
            action=approval_action or "DESTRUCTIVE_OP",
            trace_id=trace_id,
            goal=goal,
            canonical_path=canonical_path,
            canonical_cwd=canonical_cwd,
            security_args=dict(security_args or {}),
            body_hash=content_digest(content or ""),
            step_identity={},
            approval_context=approval_context,
        )

    if not force and agent._approval_authority.claim_grant(
        agent._approval_grant, digest
    ):
        log.info("DevAgent._confirm: exact plan grant claimed — %s", description)
        return True

    async with agent._confirm_lock:
        try:
            request = agent._approval_authority.create_request(
                trace_id=trace_id,
                action_digest=digest,
                description=description,
            )
        except Exception as exc:
            log.info("DevAgent._confirm: authority unavailable (%s) — DENY", exc)
            return False
        try:
            await confirm_destructive_op_locked(
                agent, request, description, card=card
            )
        except Exception as exc:
            log.info("DevAgent._confirm: responder failed (%s) — DENY", exc)
        try:
            grant = agent._approval_authority.consume_response(
                request, approved_digests=(digest,)
            )
        except Exception as exc:
            log.info("DevAgent._confirm: authority failed (%s) — DENY", exc)
            return False
        if grant is None:
            return False
        return agent._approval_authority.claim_grant(grant, digest)



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

