import re

with open('inference/dev_agent.py', 'r', encoding='utf8') as f:
    src = f.read()

# Add instantiation in __init__
init_code = """        self._edit_applier = EditApplier()

        self._context_builder = ContextBuilder(agent=self, agent_db=self._agent_db, memory=self._memory, session_context=session_context)
        self._saga_manager = SagaManager(agent=self, agent_db=self._agent_db)
        self._step_executor = StepExecutor(agent=self, router=self._router, coordinator=self._coordinator, agent_db=self._agent_db)
"""
src = src.replace("        self._edit_applier = EditApplier()\n", init_code)

# Add setter delegations in DevAgent setters
# set_indexer
src = src.replace(
    "    def set_indexer(self, indexer: \"CodebaseIndexer\") -> None:\n        self._indexer = indexer\n",
    "    def set_indexer(self, indexer: \"CodebaseIndexer\") -> None:\n        self._indexer = indexer\n        self._context_builder.set_indexer(indexer)\n"
)
# set_bridge
src = src.replace(
    "    def set_bridge(self, bridge: \"BridgeClient\") -> None:\n        self._bridge = bridge\n",
    "    def set_bridge(self, bridge: \"BridgeClient\") -> None:\n        self._bridge = bridge\n        self._step_executor.set_bridge(bridge)\n"
)
# set_skill_registry
src = src.replace(
    "    def set_skill_registry(self, registry) -> None:\n        self._skill_registry = registry\n",
    "    def set_skill_registry(self, registry) -> None:\n        self._skill_registry = registry\n        self._step_executor.set_skill_registry(registry)\n"
)
# set_personal_kb
src = src.replace(
    "    def set_personal_kb(self, kb) -> None:\n        self._personal_kb = kb\n",
    "    def set_personal_kb(self, kb) -> None:\n        self._personal_kb = kb\n        self._step_executor.set_personal_kb(kb)\n"
)
# set_repo_root
src = src.replace(
    "    def set_repo_root(self, path: str) -> bool:\n",
    "    def set_repo_root(self, path: str) -> bool:\n        self._context_builder.set_repo_root(path)\n        self._step_executor.set_repo_root(path)\n"
)

# Append __getattr__ to DevAgent
getattr_code = """
    def __getattr__(self, name):
        # Prevent recursion if these aren't set yet during __init__
        if name in ('_step_executor', '_saga_manager', '_context_builder'):
            raise AttributeError(name)
        for delegate in (self._step_executor, self._saga_manager, self._context_builder):
            if hasattr(delegate, name):
                return getattr(delegate, name)
        raise AttributeError(f"'DevAgent' object has no attribute '{name}'")
"""
src += getattr_code

with open('inference/dev_agent.py', 'w', encoding='utf8') as f:
    f.write(src)

print("Patched DevAgent with delegates and __getattr__")
