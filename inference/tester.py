"""Autonomous Tester loop for the DevAgent WRITE_FILE path (specs/dev-agent-critic R3).

After a WRITE_FILE to a `.py` source file commits, generate a focused pytest test
for the change, run it through the EXISTING sandbox
(`inference.sandbox.run_sandboxed`), and surface the outcome. Closes the gap where
the agent only learns a change is broken if a human-written test happens to
exercise it later.

Semantics (safe-observation, by design): the write has already succeeded when the
test runs, so a failing generated test is surfaced as an **observation** appended
to the step result (the reflect step and any later plan steps see it) — it does
NOT mark the good write failed, so the saga never rolls back working code. The
stronger "force a replan + rollback" coupling is a deliberate non-goal here.

Ephemeral-sandbox aware (R3.7): `run_sandboxed` is one-shot with no cross-step
state, so the test is run as a single self-contained `pytest <file>` invocation —
never split across steps that assume shared setup. Degrades gracefully (R3.5):
any failure to generate/run skips the test and is NEVER reported as a pass; it
never blocks the plan. The model is injected as a `router`, so this is unit-tested
without a GPU.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.execution_policy import ExecutionScope
from inference.sandbox import (
    ContainmentPolicy,
    WorkTrust,
    get_containment_policy,
)

log = logging.getLogger(__name__)

_MAX_CODE_CHARS = 6000      # cap the code we show the generator
_MAX_OUTPUT_CHARS = 4000    # cap the traceback we surface as an observation


@dataclass
class TestArtifact:
    __test__ = False  # not a pytest test class
    path: str         # scratch test file (NOT the repo tests/ tree)
    body: str
    target: str       # the source file it exercises


@dataclass
class TestOutcome:
    __test__ = False  # not a pytest test class
    ran: bool = False
    passed: Optional[bool] = None     # None when not run
    skipped: bool = False
    reason: str = ""                  # why skipped / generation failed
    output: str = ""                  # captured pytest output (truncated)
    note: str = ""                    # one-line(ish) observation for the step result
    artifact: Optional[TestArtifact] = None


_TEST_PROMPT = """\
Write a SINGLE, self-contained pytest test for the change below. Requirements:
- Output ONLY one Python code block — no prose.
- It must be runnable as `pytest <thisfile>` from the project root with NO network
  and NO extra setup (the sandbox is offline and one-shot).
- Import the code under test from its module; test the behavior the GOAL describes.
- Prefer one focused test function over many; keep it short.

GOAL: {goal}
FILE: {path}

CODE (current contents of {path}):
{code}"""

_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL)


def _extract_code_block(text: str) -> str:
    """Return the first fenced code block, else the whole text (best-effort)."""
    if not text:
        return ""
    m = _CODE_BLOCK_RE.search(text)
    return (m.group(1) if m else text).strip()


def is_testable_source(path_str: str) -> bool:
    """A `.py` source file worth generating a test for — not a test file itself,
    not under a tests/ tree (R3.2: we generate tests for source, not for tests)."""
    p = (path_str or "").strip().strip("'\"").replace("\\", "/").lower()
    if not p.endswith(".py"):
        return False
    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return False
    name = parts[-1]
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return False
    if "tests" in parts:
        return False
    return True


class Tester:
    """Generate + run a focused pytest test for a just-written source file."""

    __test__ = False  # not a pytest test class

    def __init__(self, router, *, execution_scope: ExecutionScope,
                 code_domain: str = "code",
                 scratch_root: Optional[str] = None,
                 sandbox_timeout: float = 60.0,
                 containment_policy: ContainmentPolicy | None = None) -> None:
        self._router = router
        self._execution_scope = execution_scope
        self._containment_policy = containment_policy or get_containment_policy()
        self._code_domain = code_domain
        self._scratch_root = Path(scratch_root) if scratch_root else (
            Path(tempfile.gettempdir()) / "pda_dev_tests"
        )
        self._timeout = sandbox_timeout

    async def generate_and_run(self, goal: str, path: str, code: str) -> TestOutcome:
        """Generate a test for `code`, run it sandboxed, return the outcome.

        Never raises — any failure degrades to a skipped outcome (R3.5)."""
        import asyncio
        try:
            prompt = _TEST_PROMPT.format(
                goal=(goal or "(no goal)")[:300], path=path, code=(code or "")[:_MAX_CODE_CHARS])
            result = await self._router.infer(
                domain=self._code_domain, user_text=prompt, context="")
            if not getattr(result, "ok", True):
                return TestOutcome(skipped=True, reason="test generation failed")
            test_src = _extract_code_block(getattr(result, "text", "") or "")
            if not test_src:
                return TestOutcome(skipped=True, reason="no test generated")

            # Resolve the active project before execution while keeping the
            # generated artifact in the configured temp scratch directory.
            cwd_decision = await asyncio.to_thread(
                self._execution_scope.authorize_cwd, None
            )
            if not cwd_decision.allowed or cwd_decision.canonical_path is None:
                raise PermissionError(cwd_decision.reason or "tester cwd denied")
            project_dir = str(cwd_decision.canonical_path)
            artifact_path = self._artifact_path(path, project_dir)
            artifact_decision = await asyncio.to_thread(
                self._execution_scope.authorize_path,
                str(artifact_path),
                operation="autonomous tester artifact",
            )
            if not artifact_decision.allowed or artifact_decision.canonical_path is None:
                raise PermissionError(
                    artifact_decision.reason or "autonomous tester artifact denied"
                )
            artifact = await asyncio.to_thread(
                self._write_artifact,
                artifact_decision.canonical_path,
                path,
                test_src,
            )

            sb = await asyncio.to_thread(
                self._run_pytest, artifact.path, project_dir
            )
            passed = sb.returncode == 0
            output = ((sb.stdout or "") + (sb.stderr or ""))[:_MAX_OUTPUT_CHARS]
            note = ("[tester] generated test PASSED" if passed
                    else f"[tester] generated test FAILED:\n{output}")
            return TestOutcome(ran=True, passed=passed, skipped=False,
                               output=output, note=note, artifact=artifact)
        except Exception as exc:                       # degrade-gracefully (R3.5)
            log.warning("Tester: degraded (%s) — skipping", exc)
            return TestOutcome(skipped=True, reason=str(exc))

    def _artifact_path(self, target: str, project_dir: str) -> Path:
        stem = Path(target.strip().strip("'\"")).stem or "module"
        return self._scratch_root / f"test_gen_{stem}.py"

    @staticmethod
    def _write_artifact(out: Path, target: str, body: str) -> TestArtifact:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        return TestArtifact(path=str(out), body=body, target=target)

    def _run_pytest(self, test_path: str, project_dir: str):
        """One-shot sandboxed `pytest <file>` (R3.7 — no cross-step state)."""
        from inference.sandbox import run_sandboxed
        source_b64 = base64.b64encode(Path(test_path).read_bytes()).decode("ascii")
        inner = (
            "import base64, os, pathlib, pytest\n"
            "path = pathlib.Path('/tmp/pda_generated_test.py')\n"
            "path.write_bytes(base64.b64decode(os.environ['PDA_TEST_SOURCE_B64']))\n"
            "raise SystemExit(pytest.main(['-q', str(path)]))"
        )
        python = (
            '"$HOME/.venv-wsl/bin/python"'
            if os.name == "nt"
            else shlex.quote(sys.executable)
        )
        cmd = f"{python} -c {shlex.quote(f'exec({inner!r})')}"
        return run_sandboxed(
            cmd,
            project_dir=project_dir,
            timeout=self._timeout,
            work_trust=WorkTrust.AUTONOMOUS_OR_UNTRUSTED,
            containment_policy=self._containment_policy,
            extra_env={"PDA_TEST_SOURCE_B64": source_b64},
        )
