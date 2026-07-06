import ast
import os

src = open('inference/dev_agent.py', encoding='utf8').read().splitlines()
tree = ast.parse('\n'.join(src))

dev_agent = None
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == 'DevAgent':
        dev_agent = node
        break

methods = [
    '_execute_step',
    '_parse_skill_args',
    '_build_skill_args',
    '_execute_skill_step',
    '_audit_skill',
    '_verify_math_with_cas',
    '_extract_cas_check',
    '_handle_skill',
    '_handle_personal_query',
    '_apply_edit',
    '_diff_for_confirm',
    '_write_file',
    '_read_file',
    '_grep',
    '_run_terminal',
    '_git_status',
    '_git_diff',
    '_git_commit',
    '_git_checkout',
    '_github_pr',
    '_fetch_url',
    '_scan_web_content',
    '_capture_screenshot',
    '_parse_accessibility_params',
    '_confirm_destructive_op',
    '_confirm_destructive_op_locked'
]

lines = [
    "import asyncio",
    "import json",
    "import logging",
    "import os",
    "import re",
    "import shutil",
    "import subprocess",
    "import time",
    "import uuid",
    "import webbrowser",
    "from typing import Optional, TYPE_CHECKING",
    "from inference.plan_parser import AgentStep, AgentResult",
    "from core.approval_keywords import classify_confirmation",
    "from inference.edit_format import EditApplier, render_hashline, HASHLINE, UDIFF",
    "",
    "if TYPE_CHECKING:",
    "    from inference.model_router import ModelRouter",
    "    from core.hybrid_coordinator import HybridCoordinator",
    "    from inference.bridge_client import BridgeClient",
    "    from storage.db import AgentDB",
    "",
    "log = logging.getLogger(__name__)",
    "",
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
    "        self._confirm_whisper = None",
    "",
    "    def set_bridge(self, bridge: 'BridgeClient') -> None:",
    "        self._bridge = bridge",
    "",
    "    def set_skill_registry(self, registry) -> None:",
    "        self._skill_registry = registry",
    "",
    "    def set_personal_kb(self, kb) -> None:",
    "        self._personal_kb = kb",
    "",
    "    def set_repo_root(self, path: str) -> None:",
    "        self._repo_root = path",
    ""
]

for node in dev_agent.body:
    if getattr(node, 'name', '') in methods:
        method_lines = src[node.lineno-1:node.end_lineno]
        method_lines = [l[4:] if l.startswith('    ') else l for l in method_lines]
        lines.extend(method_lines)
        lines.append('')

with open('inference/step_executor.py', 'w', encoding='utf8') as f:
    f.write('\n'.join(lines))
print("Created inference/step_executor.py")
