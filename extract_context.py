import ast
import os

src = open('inference/dev_agent.py', encoding='utf8').read().splitlines()
tree = ast.parse('\n'.join(src))

dev_agent = None
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == 'DevAgent':
        dev_agent = node
        break

if not dev_agent:
    print("DevAgent not found")
    exit(1)

methods = [
    '_session_seed_context',
    '_git_context',
    '_push_context',
    '_format_context',
    '_workspace_context',
    'invalidate_workspace_context',
    '_rag_context'
]

lines = [
    "import logging",
    "import os",
    "import subprocess",
    "from typing import Optional, TYPE_CHECKING",
    "",
    "if TYPE_CHECKING:",
    "    from inference.codebase_indexer import CodebaseIndexer",
    "    from storage.db import AgentDB",
    "",
    "log = logging.getLogger(__name__)",
    "",
    "class ContextBuilder:",
    "    def __init__(self, agent_db: Optional['AgentDB'] = None, memory=None, indexer: Optional['CodebaseIndexer'] = None, repo_root: str = '', session_context: Optional[list[str]] = None):",
    "        self._agent_db = agent_db",
    "        self._memory = memory",
    "        self._indexer = indexer",
    "        self._repo_root = repo_root or os.getcwd()",
    "        self._context = session_context or []",
    "        self._workspace_built = False",
    "        self._workspace_block = None",
    "",
    "    def set_indexer(self, indexer: 'CodebaseIndexer') -> None:",
    "        self._indexer = indexer",
    "",
    "    def set_repo_root(self, path: str) -> bool:",
    "        rp = os.path.realpath(os.path.expanduser(path or ''))",
    "        if not os.path.isdir(rp):",
    "            return False",
    "        self._repo_root = rp",
    "        return True",
    "",
]

for node in dev_agent.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods:
        # Subtract indent (8 spaces) since it was inside DevAgent, we put it inside ContextBuilder (4 spaces)
        method_lines = src[node.lineno-1:node.end_lineno]
        method_lines = [l[4:] if l.startswith('    ') else l for l in method_lines]
        lines.extend(method_lines)
        lines.append('')

with open('inference/context_builder.py', 'w', encoding='utf8') as f:
    f.write('\n'.join(lines))
print("Created inference/context_builder.py")
