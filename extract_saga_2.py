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
    '_escalation_sidecar',
    '_record_escalation',
    '_append_escalation_sidecar',
    'reconcile_pending_escalations',
    '_read_escalation_sidecar',
    '_rewrite_escalation_sidecar'
]

lines = []
for node in dev_agent.body:
    if getattr(node, 'name', '') in methods:
        method_lines = src[node.lineno-1:node.end_lineno]
        method_lines = [l[4:] if l.startswith('    ') else l for l in method_lines]
        lines.extend(method_lines)
        lines.append('')

with open('inference/saga_manager.py', 'a', encoding='utf8') as f:
    f.write('\n'.join(lines))
print("Appended escalations to saga_manager.py")
