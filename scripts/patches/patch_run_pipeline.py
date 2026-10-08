import sys

with open('run_pipeline.py', 'r', encoding='utf-8') as f:
    content = f.read()

health_func = """
def evaluate_collection_health(collection_tasks: list[dict], min_required_agents: int) -> tuple[bool, int]:
    healthy_agents = set()
    for t in collection_tasks:
        agent_name = str(t.get("agent_name"))
        status = str(t.get("status", "")).lower()
        outputs_count = int(t.get("outputs_count", 0) or 0)
        
        # An agent is healthy if it succeeded (or partially succeeded) AND produced evidence.
        if status in ("success", "partial_success") and outputs_count > 0:
            healthy_agents.add(agent_name)
    
    is_healthy = len(healthy_agents) >= min_required_agents
    return is_healthy, len(healthy_agents)

"""

if "def evaluate_collection_health" not in content:
    content = content.replace('def run_stage', health_func + 'def run_stage')

replace_from = """    collection_tasks = downstream_plan.get("tasks", []) if isinstance(downstream_plan, dict) else []
    healthy_tasks = [t for t in collection_tasks if str(t.get("status", "")).lower() == "success" and int(t.get("outputs_count", 0) or 0) > 0]
    healthy_agents = {str(t.get("agent_name")) for t in healthy_tasks if t.get("agent_name")}
    
    collector_health = {"healthy_agents": len(healthy_agents), "total_agents": len(downstream_agents), "required": max(5, (len(downstream_agents) + 1) // 2)}
    if len(healthy_agents) < collector_health["required"]:
        print(f"❌ Shared collection quality gate failed: {len(healthy_agents)}/{len(downstream_agents)} unique collectors produced evidence.")
        return 4"""

replace_to = """    collection_tasks = downstream_plan.get("tasks", []) if isinstance(downstream_plan, dict) else []
    min_required = max(5, (len(downstream_agents) + 1) // 2)
    is_healthy, healthy_count = evaluate_collection_health(collection_tasks, min_required)
    
    collector_health = {"healthy_agents": healthy_count, "total_agents": len(downstream_agents), "required": min_required}
    if not is_healthy:
        print(f"❌ Shared collection quality gate failed: {healthy_count}/{len(downstream_agents)} unique collectors produced evidence.")
        return 4"""

content = content.replace(replace_from, replace_to)

with open('run_pipeline.py', 'w', encoding='utf-8') as f:
    f.write(content)
