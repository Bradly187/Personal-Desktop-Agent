import ast
import re

files = [
    'inference/context_builder.py',
    'inference/saga_manager.py',
    'inference/step_executor.py'
]

for fpath in files:
    with open(fpath, 'r', encoding='utf8') as f:
        src = f.read()

    # Find the __init__ line
    init_match = re.search(r'(def __init__\([^:]+):', src)
    if not init_match:
        continue

    # Add `agent: "DevAgent"` to the signature
    old_init = init_match.group(1)
    if 'agent: "DevAgent"' not in old_init:
        # replace `def __init__(self, ` with `def __init__(self, agent: "DevAgent", `
        new_init = old_init.replace('def __init__(self, ', 'def __init__(self, agent: "DevAgent", ')
        if new_init == old_init:
            new_init = old_init.replace('def __init__(self)', 'def __init__(self, agent: "DevAgent")')
        
        src = src.replace(old_init + ':', new_init + ':')
        
        # Inject `self._agent = agent` right after `def __init__(...):`
        # and inject `__getattr__` method
        
        # Find the end of __init__ def line
        init_pos = src.find(new_init + ':')
        # find the newline
        eol_pos = src.find('\n', init_pos)
        
        # insert `self._agent = agent`
        src = src[:eol_pos+1] + '        self._agent = agent\n' + src[eol_pos+1:]
        
        # append __getattr__ to the class
        # we can just put it at the very end of the file since it's all one class
        src += "\n    def __getattr__(self, name):\n        return getattr(self._agent, name)\n"

    # Also make sure "DevAgent" can be typed
    if "DevAgent" not in src:
        src = src.replace("if TYPE_CHECKING:", "if TYPE_CHECKING:\n    from inference.dev_agent import DevAgent")
        
    with open(fpath, 'w', encoding='utf8') as f:
        f.write(src)

print("Patched __init__ and __getattr__ on components.")
