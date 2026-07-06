import ast
import subprocess

# get original dev_agent source
orig_src = subprocess.check_output(['git', 'show', 'HEAD:inference/dev_agent.py']).decode('utf8')
tree = ast.parse(orig_src)

decorators_map = {}
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == 'DevAgent':
        for m in node.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if m.decorator_list:
                    # just collect the names of the decorators (e.g., 'staticmethod', 'classmethod')
                    decs = []
                    for d in m.decorator_list:
                        if isinstance(d, ast.Name):
                            decs.append(d.id)
                    decorators_map[m.name] = decs

files = ['inference/saga_manager.py', 'inference/step_executor.py', 'inference/context_builder.py', 'inference/plan_parser.py']

for fpath in files:
    with open(fpath, 'r', encoding='utf8') as f:
        lines = f.read().splitlines()
    
    out = []
    for line in lines:
        if line.strip().startswith('def ') or line.strip().startswith('async def '):
            # extract method name
            parts = line.strip().split(' ')
            name_part = parts[1] if parts[0] == 'def' else parts[2]
            mname = name_part.split('(')[0]
            
            if mname in decorators_map and decorators_map[mname]:
                indent = line[:len(line) - len(line.lstrip())]
                for d in decorators_map[mname]:
                    out.append(f"{indent}@{d}")
        out.append(line)
        
    with open(fpath, 'w', encoding='utf8') as f:
        f.write('\n'.join(out))

print("Restored decorators.")
