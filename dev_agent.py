"""DevAgent — agentic task loop for software development and research tasks.

Implements a plan → execute → observe → reflect loop that lets the agent
autonomously complete multi-step development tasks using specialist models
and an expanded action vocabulary.

Expanded action verbs (beyond the 9 accessibility verbs):
  WRITE_FILE <path>   — write content to a file
  RUN_TERMINAL <cmd>  — execute a shell command, capture output
  EXPLAIN <text>      — return a text response to the user (no desktop action)
  SEARCH_WEB <query>  — open browser with search query
  READ_SCREEN         — take screenshot, optionally pass to vision model

Entry points:
  DevAgent.handle(text)    — classify, route, execute; single-turn or agentic
  DevAgent.plan_and_run(goal)  — full plan→execute→reflect loop

The DevAgent sits above HybridCoordinator. Simple accessibility commands
(domain="command") are passed straight through to the existing pipeline.
All dev-domain queries are handled by specialist models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from domain_classifier import DomainClassifier
from model_router import ModelRouter, RouterResult

if TYPE_CHECKING:
    from codebase_indexer import CodebaseIndexer
    from command_executor import Command, CommandExecutor
    from continuous_trainer import ContinuousTrainer
    from db import AgentDB
    from hybrid_coordinator import HybridCoordinator
    from kiro_client import KiroClient
    from mcp_server.tools import screen as screen_tools

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML text extraction helper
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Very simple HTML → plain text: strip tags, collapse whitespace."""
    # Remove script/style blocks entirely
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags
    clean = re.sub(r"<[^>]+>", " ", clean)
    # Collapse whitespace
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()

# ---------------------------------------------------------------------------
# Step model
# ---------------------------------------------------------------------------

# Action verbs the planner model is allowed to emit
_PLAN_ACTIONS = {
    "WRITE_FILE", "RUN_TERMINAL", "CLICK", "OPEN", "HOTKEY",
    "EXPLAIN", "SEARCH_WEB", "READ_SCREEN", "READ_FILE", "GREP",
    "SCROLL", "TYPE",
    # Git-native verbs (item #3 / #8 in roadmap)
    "GIT_STATUS", "GIT_DIFF", "GIT_COMMIT", "GIT_CHECKOUT",
    # GitHub integration
    "GITHUB_PR",
    # Web retrieval (replaces browser-open SEARCH_WEB for context injection)
    "FETCH_URL",
}

_STEP_PATTERN = re.compile(
    r"^\s*(?:Step\s*\d+[:.]\s*)?"          # optional "Step N:"
    r"\[?"                                   # optional [
    r"(WRITE_FILE|RUN_TERMINAL|CLICK|OPEN|HOTKEY|EXPLAIN|SEARCH_WEB"
    r"|READ_SCREEN|READ_FILE|GREP|SCROLL|TYPE"
    r"|GIT_STATUS|GIT_DIFF|GIT_COMMIT|GIT_CHECKOUT|GITHUB_PR|FETCH_URL)"
    r"(?:\s+([^\]]*?))?"                    # optional args
    r"\]?",                                 # optional ]
    re.IGNORECASE,
)


@dataclass
class AgentStep:
    action: str
    args: str = ""
    body: str = ""          # multi-line content (e.g. file content)
    result: Optional[str] = None
    success: Optional[bool] = None
    latency_ms: float = 0.0


@dataclass
class AgentResult:
    goal: str
    domain: str
    model_used: str
    steps: list[AgentStep] = field(default_factory=list)
    response_text: str = ""    # for single-turn (non-plan) responses
    success: bool = True
    error: Optional[str] = None
    total_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

def _parse_plan(text: str) -> list[AgentStep]:
    """Extract AgentStep objects from a planner model response."""
    steps: list[AgentStep] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _STEP_PATTERN.match(lines[i])
        if m:
            action = m.group(1).upper()
            args = (m.group(2) or "").strip()
            # Collect body lines until the next step or end
            body_lines = []
            i += 1
            while i < len(lines):
                if _STEP_PATTERN.match(lines[i]):
                    break
                body_lines.append(lines[i])
                i += 1
            # Strip leading/trailing blank lines from body
            body = "\n".join(body_lines).strip()
            # Remove markdown code fences from body
            body = re.sub(r"^```[a-z]*\n?", "", body, flags=re.MULTILINE)
            body = re.sub(r"\n?```$", "", body, flags=re.MULTILINE).strip()
            steps.append(AgentStep(action=action, args=args, body=body))
        else:
            i += 1
    return steps


# ---------------------------------------------------------------------------
# DevAgent
# ---------------------------------------------------------------------------

class DevAgent:
    """Agentic loop for dev-domain tasks.

    Wire-up in main.py:
        dev_agent = DevAgent(router, coordinator, trainer)
        result = await dev_agent.handle("write a PyTorch CNN for CIFAR-10")
    """

    # How many steps we'll run autonomously before pausing
    MAX_STEPS = 20

    def __init__(
        self,
        router: ModelRouter,
        coordinator: Optional["HybridCoordinator"] = None,
        trainer: Optional["ContinuousTrainer"] = None,
        session_context: Optional[list[str]] = None,
        agent_db: Optional["AgentDB"] = None,
    ) -> None:
        self._router = router
        self._coordinator = coordinator
        self._trainer = trainer
        self._agent_db = agent_db
        self._classifier = DomainClassifier()
        self._context: list[str] = session_context or []
        self._results_log: list[AgentResult] = []  # kept for get_last_result()
        self._indexer: Optional["CodebaseIndexer"] = None   # set via set_indexer()
        self._kiro: Optional["KiroClient"] = None            # set via set_kiro()

    def set_indexer(self, indexer: "CodebaseIndexer") -> None:
        """Wire a CodebaseIndexer for RAG context injection at plan/query time."""
        self._indexer = indexer

    def set_kiro(self, kiro: "KiroClient") -> None:
        """Wire a KiroClient for IDE context (cursor, file, git, diagnostics)."""
        self._kiro = kiro

    # ---------------------------------------------------------------------- #
    # Primary entry point
    # ---------------------------------------------------------------------- #

    async def handle(self, text: str, screenshot_b64: Optional[str] = None) -> AgentResult:
        """Classify, route, and execute a user query.

        - COMMAND domain → passes through to HybridCoordinator (existing pipeline)
        - PLAN domain → plan_and_run loop
        - CODE/MATH/VISION/GENERAL → single specialist inference, result returned
        """
        t0 = time.monotonic()
        domain = self._classifier.classify(text)
        log.info("DevAgent: domain=%s  text=%r", domain, text[:80])

        if domain == "command" and self._coordinator:
            # Pass through to the accessibility pipeline
            from command_executor import Command
            cmd = Command(text=text, action="CLARIFY", source="voice")
            result_dict = await self._coordinator.route(cmd)
            return AgentResult(
                goal=text,
                domain="command",
                model_used="llama3.1:8b",
                response_text=str(result_dict),
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )

        if domain == "plan":
            return await self.plan_and_run(text)

        if domain == "vision" and screenshot_b64 is None:
            # Auto-capture screen for vision queries
            screenshot_b64 = await self._capture_screenshot()

        # Single-turn specialist inference — inject RAG context for dev domains
        extra_ctx = self._format_context()
        if domain in ("code", "math", "vision", "general", "plan"):
            rag = await self._rag_context(text, n=3)
            if rag:
                extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag

        router_result = await self._router.infer(
            domain=domain,
            user_text=text,
            screenshot_b64=screenshot_b64,
            context=extra_ctx,
        )

        self._push_context(f"User: {text}\nAssistant ({router_result.model}): {router_result.text[:200]}")

        result = AgentResult(
            goal=text,
            domain=domain,
            model_used=router_result.model,
            response_text=router_result.text,
            success=router_result.ok,
            error=router_result.error,
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._results_log.append(result)
        await self._persist_run(result, command_id=None)

        # Speak EXPLAIN responses via Polly Bidirectional Streaming TTS.
        # The Node.js sidecar handles StartSpeechSynthesisStream internally, so
        # even a complete-string POST benefits from the Generative engine and
        # 24kHz audio quality. True token-by-token streaming to TTS would require
        # ModelRouter.infer_stream() — a future enhancement.
        if router_result.text and router_result.ok:
            try:
                from polly_stream import get_client as _get_tts
                asyncio.create_task(_get_tts().speak(router_result.text))
            except Exception as _tts_exc:
                log.debug("DevAgent TTS failed: %s", _tts_exc)

        return result

    # ---------------------------------------------------------------------- #
    # Plan → Execute → Reflect loop
    # ---------------------------------------------------------------------- #

    async def plan_and_run(self, goal: str) -> AgentResult:
        """Decompose a complex goal into steps and execute them sequentially."""
        t0 = time.monotonic()
        log.info("DevAgent: planning goal %r", goal[:80])

        # Step 1: Generate plan — inject RAG context + git/IDE context
        extra_ctx = self._format_context()
        rag = await self._rag_context(goal, n=4)
        if rag:
            extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag

        # Git context injection (item #8): gives LLM branch/diff awareness
        git_ctx = await self._git_context()
        if git_ctx:
            extra_ctx = f"{git_ctx}\n\n{extra_ctx}" if extra_ctx else git_ctx

        plan_result = await self._router.infer(
            domain="plan",
            user_text=goal,
            context=extra_ctx,
        )
        if not plan_result.ok:
            return AgentResult(
                goal=goal, domain="plan",
                model_used=plan_result.model,
                error=plan_result.error,
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )

        steps = _parse_plan(plan_result.text)
        if not steps:
            # Planner returned free-form — treat as single EXPLAIN step
            steps = [AgentStep(action="EXPLAIN", body=plan_result.text)]

        log.info("DevAgent: plan has %d steps", len(steps))

        # Step 2: Execute
        executed: list[AgentStep] = []
        for i, step in enumerate(steps[: self.MAX_STEPS]):
            log.info("DevAgent: step %d/%d  action=%s  args=%r",
                     i + 1, len(steps), step.action, step.args[:60])
            step_t0 = time.monotonic()
            try:
                step.result = await self._execute_step(step)
                step.success = True
            except Exception as exc:
                log.error("DevAgent: step %d failed: %s", i + 1, exc)
                step.result = f"ERROR: {exc}"
                step.success = False
            step.latency_ms = (time.monotonic() - step_t0) * 1000
            executed.append(step)

            if step.action in ("RUN_TERMINAL",) and not step.success:
                log.warning("DevAgent: step %d failed, stopping plan", i + 1)
                break

        # Step 3: Reflect — ask the model to summarise what was accomplished
        # and surface any problems from step outputs.
        reflect_text = await self._reflect(goal, executed, plan_result.model)

        self._push_context(f"Completed plan: {goal}\n"
                           + "\n".join(f"  {s.action} → {'ok' if s.success else 'failed'}"
                                       for s in executed))

        result = AgentResult(
            goal=goal,
            domain="plan",
            model_used=plan_result.model,
            steps=executed,
            response_text=reflect_text or plan_result.text,
            success=all(s.success for s in executed),
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._results_log.append(result)
        await self._persist_run(result, command_id=None)
        return result

    async def _reflect(
        self, goal: str, steps: list[AgentStep], model: str
    ) -> Optional[str]:
        """Reflect on executed steps: summarise outcomes, flag failures.

        Sends a lightweight prompt that includes each step's action + result
        so the model can reason about what was actually accomplished.
        Returns the reflection text, or None on failure.
        """
        if not steps:
            return None

        # Build step summary — include full result for failed steps so the
        # model can diagnose; truncate successes to avoid prompt bloat.
        lines = [f"Goal: {goal}", "", "Steps executed:"]
        for i, s in enumerate(steps, 1):
            status = "✓" if s.success else "✗"
            result_snippet = (s.result or "")
            if s.success:
                result_snippet = result_snippet[:200]
            else:
                result_snippet = result_snippet[:600]   # full error for failures
            lines.append(
                f"  {i}. {status} {s.action} {s.args[:60]}\n"
                f"     → {result_snippet}"
            )

        lines += [
            "",
            "Briefly summarise: what was accomplished, what (if anything) failed,"
            " and what the user should know or do next.",
        ]
        reflect_prompt = "\n".join(lines)

        try:
            r = await self._router.infer(
                domain="general",
                user_text=reflect_prompt,
                context=None,
            )
            if r.ok and r.text:
                log.info("DevAgent: reflection — %s", r.text[:120])
                return r.text
        except Exception as exc:
            log.debug("DevAgent._reflect() failed: %s", exc)
        return None

    # ---------------------------------------------------------------------- #
    # Step execution
    # ---------------------------------------------------------------------- #

    async def _execute_step(self, step: AgentStep) -> str:
        action = step.action.upper()

        if action == "WRITE_FILE":
            return await asyncio.to_thread(self._write_file, step.args, step.body)

        if action == "RUN_TERMINAL":
            cmd = step.args or step.body
            return await asyncio.to_thread(self._run_terminal, cmd)

        if action == "EXPLAIN":
            # Return text to the caller; no desktop action
            return step.body or step.args

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
            return await asyncio.to_thread(self._read_file, path_str.strip())

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
            return await asyncio.to_thread(self._git_commit, msg)

        if action == "GIT_CHECKOUT":
            # args: [-b] <branch>
            branch_args = (step.args or "").strip()
            return await asyncio.to_thread(self._git_checkout, branch_args)

        # ── GitHub integration (roadmap item #3) ────────────────────────────

        if action == "GITHUB_PR":
            # args: title  body: PR description
            title = (step.args or "").strip()
            body = (step.body or "").strip()
            if not title:
                raise ValueError("GITHUB_PR requires a title in args")
            return await asyncio.to_thread(self._github_pr, title, body)

        # ── Web retrieval (roadmap item #3) ─────────────────────────────────

        if action == "FETCH_URL":
            url = (step.args or step.body or "").strip()
            if not url:
                raise ValueError("FETCH_URL requires a URL")
            return await self._fetch_url(url)

        # Fall through: accessibility verbs → CommandExecutor
        if self._coordinator:
            from command_executor import Command
            cmd = Command(
                text=step.args,
                action=action,
                source="dev_agent",
                params=self._parse_accessibility_params(action, step.args),
            )
            result_dict = await self._coordinator._executor.execute(cmd)
            return json.dumps(result_dict)

        return f"No executor for action: {action}"

    # ---------------------------------------------------------------------- #
    # Dev action implementations
    # ---------------------------------------------------------------------- #

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

        Returns matching lines as a string (file:line: content format).
        Uses Python re for portability — no dependency on system grep.
        """
        import os as _os

        root = Path(search_path)
        if not root.exists():
            return f"Path does not exist: {search_path}"

        compiled = re.compile(pattern)
        results: list[str] = []
        extensions = {".py", ".swift", ".md", ".txt", ".json", ".yaml", ".yml"}

        def _search_file(fp: Path) -> None:
            if len(results) >= max_lines:
                return
            try:
                for lineno, line in enumerate(
                    fp.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if compiled.search(line):
                        results.append(f"{fp}:{lineno}: {line.rstrip()}")
                        if len(results) >= max_lines:
                            break
            except OSError:
                pass

        if root.is_file():
            _search_file(root)
        else:
            for dirpath, dirnames, filenames in _os.walk(root):
                # Prune excluded directories in-place
                dirnames[:] = [
                    d for d in dirnames
                    if d not in {"__pycache__", ".git", "node_modules",
                                 "venv", ".venv", "chroma_db", "DerivedData"}
                ]
                for fname in filenames:
                    if Path(fname).suffix in extensions:
                        _search_file(Path(dirpath) / fname)
                    if len(results) >= max_lines:
                        break

        if not results:
            return f"No matches for pattern {pattern!r} in {search_path}"
        summary = f"Found {len(results)} match(es)"
        if len(results) >= max_lines:
            summary += f" (truncated at {max_lines})"
        return summary + "\n" + "\n".join(results)

    @staticmethod
    def _run_terminal(cmd: str) -> str:
        cmd = cmd.strip()
        log.info("DevAgent: running terminal command: %s", cmd)
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
        log.info("DevAgent: terminal %s → %s", status, output[:120])
        if result.returncode != 0:
            raise RuntimeError(f"Command failed ({status}): {output[:200]}")
        return output or status

    # ── Git implementations ──────────────────────────────────────────────────

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
        # Stage all tracked changes then commit
        subprocess.run(["git", "add", "-u"], check=True, timeout=10)
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

    async def _fetch_url(self, url: str, max_chars: int = 6000) -> str:
        """Fetch a URL and return extracted text (replaces browser-open SEARCH_WEB)."""
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
                    log.info("DevAgent: fetched %s (%d chars)", url, len(text))
                    return text
        except Exception as exc:
            raise RuntimeError(f"FETCH_URL {url} failed: {exc}") from exc

    # ── Context helpers ──────────────────────────────────────────────────────

    async def _git_context(self) -> Optional[str]:
        """Fetch git state for plan prompt injection.

        Tries KiroClient first (richer VS Code git data), falls back to
        subprocess git commands directly.
        """
        # Try Kiro first
        if self._kiro is not None:
            git = await self._kiro.get_git_context()
            if git and "error" not in git:
                return self._kiro.format_git_context_for_prompt(git)

        # Subprocess fallback
        try:
            result = await asyncio.to_thread(
                lambda: subprocess.run(
                    ["git", "status", "--short", "--branch"],
                    capture_output=True, text=True, timeout=5,
                )
            )
            if result.returncode == 0 and result.stdout.strip():
                out = result.stdout.strip()
                return f"```git-context\n{out}\n```"
        except Exception as exc:
            log.debug("DevAgent._git_context() subprocess fallback failed: %s", exc)

        return None

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

    # ---------------------------------------------------------------------- #
    # DB persistence
    # ---------------------------------------------------------------------- #

    async def _persist_run(
        self, result: AgentResult, command_id: Optional[int]
    ) -> None:
        if not self._agent_db or not self._agent_db.available:
            return
        run_id = await self._agent_db.insert_agent_run(
            command_id=command_id,
            goal=result.goal,
            domain=result.domain,
            model_used=result.model_used,
            step_count=len(result.steps),
            success=result.success,
            total_latency_ms=result.total_latency_ms,
            error=result.error,
        )
        for i, step in enumerate(result.steps):
            await self._agent_db.insert_agent_step(
                run_id=run_id,
                step_num=i + 1,
                action=step.action,
                args=step.args or None,
                body=step.body or None,
                result=step.result,
                success=step.success,
                latency_ms=step.latency_ms,
            )

    # ---------------------------------------------------------------------- #
    # Context management
    # ---------------------------------------------------------------------- #

    def _push_context(self, entry: str) -> None:
        self._context.append(entry)
        if len(self._context) > 10:
            self._context = self._context[-10:]

    def _format_context(self) -> Optional[str]:
        if not self._context:
            return None
        return "\n".join(self._context[-5:])

    async def _rag_context(self, query: str, n: int = 3) -> Optional[str]:
        """Fetch top-n relevant source chunks from CodebaseIndexer for `query`.

        Returns a formatted string block suitable for injection as extra context
        in the system/user prompt, or None if the indexer is unavailable or returns
        no useful hits.
        """
        if self._indexer is None or not self._indexer.available:
            return None
        try:
            hits = await self._indexer.query_combined(query, n=n)
            if not hits:
                return None
            lines = ["[Relevant codebase context]"]
            for h in hits:
                if h.get("chunk_type") == "page":
                    lines.append(
                        f"# {h['file']} p.{h.get('page')} (score={h.get('score', 0):.2f})"
                    )
                else:
                    lines.append(
                        f"# {h['file']}::{h.get('name')} [{h.get('chunk_type')}]"
                        f" line {h.get('start_line', '?')} (score={h.get('score', 0):.2f})"
                    )
                snippet = (h.get("text") or "")[:600]
                lines.append(snippet)
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            log.debug("DevAgent._rag_context() failed: %s", exc)
            return None

    # ---------------------------------------------------------------------- #
    # Status / introspection
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "classifier": "DomainClassifier (keyword-scoring)",
            "router": self._router.get_status(),
            "context_entries": len(self._context),
            "tasks_completed": len(self._results_log),
            "rag_indexer": (
                "wired" if (self._indexer and self._indexer.available)
                else "not wired" if self._indexer is None
                else "unavailable"
            ),
            "kiro_bridge": (
                self._kiro.get_status() if self._kiro is not None else "not wired"
            ),
        }

    def get_last_result(self) -> Optional[AgentResult]:
        return self._results_log[-1] if self._results_log else None
