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
    '_start_run',
    '_saga_dir',
    '_snapshot_for_write',
    '_saga_git_backend_enabled',
    '_git_blob_snapshot',
    '_git_cat_blob',
    '_compensation_for',
    '_pre_register_step',
    '_persist_step',
    '_halt_and_compensate',
    '_run_compensations',
    '_restore_file',
    '_finalize_run',
    'revert_last_run'
]

lines = [
    "import asyncio",
    "import json",
    "import logging",
    "import os",
    "import shutil",
    "import subprocess",
    "import time",
    "from pathlib import Path",
    "from typing import Optional, TYPE_CHECKING",
    "from inference.plan_parser import AgentResult, AgentStep",
    "",
    "if TYPE_CHECKING:",
    "    from storage.db import AgentDB",
    "",
    "log = logging.getLogger(__name__)",
    "",
    "class SagaManager:",
    "    def __init__(self, agent_db: Optional['AgentDB'] = None):",
    "        self._agent_db = agent_db",
    "        self._saga_announce = os.environ.get('DA_SAGA_ANNOUNCE', '1').strip().lower() in ('1', 'true', 'on', 'yes')",
    "        self._rollback_summary = None",
    ""
]

for node in dev_agent.body:
    if getattr(node, 'name', '') in methods:
        method_lines = src[node.lineno-1:node.end_lineno]
        method_lines = [l[4:] if l.startswith('    ') else l for l in method_lines]
        lines.extend(method_lines)
        lines.append('')

with open('inference/saga_manager.py', 'w', encoding='utf8') as f:
    f.write('\n'.join(lines))
print("Created inference/saga_manager.py")
