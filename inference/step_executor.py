import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import webbrowser
from typing import Optional, TYPE_CHECKING
from pathlib import Path

from inference.plan_parser import AgentStep, AgentResult, _extract_json_obj
from core.approval_keywords import classify_confirmation
from core.events import TOPIC_DAG_APPROVAL
from inference.edit_format import EditApplier, render_hashline, HASHLINE, UDIFF, SEARCH_REPLACE
from inference.critic import BLOCK, PASS, REVISE, Critic, CriticVerdict, Finding
from inference.dev_common import (
    _RAG_OPEN_FENCE, _RAG_CLOSE_FENCE,
    _get_trust_classifier, _get_content_filter, _strip_html,
)

if TYPE_CHECKING:
    from inference.dev_agent import DevAgent
    from inference.model_router import ModelRouter
    from core.hybrid_coordinator import HybridCoordinator
    from inference.bridge_client import BridgeClient
    from storage.db import AgentDB

log = logging.getLogger(__name__)

class StepExecutor:
    # Safety limit: max lines of diff to include in a single prompt
    _CONFIRM_DIFF_MAX_LINES = 100

    def __init__(self, agent: "DevAgent", router: 'ModelRouter', coordinator: Optional['HybridCoordinator'], agent_db: Optional['AgentDB'] = None):
        # Wiring state (_bridge, _skill_registry, _personal_kb, _critic,
        # _critic_enabled, ...) lives on the agent — DevAgent's set_* methods
        # assign there and reads here delegate via __getattr__, so late wiring
        # after construction is always visible.
        self._agent = agent
        self._router = router
        self._coordinator = coordinator
        self._agent_db = agent_db
        self._edit_applier = EditApplier()
        self._repo_root = os.getcwd()
        self._confirm_lock = asyncio.Lock()

    def set_repo_root(self, path: str) -> None:
        self._repo_root = path

    async def _execute_step(self, step: AgentStep) -> str:
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

            if self._critic is None or not self._critic_enabled:
                # ── Legacy path (Critic OFF) — byte-identical to pre-feature ──
                # (The edit is applied AFTER approval here, so the chat card can
                # only carry the target path — no diff exists yet; R5.4.)
                if not await self._agent._confirm_destructive_op(
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
                    self._agent._apply_edit, step.args, step.body, fmt_override
                )
                # Snapshot BEFORE writing so a saga rollback restores an
                # overwritten file instead of deleting it. Captured even though
                # we're about to write — if the write fails, no compensation is
                # registered anyway.
                step.comp_args = json.dumps(await asyncio.to_thread(
                    self._agent._snapshot_for_write, step.args
                ))
                result = await asyncio.to_thread(self._agent._write_file, step.args, new_text)
                return await self._maybe_run_tester(step, result)

            # ── Critic-enabled path (specs/dev-agent-critic) ────────────────
            # Apply (lint gate) FIRST so the Critic reviews the actual resulting
            # text; an EditError still fails closed → replan (unchanged). The
            # Critic runs BEFORE the approval gate so it can escalate it.
            new_text = await asyncio.to_thread(
                self._agent._apply_edit, step.args, step.body, fmt_override
            )
            verdict = await self._critic_review(step, new_text)
            if verdict.decision in (REVISE, BLOCK):
                # No write, no snapshot/compensation — the diagnostic becomes the
                # step result the replan loop reacts to (R1.4, R1.6, R2.4).
                return self._critic_reject_message(step, verdict)
            # PASS: a non-pass-confidence verdict forces an explicit confirm even
            # for an upfront-authorized plan; it can never WEAKEN an existing gate
            # (R2.2, R2.3). new_text exists here, so the chat approval card can
            # show the exact pending diff (specs/chat-workbench-parity R5.1).
            if not await self._agent._confirm_destructive_op(
                f"Approve writing file {target[:60]}?", force=verdict.escalate,
                card={"file_path": target,
                      "diff": await asyncio.to_thread(
                          self._agent._diff_for_confirm, step.args, new_text)},
            ):
                return f"{action} cancelled by user"
            step.comp_args = json.dumps(await asyncio.to_thread(
                self._agent._snapshot_for_write, step.args
            ))
            result = await asyncio.to_thread(self._agent._write_file, step.args, new_text)
            return await self._maybe_run_tester(step, result)

        if action == "RUN_TERMINAL":
            cmd = step.args or step.body
            if not await self._agent._confirm_destructive_op(
                f"Approve running command: {cmd.strip()[:60]}?",
                card={"command": cmd.strip()[:500]},
            ):
                return "RUN_TERMINAL cancelled by user"
            return await asyncio.to_thread(self._run_terminal, cmd)

        if action == "EXPLAIN":
            # Return text to the caller; no desktop action
            return step.body or step.args

        if action == "DELEGATE":
            # Planner-driven read-only investigation sub-agent (Gap D). Always at
            # depth current+1; the child cannot reach a destructive verb (allowlist).
            question = (step.args or step.body or "").strip()
            return await self._delegate_investigate(question, self._delegate_depth + 1)

        if action == "SEARCH_WEB":
            query = step.args or step.body
            url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            await asyncio.to_thread(webbrowser.open, url)
            return f"Opened browser: {url}"

        if action == "READ_SCREEN":
            b64 = await self._capture_screenshot()
            if b64 and step.args:
                # Ask vision model the question in args
                r = await self._router.analyse_screen(b64, step.args)
                return r.text
            return "Screenshot captured"

        if action == "READ_FILE":
            path_str = step.args or step.body
            text = await asyncio.to_thread(self._read_file, path_str.strip())
            # When the plan model edits in hashline, anchor the view with
            # line:hash prefixes so its WRITE_FILE ops can reference them
            # (specs/edit-format-aci R4). Whole_file models see raw text.
            if self._router.edit_format_for(self._active_plan_model) == HASHLINE:
                text = render_hashline(text)
            return text

        if action == "GREP":
            # args format: "PATTERN [PATH]"  — path optional, defaults to project root
            parts = step.args.split(None, 1)
            pattern = parts[0] if parts else step.body
            search_path = parts[1].strip() if len(parts) > 1 else "."
            return await asyncio.to_thread(self._grep, pattern, search_path)

        # ── Git-native verbs (roadmap item #3) ──────────────────────────────

        if action == "GIT_STATUS":
            return await asyncio.to_thread(self._git_status)

        if action == "GIT_DIFF":
            # args: optional "--staged" or a file path
            flags = (step.args or "").strip()
            return await asyncio.to_thread(self._git_diff, flags)

        if action == "GIT_COMMIT":
            # args: commit message
            msg = (step.args or step.body or "").strip()
            if not msg:
                raise ValueError("GIT_COMMIT requires a commit message")
            if not await self._agent._confirm_destructive_op(
                f"Approve git commit: {msg[:60]}?",
                card={"command": f"git commit -m {msg[:200]!r}"},
            ):
                return "GIT_COMMIT cancelled by user"
            return await asyncio.to_thread(self._git_commit, msg)

        if action == "GIT_CHECKOUT":
            # args: [-b] <branch>
            branch_args = (step.args or "").strip()
            if not await self._agent._confirm_destructive_op(
                f"Approve git checkout {branch_args[:40]}?",
                card={"command": f"git checkout {branch_args[:200]}"},
            ):
                return "GIT_CHECKOUT cancelled by user"
            return await asyncio.to_thread(self._git_checkout, branch_args)

        # ── GitHub integration (roadmap item #3) ────────────────────────────

        if action == "GITHUB_PR":
            # args: title  body: PR description
            title = (step.args or "").strip()
            body = (step.body or "").strip()
            if not title:
                raise ValueError("GITHUB_PR requires a title in args")
            if not await self._agent._confirm_destructive_op(
                f"Approve opening pull request: {title[:60]}?"
            ):
                return "GITHUB_PR cancelled by user"
            return await asyncio.to_thread(self._github_pr, title, body)

        # ── Web retrieval (roadmap item #3) ─────────────────────────────────

        if action == "FETCH_URL":
            url = (step.args or step.body or "").strip()
            if not url:
                raise ValueError("FETCH_URL requires a URL")
            return await self._fetch_url(url)

        if action in ("SKILL_QUERY", "SKILL_CALL"):
            return await self._execute_skill_step(step)

        if action == "SEARCH_PERSONAL":
            # Read-only semantic search over the user's own documents. Results
            # are fenced as retrieved DATA (same convention as RAG context).
            if self._personal_kb is None or not getattr(self._personal_kb, "available", False):
                return "Personal knowledge base is not available"
            q = (step.args or step.body or "").strip()
            if not q:
                return "SEARCH_PERSONAL requires a query"
            hits = await self._personal_kb.query(q, n=4)
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
        if self._coordinator:
            from core.command_executor import Command
            action_cmd = Command(
                text=step.args,
                action=action,
                source="dev_agent",
                params=self._parse_accessibility_params(action, step.args),
            )
            result_dict = await self._coordinator._executor.execute(action_cmd)
            return json.dumps(result_dict)

        return f"No executor for action: {action}"

    @staticmethod
    def _parse_skill_args(raw: str, body: str = "") -> tuple[str, str, dict]:
        """Parse a SKILL step's args: `<skill_id> <tool> {json}` (json optional;
        may also arrive as the step body)."""
        parts = (raw or "").split(None, 2)
        skill_id = parts[0] if parts else ""
        tool = parts[1] if len(parts) > 1 else ""
        blob = parts[2] if len(parts) > 2 else (body or "")
        args: dict = {}
        if blob.strip():
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    args = parsed
            except (ValueError, TypeError):
                args = {}
        return skill_id, tool, args

    @staticmethod
    def _build_skill_args(text: str, match: dict, schema: dict) -> dict:
        """Heuristic NL→args for the single-turn path: a tool with exactly one
        required string param gets the utterance (minus the matched keyword); a
        no-arg tool gets {}. Complex multi-arg tools are best driven by the
        planner (which emits explicit JSON args)."""
        props = (schema or {}).get("properties", {})
        required = (schema or {}).get("required", [])
        payload = text
        kw = (match or {}).get("keyword")
        if kw:
            idx = text.lower().find(kw.lower())
            if idx >= 0:
                payload = (text[:idx] + text[idx + len(kw):]).strip()
        str_required = [p for p in required if props.get(p, {}).get("type") == "string"]
        if len(str_required) == 1:
            return {str_required[0]: payload or text}
        return {}

    async def _execute_skill_step(self, step: AgentStep) -> str:
        """Run a SKILL_QUERY/SKILL_CALL step through the registry with the full
        trust flow: outbound scrub + send-gate on SEND tools, inbound taint check
        on read results, and an audit record."""
        if self._skill_registry is None:
            return "No skills available (registry not wired)"

        skill_id, tool, args = self._parse_skill_args(step.args, step.body)
        if not skill_id or not tool:
            return "SKILL step needs '<skill_id> <tool> {json args}'"

        is_send = (step.action.upper() == "SKILL_CALL"
                   or self._skill_registry.is_send_tool(skill_id, tool))

        if is_send:
            # Outbound scrub: redact secrets/PII from the payload before egress.
            try:
                clean_blob, findings = _get_content_filter().scrub_sync(json.dumps(args))
                scrubbed = json.loads(clean_blob)
                if isinstance(scrubbed, dict):
                    args = scrubbed
                if findings:
                    log.info("Skill send: scrubbed %d secret(s) from %s.%s payload",
                             len(findings), skill_id, tool)
            except Exception as exc:
                log.debug("Skill send scrub failed (%s) — proceeding with raw args", exc)
            # Send-gate: fail-safe DENY voice confirmation (same gate as git verbs).
            if not await self._agent._confirm_destructive_op(
                f"Approve sending via skill {skill_id}.{tool}?"
            ):
                return f"SKILL_CALL {skill_id}.{tool} cancelled by user"

        try:
            result = await asyncio.wait_for(
                self._skill_registry.call(skill_id, tool, args),
                timeout=self.SKILL_CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning("Skill %s.%s timed out after %ds",
                        skill_id, tool, self.SKILL_CALL_TIMEOUT_S)
            return f"Skill {skill_id}.{tool} timed out after {self.SKILL_CALL_TIMEOUT_S}s"
        text = result.get("text", "") if isinstance(result, dict) else str(result)

        # Inbound taint: read results are untrusted external data — never let a
        # HIGH-risk (prompt-injection) payload reach a downstream prompt.
        if not is_send and text:
            try:
                verdict = _get_trust_classifier().classify_sync(
                    f"skill:{skill_id}.{tool}", text
                )
                if verdict.should_block:
                    log.warning("Skill %s.%s result quarantined (trust=HIGH)",
                                skill_id, tool)
                    await self._audit_skill(skill_id, tool, is_send, result, blocked=True)
                    return "[skill result withheld — flagged as potentially unsafe]"
            except Exception as exc:
                log.debug("Skill taint check failed: %s", exc)

        await self._audit_skill(skill_id, tool, is_send, result, blocked=False)
        if isinstance(result, dict) and result.get("status") == "error":
            return f"SKILL error: {result.get('error', 'unknown')}"
        return text or "(no output)"

    async def _audit_skill(self, skill_id: str, tool: str, is_send: bool,
                           result: dict, blocked: bool) -> None:
        if self._agent_db is None:
            return
        try:
            summary = ""
            if isinstance(result, dict):
                summary = (result.get("text") or result.get("error") or "")[:300]
            await self._agent_db.skills.log_skill_invocation(
                skill_id=skill_id, tool_name=tool, send=is_send,
                status=(result.get("status", "?") if isinstance(result, dict) else "?"),
                blocked=blocked, result_summary=summary,
            )
        except Exception as exc:
            log.debug("Skill audit write failed: %s", exc)

    async def _verify_math_with_cas(self, question: str, answer: str) -> str:
        """Verify a math answer against the SymPy CAS.

        Returns a one-line verdict block to append to the answer, or "" when
        nothing is CAS-checkable (proofs, conceptual answers) or the sympy skill
        is not loaded. Never raises — verification must never break the answer.
        """
        reg = self._skill_registry
        if reg is None or not reg.tool_schema("sympy", "verify"):
            return ""
        try:
            spec = await self._extract_cas_check(question, answer)
            if not spec or not spec.get("kind"):
                return ""
            args = {
                "kind": str(spec.get("kind", "")),
                "expression": str(spec.get("expression", "") or ""),
                "variable": str(spec.get("variable") or "x"),
                "claimed": str(spec.get("claimed") or ""),
                "lower": str(spec.get("lower") or ""),
                "upper": str(spec.get("upper") or ""),
            }
            if not args["expression"]:
                return ""
            step = AgentStep(action="SKILL_QUERY",
                             args=f"sympy verify {json.dumps(args)}")
            verdict = (await self._execute_skill_step(step) or "").strip()
            if not verdict or verdict.startswith("No CAS-checkable"):
                return ""
            return f"**SymPy verification:** {verdict}"
        except Exception as exc:
            log.debug("math CAS verification skipped: %s", exc)
            return ""

    async def _extract_cas_check(self, question: str, answer: str) -> dict:
        """Reduce a free-form math answer to one machine-checkable CAS spec via
        the LOCAL general model (no thinking trace, keeps it cheap/parseable)."""
        prompt = (
            "You convert a solved math problem into ONE machine-checkable SymPy "
            "verification. Output ONLY a JSON object, no other text.\n"
            "Keys:\n"
            '  "kind": one of "solve","integrate","differentiate","simplify",'
            '"factor","evaluate" — or null if the problem is a proof or '
            "conceptual answer with no single closed-form result to check.\n"
            '  "expression": the core expression or equation, SymPy-parseable '
            "(use ** for powers, * for multiplication; for solve include the "
            "full equation).\n"
            '  "variable": the main variable (default "x").\n'
            '  "claimed": the answer\'s final result as a SymPy-parseable '
            "expression (for solve: comma-separated roots; for a definite "
            "integral: the numeric value), or null if unclear.\n"
            '  "lower","upper": the integration bounds for a definite integral, '
            "else null.\n\n"
            f"Problem: {question}\n\nProposed answer:\n{answer[:1500]}"
        )
        r = await self._router.infer(domain="general", user_text=prompt)
        if not getattr(r, "ok", False) or not getattr(r, "text", ""):
            return {}
        return _extract_json_obj(r.text)

    async def _handle_skill(self, text: str) -> "AgentResult":
        """Single-turn skill path: resolve the intent, build args, execute, and
        (for reads) speak the result. Used when the classifier routes a short
        utterance to a registered skill intent."""
        t0 = time.monotonic()
        match = self._skill_registry.match_intent(text)
        if match.get("plan"):
            # The tool needs LLM-generated input (e.g. a diagram's Mermaid/SVG
            # source) — a direct call can't synthesise it. Route through the
            # planner, which generates the content and emits the skill step.
            return await self.plan_and_run(text)
        schema = self._skill_registry.tool_schema(match["skill_id"], match["tool"])
        args = self._build_skill_args(text, match, schema)
        step = AgentStep(
            action="SKILL_CALL" if match["send"] else "SKILL_QUERY",
            args=f"{match['skill_id']} {match['tool']} {json.dumps(args)}",
        )
        result_text = await self._execute_skill_step(step)

        # Optional on-device summarisation (e.g. "summarize my inbox"): the raw
        # result has ALREADY been taint-checked in _execute_skill_step, so a
        # quarantined ("withheld") payload is never summarised. Summarisation
        # stays local (domain="general").
        if (not match["send"] and match.get("summarize") and result_text
                and "withheld" not in result_text.lower()):
            try:
                r = await self._router.infer(
                    domain="general",
                    user_text=f"Summarize these items concisely for the user:\n\n{result_text}",
                )
                if getattr(r, "ok", False) and getattr(r, "text", ""):
                    result_text = r.text
            except Exception as exc:
                log.debug("Skill summarise failed: %s", exc)

        if not match["send"] and result_text:
            try:
                from tts.polly_stream import get_client as _get_tts
                asyncio.create_task(_get_tts().speak(result_text))
            except Exception as exc:
                log.debug("Skill TTS failed: %s", exc)

        result = AgentResult(
            goal=text,
            domain="skill",
            model_used=f"skill:{match['skill_id']}.{match['tool']}",
            response_text=result_text,
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._results_log.append(result)
        return result

    async def _handle_personal_query(self, text: str) -> "AgentResult":
        """Answer a question about the user's OWN documents from the PersonalKB.

        Retrieved chunks are fenced as DATA and synthesised by the LOCAL general
        model (never cloud — personal documents stay on-device). When nothing
        matches, says so honestly instead of falling through to a model that
        would hallucinate the user's notes.
        """
        t0 = time.monotonic()
        hits = await self._personal_kb.query(text, n=4)

        if not hits:
            answer = "I couldn't find anything about that in your documents."
            spoken = answer
            model_used = "personal_kb"
        else:
            lines = []
            fnames: list[str] = []
            for h in hits:
                fname = Path(h["file"]).name
                if fname not in fnames:
                    fnames.append(fname)
                lines.append(f"# {fname} — {h.get('name', '')}")
                lines.append((h.get("text") or "")[:600])
                lines.append("")
            context = f"{_RAG_OPEN_FENCE}\n" + "\n".join(lines) + f"\n{_RAG_CLOSE_FENCE}"
            model_used = "personal_kb"
            answer = context
            # Synthesis-failure fallback: the raw fenced excerpts stay in
            # response_text for on-screen display, but are NEVER spoken — the
            # default TTS is AWS Polly, and reading whole document chunks aloud
            # would both egress personal content and read the fence sentinel.
            spoken = (f"I found {len(hits)} matching passage"
                      f"{'s' if len(hits) != 1 else ''} in "
                      f"{', '.join(fnames[:3])}, but couldn't summarize them.")
            try:
                r = await self._router.infer(
                    domain="general",
                    user_text=(f"Answer the user's question using ONLY the retrieved "
                               f"excerpts from their personal documents below. Quote "
                               f"the source file names. Question: {text}\n\n{context}"),
                )
                if getattr(r, "ok", False) and getattr(r, "text", ""):
                    answer = r.text
                    spoken = r.text
                    model_used = r.model
            except Exception as exc:
                log.debug("PersonalKB synthesis failed (%s) — returning raw excerpts", exc)

        try:
            from tts.polly_stream import get_client as _get_tts
            asyncio.create_task(_get_tts().speak(spoken))
        except Exception as exc:
            log.debug("PersonalKB TTS failed: %s", exc)

        result = AgentResult(
            goal=text,
            domain="personal",
            model_used=model_used,
            response_text=answer,
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._results_log.append(result)
        return result

    def _apply_edit(
        self, path_str: str, body: str, edit_format: Optional[str] = None
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
        if edit_format is None:
            edit_format = self._router.edit_format_for(self._active_plan_model)
        path = Path(path_str.strip().strip("'\""))
        current = (
            path.read_text(encoding="utf-8", errors="replace")
            if path.exists() else ""
        )
        return self._edit_applier.apply(
            current, body, edit_format=edit_format, path=str(path)
        )

    @classmethod
    def _diff_for_confirm(cls, path_str: str, new_text: str) -> str:
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
            if len(lines) > cls._CONFIRM_DIFF_MAX_LINES:
                dropped = len(lines) - cls._CONFIRM_DIFF_MAX_LINES
                lines = lines[: cls._CONFIRM_DIFF_MAX_LINES]
                lines.append(f"… {dropped} more lines\n")
            return "".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.debug("DevAgent._diff_for_confirm failed: %s", exc)
            return ""

    @staticmethod
    def _write_file(path_str: str, content: str) -> str:
        path = Path(path_str.strip().strip("'\""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("DevAgent: wrote %d bytes to %s", len(content), path)
        return f"Written {len(content)} bytes to {path}"

    @staticmethod
    def _read_file(path_str: str, max_chars: int = 8000) -> str:
        """Read a file and return its contents (truncated to max_chars)."""
        path = Path(path_str.strip("'\""))
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n… [truncated at {max_chars} chars]"
        log.info("DevAgent: read %d chars from %s", len(text), path)
        return text

    @staticmethod
    def _grep(pattern: str, search_path: str, max_lines: int = 100) -> str:
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

    @staticmethod
    def _run_terminal(cmd: str) -> str:
        cmd = cmd.strip()
        log.info("DevAgent: running terminal command: %s", cmd)
        # Sandbox (mistake-containment): cwd-jail + resource/output caps. Network
        # is granted only for curated package/VCS/fetch ops (pip install, git
        # push, …); everything else stays offline. Those ops are themselves
        # approval-gated by the goal-session allowlist upstream.
        from inference.sandbox import run_sandboxed, command_needs_network
        # Slopsquatting guard (GAP-7): block a `pip install` of a package that
        # doesn't exist on PyPI (a hallucinated name is the supply-chain threat).
        # Fails open on a network error so offline dev isn't blocked.
        from core.goal_session import verify_pip_install
        ok, reason = verify_pip_install(cmd)
        if not ok:
            log.warning("DevAgent: blocked pip install — %s", reason)
            raise RuntimeError(reason)
        net = command_needs_network(cmd)
        result = run_sandboxed(cmd, timeout=60, allow_network=net)
        output = (result.stdout + result.stderr).strip()
        status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
        log.info("DevAgent: terminal %s%s%s → %s",
                 status, "" if result.sandboxed else " [unsandboxed]",
                 " [net]" if net else "", output[:120])
        if result.returncode != 0:
            raise RuntimeError(f"Command failed ({status}): {output[:200]}")
        return output or status

    async def _confirm_destructive_op(self, description: str, *, force: bool = False,
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
        if self._plan_authorized and not force:
            log.info("DevAgent._confirm: skipping (plan authorized) — %s", description)
            return True

        # Serialize: DAG waves may run two destructive steps concurrently;
        # overlapping TTS prompts + mic captures would garble both answers.
        async with self._confirm_lock:
            return await self._agent._confirm_destructive_op_locked(description, card=card)

    async def _confirm_destructive_op_locked(self, description: str,
                                             card: Optional[dict] = None) -> bool:
        import numpy as np

        log.info("DevAgent: confirmation required — %s", description)

        # Chat responder (specs/chat-workbench-parity R5): when a chat request is
        # in flight, surface the confirm as an in-chat approval card (with the
        # pending diff/command when the caller supplied one) and accept a yes/no
        # through the shared signal file — same protocol the plan gate already
        # uses; chat is just another responder. Non-chat runs are byte-identical.
        chat_live = self._event_bus is not None and bool(self._active_trace_id)
        if chat_live:
            payload = {"message": description, "destructive": True}
            if card:
                payload.update({k: v for k, v in card.items() if v})
            await self._publish_live(TOPIC_DAG_APPROVAL, payload)
        chat_window_s = getattr(self, "_chat_confirm_window_s", 7.0)

        # --- 1. Speak via TTS ------------------------------------------------
        tts_ok = True
        try:
            from tts.polly_stream import get_client as _get_tts
            _tts = _get_tts()
            await asyncio.to_thread(_tts.speak_sync, description)
        except Exception as exc:
            if not chat_live:
                log.info("DevAgent._confirm: TTS unavailable (%s) — DENY (fail-safe)", exc)
                return False
            # A chat card is on screen — an explicit click can still answer.
            # Timeout below still fails safe to DENY.
            log.info("DevAgent._confirm: TTS unavailable (%s) — chat card only", exc)
            tts_ok = False

        # --- 1b. Chat/signal-file window (chat requests only) -----------------
        # Mirrors _approve_plan_upfront: write `pending`, poll `response` for 7 s.
        # The response may come from the chat card click, the iPad, or the
        # WhisperStream approval gate. Explicit deny blocks; explicit yes grants;
        # ambiguity/timeout falls through to the mic capture below (which keeps
        # its own fail-safe DENY).
        if chat_live:
            _appr_dir = Path.home() / ".claude" / "approval"
            _pending = _appr_dir / "pending"
            _response = _appr_dir / "response"
            try:
                _appr_dir.mkdir(parents=True, exist_ok=True)
                _response.unlink(missing_ok=True)
                _pending.write_text(str(time.monotonic()), encoding="utf-8")
                deadline = time.monotonic() + chat_window_s
                transcript: Optional[str] = None
                while time.monotonic() < deadline:
                    if _response.exists():
                        transcript = _response.read_text(encoding="utf-8-sig").strip()
                        break
                    await asyncio.sleep(0.1)
            except OSError as exc:
                log.debug("DevAgent._confirm: signal-file window failed: %s", exc)
                transcript = None
            finally:
                _pending.unlink(missing_ok=True)
                _response.unlink(missing_ok=True)
            if transcript is not None:
                verdict = classify_confirmation(transcript)
                if verdict == "deny":
                    log.info("DevAgent._confirm: REJECTED via signal file — %r", transcript)
                    return False
                if verdict == "approve":
                    log.info("DevAgent._confirm: approved via signal file — %r", transcript)
                    return True
            if not tts_ok:
                # No spoken prompt was delivered, so a mic answer can't be an
                # informed one — and the chat window elapsed without an explicit
                # yes. Fail safe to DENY rather than transcribing ambient audio.
                log.info("DevAgent._confirm: no TTS and no chat answer → DENY (fail-safe)")
                return False

        # --- 2. Record 4 s of mic audio --------------------------------------
        try:
            import sounddevice as sd
            audio = await asyncio.to_thread(
                lambda: sd.rec(
                    int(4.0 * 16_000), samplerate=16_000,
                    channels=1, dtype="float32",
                ).flatten()
            )
            await asyncio.to_thread(sd.wait)
        except Exception as exc:
            log.info("DevAgent._confirm: mic unavailable (%s) — DENY (fail-safe)", exc)
            return False

        # --- 3. Check for voice activity; silence → DENY (fail-safe) ---------
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.005:
            log.info("DevAgent._confirm: silence → DENY (fail-safe)")
            return False

        # --- 4. Transcribe with tiny Whisper on CPU (no GPU contention) ------
        # Model is cached on self so the ~600ms load cost is paid once.
        try:
            if getattr(self._agent, "_confirm_whisper", None) is None:
                from faster_whisper import WhisperModel
                import typing
                self._agent._confirm_whisper = typing.cast(typing.Any, await asyncio.to_thread(
                    WhisperModel, "tiny", device="cpu", compute_type="int8"
                ))
            import typing
            model: typing.Any = self._agent._confirm_whisper

            def _transcribe() -> str:
                segs, _ = model.transcribe(
                    audio, language="en", beam_size=1, vad_filter=False
                )
                return " ".join(s.text for s in segs).lower().strip()

            text = await asyncio.to_thread(_transcribe)
            log.info("DevAgent._confirm: heard %r", text)
        except Exception as exc:
            log.info("DevAgent._confirm: transcription failed (%s) — DENY (fail-safe)", exc)
            return False

        # --- 5. Keyword detection (shared vocabulary, core/approval_keywords) -
        verdict = classify_confirmation(text)
        if verdict == "deny":
            log.info("DevAgent._confirm: REJECTED — %r", text)
            return False
        if verdict == "approve":
            log.info("DevAgent._confirm: approved — %r", text)
            return True

        # Ambiguous / unrecognised reply → DENY. This op is destructive, so the
        # only outcome that proceeds is an explicit "yes" (handled above).
        log.info("DevAgent._confirm: ambiguous response %r → DENY (fail-safe)", text)
        return False

    @staticmethod
    def _git_status() -> str:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        if result.returncode != 0:
            raise RuntimeError(f"git status failed: {result.stderr.strip()[:200]}")
        return out or "(nothing to commit, working tree clean)"

    @staticmethod
    def _git_diff(flags: str = "", max_chars: int = 8000) -> str:
        cmd = ["git", "diff"]
        if flags:
            cmd.extend(flags.split())
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr.strip()[:200]}")
        out = result.stdout.strip()
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n… [truncated at {max_chars} chars]"
        return out or "(no diff)"

    @staticmethod
    def _git_commit(message: str) -> str:
        # Stage all tracked changes then commit. Capture output and raise a
        # RuntimeError with stderr (not a raw CalledProcessError) so a staging
        # failure surfaces consistently with the commit path / saga (#30).
        add = subprocess.run(
            ["git", "add", "-u"], capture_output=True, text=True, timeout=10,
        )
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {add.stderr.strip()[:200]}")
        result = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr.strip()[:200]}")
        out = result.stdout.strip()
        log.info("DevAgent: git commit — %s", out[:100])
        return out

    @staticmethod
    def _git_checkout(branch_args: str) -> str:
        cmd = ["git", "checkout"] + branch_args.split()
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed: {result.stderr.strip()[:200]}")
        return result.stdout.strip() or result.stderr.strip() or "ok"

    @staticmethod
    def _github_pr(title: str, body: str) -> str:
        """Create a GitHub PR using the gh CLI and return the PR URL."""
        cmd = ["gh", "pr", "create", "--title", title]
        if body:
            cmd.extend(["--body", body])
        else:
            cmd.extend(["--body", "Created by Personal Desktop Agent via voice command."])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {result.stderr.strip()[:200]}")
        url = result.stdout.strip()
        log.info("DevAgent: PR created — %s", url)
        return url

    async def _fetch_url(self, url: str, max_chars: int = 4000) -> str:
        """Fetch a URL and return extracted text (replaces browser-open SEARCH_WEB).

        GAP-3 (Pillar 1 — Egress Governance): adversarial web content is untrusted
        input. Before it can enter the plan/reflect reasoning context the extracted
        text is screened with MCPTrustClassifier — HIGH-risk pages (injection
        payloads) are withheld, MEDIUM-risk pages are kept but flagged. The content
        is capped (default 4000 chars) to bound the injection surface.
        """
        from core.egress import EgressController, EgressError
        try:
            await EgressController.validate_url(url)
        except EgressError as e:
            raise RuntimeError(f"Egress policy violation: {e}")

        try:
            import aiohttp
        except ImportError:
            # Fall back to webbrowser open (old behaviour)
            await asyncio.to_thread(webbrowser.open, url)
            return f"Opened browser: {url}"

        headers = {"User-Agent": "Mozilla/5.0 (compatible; DesktopAgent/1.0)"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content_type = resp.content_type or ""
                    if "html" in content_type:
                        html = await resp.text(errors="replace")
                        text = _strip_html(html)
                    else:
                        text = await resp.text(errors="replace")
                    if len(text) > max_chars:
                        text = text[:max_chars] + f"\n… [truncated at {max_chars}]"
                    text = await self._scan_web_content(url, text)
                    log.info("DevAgent: fetched %s (%d chars)", url, len(text))
                    return text
        except Exception as exc:
            raise RuntimeError(f"FETCH_URL {url} failed: {exc}") from exc

    async def _scan_web_content(self, url: str, text: str) -> str:
        """Taint-screen fetched web content before it enters the reasoning context.

        HIGH-risk → withheld sentinel (the LLM never sees the payload).
        MEDIUM-risk → kept with a visible [TAINT] marker. Fails open on any
        classifier error so a transient fault never breaks a legitimate fetch.
        """
        try:
            verdict = await _get_trust_classifier().classify("fetch_url", text)
        except Exception as exc:  # noqa: BLE001 - fail open
            log.debug("DevAgent: web content scan failed (%s) — passing through", exc)
            return text
        if verdict.should_block:
            log.warning(
                "DevAgent: withheld HIGH-risk web content from %s [%s]",
                url, ", ".join(verdict.flags) or "?",
            )
            return ("[fetched content withheld — flagged as a possible prompt-"
                    "injection / unsafe payload]")
        if verdict.should_warn:
            return "[TAINT] " + text
        return text

    @staticmethod
    async def _capture_screenshot() -> Optional[str]:
        try:
            from mcp_server.tools import screen as _screen
            result = await asyncio.to_thread(_screen.screenshot)
            return result.get("image_base64")
        except Exception as exc:
            log.warning("DevAgent: screenshot failed: %s", exc)
            return None

    @staticmethod
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

    def __getattr__(self, name):
        agent = self.__dict__.get('_agent')
        if not agent:
            raise AttributeError(name)
        return getattr(agent, name)