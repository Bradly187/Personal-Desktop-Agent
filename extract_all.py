import ast
import os

src = open('inference/dev_agent.py', encoding='utf8').read().splitlines()
tree = ast.parse('\n'.join(src))
dev_agent = None
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == 'DevAgent':
        dev_agent = node
        break

# ContextBuilder
cb_methods = ['_session_seed_context', '_git_context', '_push_context', '_format_context', '_workspace_context', 'invalidate_workspace_context', '_rag_context']
cb_lines = [
    "import logging", "import os", "import subprocess", "from typing import Optional, TYPE_CHECKING", "",
    "if TYPE_CHECKING:", "    from inference.codebase_indexer import CodebaseIndexer", "    from storage.db import AgentDB", "",
    "log = logging.getLogger(__name__)", "",
    "class ContextBuilder:",
    "    def __init__(self, agent_db: Optional['AgentDB'] = None, memory=None, indexer: Optional['CodebaseIndexer'] = None, repo_root: str = '', session_context: Optional[list[str]] = None):",
    "        self._agent_db = agent_db",
    "        self._memory = memory",
    "        self._indexer = indexer",
    "        self._repo_root = repo_root or os.getcwd()",
    "        self._context = session_context or []",
    "        self._workspace_built = False",
    "        self._workspace_block = None", "",
    "    def set_indexer(self, indexer: 'CodebaseIndexer') -> None:",
    "        self._indexer = indexer", "",
    "    def set_repo_root(self, path: str) -> bool:",
    "        rp = os.path.realpath(os.path.expanduser(path or ''))",
    "        if not os.path.isdir(rp): return False",
    "        self._repo_root = rp",
    "        return True", ""
]

# SagaManager
sm_methods = ['_start_run', '_saga_dir', '_snapshot_for_write', '_saga_git_backend_enabled', '_git_blob_snapshot', '_git_cat_blob', '_compensation_for', '_pre_register_step', '_persist_step', '_halt_and_compensate', '_run_compensations', '_restore_file', '_finalize_run', 'revert_last_run', '_escalation_sidecar', '_record_escalation', '_append_escalation_sidecar', 'reconcile_pending_escalations', '_read_escalation_sidecar', '_rewrite_escalation_sidecar']
sm_lines = [
    "import asyncio", "import json", "import logging", "import os", "import shutil", "import subprocess", "import time", "from pathlib import Path", "from typing import Optional, TYPE_CHECKING", "from inference.plan_parser import AgentResult, AgentStep", "",
    "if TYPE_CHECKING:", "    from storage.db import AgentDB", "",
    "log = logging.getLogger(__name__)", "",
    "class SagaManager:",
    "    def __init__(self, agent_db: Optional['AgentDB'] = None):",
    "        self._agent_db = agent_db",
    "        self._saga_announce = os.environ.get('DA_SAGA_ANNOUNCE', '1').strip().lower() in ('1', 'true', 'on', 'yes')",
    "        self._rollback_summary = None",
    "        self._escalation_sidecar_path = Path.home() / '.claude' / 'escalations_pending.jsonl'", ""
]

# StepExecutor
se_methods = ['_execute_step', '_parse_skill_args', '_build_skill_args', '_execute_skill_step', '_audit_skill', '_verify_math_with_cas', '_extract_cas_check', '_handle_skill', '_handle_personal_query', '_apply_edit', '_diff_for_confirm', '_write_file', '_read_file', '_grep', '_run_terminal', '_git_status', '_git_diff', '_git_commit', '_git_checkout', '_github_pr', '_fetch_url', '_scan_web_content', '_capture_screenshot', '_parse_accessibility_params', '_confirm_destructive_op', '_confirm_destructive_op_locked']
se_lines = [
    "import asyncio", "import json", "import logging", "import os", "import re", "import shutil", "import subprocess", "import time", "import uuid", "import webbrowser", "from typing import Optional, TYPE_CHECKING", "from inference.plan_parser import AgentStep, AgentResult", "from core.approval_keywords import classify_confirmation", "from inference.edit_format import EditApplier, render_hashline, HASHLINE, UDIFF", "",
    "if TYPE_CHECKING:", "    from inference.model_router import ModelRouter", "    from core.hybrid_coordinator import HybridCoordinator", "    from inference.bridge_client import BridgeClient", "    from storage.db import AgentDB", "",
    "log = logging.getLogger(__name__)", "",
    "class StepExecutor:",
    "    def __init__(self, router: 'ModelRouter', coordinator: Optional['HybridCoordinator'], agent_db: Optional['AgentDB'] = None):",
    "        self._router = router",
    "        self._coordinator = coordinator",
    "        self._agent_db = agent_db",
    "        self._bridge = None",
    "        self._skill_registry = None",
    "        self._personal_kb = None",
    "        self._edit_applier = EditApplier()",
    "        self._repo_root = os.getcwd()",
    "        self._confirm_lock = asyncio.Lock()",
    "        self._confirm_whisper = None", "",
    "    def set_bridge(self, bridge: 'BridgeClient') -> None:",
    "        self._bridge = bridge", "",
    "    def set_skill_registry(self, registry) -> None:",
    "        self._skill_registry = registry", "",
    "    def set_personal_kb(self, kb) -> None:",
    "        self._personal_kb = kb", "",
    "    def set_repo_root(self, path: str) -> None:",
    "        self._repo_root = path", ""
]

def append_methods(methods, out_lines):
    for node in dev_agent.body:
        if getattr(node, 'name', '') in methods:
            out_lines.extend(src[node.lineno-1:node.end_lineno])
            out_lines.append('')

append_methods(cb_methods, cb_lines)
append_methods(sm_methods, sm_lines)
append_methods(se_methods, se_lines)

with open('inference/context_builder.py', 'w', encoding='utf8') as f: f.write('\n'.join(cb_lines))
with open('inference/saga_manager.py', 'w', encoding='utf8') as f: f.write('\n'.join(sm_lines))
with open('inference/step_executor.py', 'w', encoding='utf8') as f: f.write('\n'.join(se_lines))
print("All files rewritten cleanly")
