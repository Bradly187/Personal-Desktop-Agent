import ast
import os

src = open('inference/dev_agent.py', encoding='utf8').read().splitlines()
tree = ast.parse('\n'.join(src))

to_delete = {
    # Module level
    'AgentStep', 'AgentResult', 'DroppedStep', 'PlanParseReport',
    '_parse_deps', '_extract_json_obj', '_parse_plan_json_report',
    '_parse_plan_json', '_build_plan_repair_prompt', '_parse_plan',
    
    # DevAgent methods
    '_session_seed_context', '_git_context', '_push_context', '_format_context',
    '_workspace_context', 'invalidate_workspace_context', '_rag_context',
    
    '_start_run', '_saga_dir', '_snapshot_for_write', '_saga_git_backend_enabled',
    '_git_blob_snapshot', '_git_cat_blob', '_compensation_for', '_pre_register_step',
    '_persist_step', '_halt_and_compensate', '_run_compensations', '_restore_file',
    '_finalize_run', 'revert_last_run', '_escalation_sidecar', '_record_escalation',
    '_append_escalation_sidecar', 'reconcile_pending_escalations', '_read_escalation_sidecar',
    '_rewrite_escalation_sidecar',
    
    '_execute_step', '_parse_skill_args', '_build_skill_args', '_execute_skill_step',
    '_audit_skill', '_verify_math_with_cas', '_extract_cas_check', '_handle_skill',
    '_handle_personal_query', '_apply_edit', '_diff_for_confirm', '_write_file',
    '_read_file', '_grep', '_run_terminal', '_git_status', '_git_diff', '_git_commit',
    '_git_checkout', '_github_pr', '_fetch_url', '_scan_web_content', '_capture_screenshot',
    '_parse_accessibility_params', '_confirm_destructive_op', '_confirm_destructive_op_locked'
}

# Also remove variables _PLAN_ACTIONS, _STEP_PATTERN, _DELEGATE_PROMPT_INSTRUCTIONS
# We will identify nodes to remove
nodes_to_remove = []

for node in tree.body:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in to_delete:
            nodes_to_remove.append(node)
        elif node.name == 'DevAgent':
            for m in node.body:
                if getattr(m, 'name', '') in to_delete:
                    nodes_to_remove.append(m)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('_PLAN_ACTIONS', '_STEP_PATTERN', '_DELEGATE_PROMPT_INSTRUCTIONS'):
                nodes_to_remove.append(node)

# Collect line ranges to delete
lines_to_delete = set()
for node in nodes_to_remove:
    # include decorators if any
    start = node.lineno - 1
    if hasattr(node, 'decorator_list') and node.decorator_list:
        start = node.decorator_list[0].lineno - 1
    for i in range(start, node.end_lineno):
        lines_to_delete.add(i)

# Rebuild src without deleted lines
new_src = []
for i, line in enumerate(src):
    if i not in lines_to_delete:
        new_src.append(line)

# Add imports for new modules at the top
imports = [
    "from inference.plan_parser import AgentStep, AgentResult, DroppedStep, PlanParseReport, _parse_plan_json_report, _parse_plan, _build_plan_repair_prompt",
    "from inference.context_builder import ContextBuilder",
    "from inference.saga_manager import SagaManager",
    "from inference.step_executor import StepExecutor"
]

# Insert after the last import
insert_idx = 0
for i, line in enumerate(new_src):
    if line.startswith('import ') or line.startswith('from '):
        insert_idx = i + 1

new_src = new_src[:insert_idx] + imports + new_src[insert_idx:]

with open('inference/dev_agent.py', 'w', encoding='utf8') as f:
    f.write('\n'.join(new_src))

print(f"Deleted {len(lines_to_delete)} lines from dev_agent.py")
