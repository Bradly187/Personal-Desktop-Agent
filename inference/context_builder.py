import logging
import os
import subprocess
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from inference.codebase_indexer import CodebaseIndexer
    from storage.db import AgentDB

log = logging.getLogger(__name__)

class ContextBuilder:
    def __init__(self, agent: "DevAgent", agent_db: Optional['AgentDB'] = None, memory=None, indexer: Optional['CodebaseIndexer'] = None, repo_root: str = '', session_context: Optional[list[str]] = None):
        self._agent = agent
        self._agent_db = agent_db
        self._memory = memory
        self._indexer = indexer
        self._repo_root = repo_root or os.getcwd()
        self._context = session_context or []
        self._workspace_built = False
        self._workspace_block = None

    def set_indexer(self, indexer: 'CodebaseIndexer') -> None:
        self._indexer = indexer

    def set_repo_root(self, path: str) -> bool:
        rp = os.path.realpath(os.path.expanduser(path or ''))
        if not os.path.isdir(rp): return False
        self._repo_root = rp
        return True

    async def _session_seed_context(self, goal: str) -> str:
        """Build the cross-session working-memory seed for a fresh plan, or ''.

        Off (DA_SESSION_MEMORY unset) or any failure → '' so the plan context is
        byte-identical to today (R4.4). Scans recent runs, selects those lexically
        related to ``goal``, and renders their compact memory (derived from the
        durable agent_steps — no schema change) into a <prior-session-memory>
        block. Bounded work: one recent-runs query + ≤top_k step queries, once per
        plan, off the 60 Hz path (AGENTS.md #2)."""
        from inference.working_memory import session_memory_enabled
        if not session_memory_enabled():
            return ""
        try:
            from inference.working_memory import (
                select_related_runs, summarize_run, render_session_seed,
            )
            db = self._db()
            if not db:
                return ""
            candidates = await db.get_recent_runs(limit=20)
            related = select_related_runs(goal, candidates)
            if not related:
                return ""
            mems: list[tuple[str, object]] = []
            for run in related:
                run_id = run.get("id")
                if run_id is None:
                    continue
                steps = await db.get_steps_for_run(int(run_id))
                if not steps:
                    continue
                run_goal = run.get("goal", "") or ""
                mems.append((run_goal, summarize_run(run_goal, steps)))
            return render_session_seed(mems)
        except Exception as exc:
            log.debug("DevAgent._session_seed_context failed: %s", exc)
            return ""

    async def _git_context(self) -> Optional[str]:
        """Fetch git state for plan prompt injection.

        Tries BridgeClient first (richer VS Code git data), falls back to
        subprocess git commands directly.
        """
        # Try Bridge first
        if self._bridge is not None:
            git = await self._bridge.get_git_context()
            if git and "error" not in git:
                return self._bridge.format_git_context_for_prompt(git)

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

    def _push_context(self, entry: str) -> None:
        self._context.append(entry)
        if len(self._context) > 10:
            self._context = self._context[-10:]

    def _format_context(self) -> Optional[str]:
        if not self._context:
            return None
        return "\n".join(self._context[-5:])

    def _workspace_context(self) -> Optional[str]:
        """Stable repo-facts block, built once and memoized (Gap A, R2.1).

        Returns None when the feature is off or nothing could be collected, so
        the caller's extra_ctx is byte-identical to today (R4.4). Build failure
        degrades to None — never blocks the plan (R4.3)."""
        if not self._repo_context_enabled:
            return None
        if not self._workspace_built:
            self._workspace_built = True
            try:
                from inference.workspace_context import build_workspace_context
                block, stats = build_workspace_context(self._repo_root)
                self._workspace_block = block or None
                if self._workspace_block:
                    log.info("DevAgent: workspace context %d chars (git=%s, files=%d)",
                             stats.get("chars_out", 0), stats.get("has_git"),
                             stats.get("files_read", 0))
            except Exception as exc:  # never block the plan path (R4.3)
                log.warning("DevAgent: workspace context build failed: %s", exc)
                self._workspace_block = None
        return self._workspace_block

    def invalidate_workspace_context(self) -> None:
        """Drop the memoized workspace block so the next plan rebuilds it (R2.2).

        For a long-lived session after a branch switch / CLAUDE.md edit. Never
        called on the 60 Hz path (AGENTS.md #2 — dev-agent-only)."""
        self._workspace_built = False
        self._workspace_block = None

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
        except Exception as exc:
            log.debug("DevAgent._rag_context() failed: %s", exc)
            return None

        try:
            if not hits:
                return None
            body_lines = []
            for h in hits:
                if h.get("chunk_type") == "page":
                    body_lines.append(
                        f"# {h['file']} p.{h.get('page')} (score={h.get('score', 0):.2f})"
                    )
                else:
                    body_lines.append(
                        f"# {h['file']}::{h.get('name')} [{h.get('chunk_type')}]"
                        f" line {h.get('start_line', '?')} (score={h.get('score', 0):.2f})"
                    )
                snippet = (h.get("text") or "")[:600]
                body_lines.append(snippet)
                body_lines.append("")
            body = "\n".join(body_lines)
            # C2: cap total size so a flooding indexer can't blow the context.
            if len(body) > _RAG_MAX_CHARS:
                body = body[:_RAG_MAX_CHARS] + "\n…[truncated]"
            # C2: wrap retrieved chunks as DATA, not instructions.
            return f"{_RAG_OPEN_FENCE}\n{body}\n{_RAG_CLOSE_FENCE}"
        except Exception as exc:
            log.debug("DevAgent._rag_context() failed: %s", exc)
            return None

    def __getattr__(self, name):
        agent = self.__dict__.get('_agent')
        if not agent:
            raise AttributeError(name)
        return getattr(agent, name)