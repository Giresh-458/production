import os
import sys
import json
from pathlib import Path
from core.agent_registry import run_registered_agent
from core.source_registry import current_force_source_refresh
from core.shared_crawl_manager import SharedCrawlManager
from core.funding_selection import FundingCallContext

def run_pipeline():
    area = "RWA"
    root = Path("outputs")
    
    # 1. Force refresh
    current_force_source_refresh.set(True)
    
    # Reset stats
    mgr = SharedCrawlManager.get()
    mgr.stats_cache_hits = 0
    mgr.stats_actual_fetches = 0
    mgr.stats_in_flight_dedupes = 0
    
    # 2. Funding Agent (Discover Funding Calls)
    print("Running Funding Agent to discover calls...")
    run_registered_agent("funding", "configured_scan", area=area, input_data={"output_dir": str(root / "intermediate" / "funding"), "force_refresh": True})
    
    # Use real production logic to rank and select the best funding context
    from core.funding_selection import execute_selection
    from core.funding_selection import ApplicantProfile
    db_path = root / "funding.db"
    
    selection_payload = execute_selection("autonomous", ApplicantProfile(), db_path=db_path)
    
    if not selection_payload.get("selected_funding_call_id"):
        print("NO_VALID_FUNDING_CONTEXT")
        sys.exit(1)
        
    target_context = FundingCallContext.from_dict(selection_payload)
    funding_contexts = [target_context]
    
    print(f"Selected Funding Call: {target_context.funding_call_id}")
    print(f"Propagated attributes: Amount={target_context.funding_amount}, Deadline={target_context.deadline}")

    # 3. Rest of Collectors (ALL 11 downstream)
    print("Running all other collectors...")
    collectors = [
        "lab", "literature", "company", "regulation", "opensource",
        "practitioner", "investment", "failure", "data_availability",
        "hackathon", "expert"
    ]
    for collector in collectors:
        print(f"  -> {collector}")
        resp = run_registered_agent(collector, "configured_scan", area=area, input_data={"output_dir": str(root / "intermediate" / collector), "force_refresh": True}, funding_contexts=funding_contexts)
        print(f"     status: {resp.get('status')} errors: {resp.get('errors', [])}")
    
    print("Running Normalization...")
    from core.normalization import save_normalized_collection_records
    save_normalized_collection_records(root, root)
    
    print("Running Processing...")
    for proc in ["tagging", "clustering", "trend"]:
        resp = run_registered_agent(proc, "configured_scan", area=area, input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / f"{proc}s")}, funding_context=target_context)
        print(f"  -> {proc}: {resp.get('status')}")
    
    print("Running Synthesis...")
    resp = run_registered_agent("synthesis", "configured_scan", area=area, input_data={"outputs_root": str(root), "output_dir": str(root / "synthesis" / "generated"), "manifest_dir": str(root / "synthesis" / "manifest")}, funding_context=target_context)
    print(f"  -> synthesis: {resp.get('status')}")
    
    print("Running Intelligence (Idea + Proposal)...")
    idea = run_registered_agent("idea", "configured_scan", area=area, input_data={"output_dir": str(root / "ideas"), "manifest_dir": str(root / "synthesis" / "manifest"), "external_novelty_validation": True}, funding_context=target_context)
    print(f"  -> idea: {idea.get('status')} | {len(idea.get('outputs', []))} outputs")
    
    proposal = run_registered_agent("proposal", "configured_scan", area=area, input_data={"output_dir": str(root / "proposals"), "idea_dir": str(root / "ideas")}, funding_context=target_context)
    print(f"  -> proposal: {proposal.get('status')} | {len(proposal.get('outputs', []))} outputs")
    
    stats = {
        "cache_hits": mgr.stats_cache_hits,
        "actual_fetches": mgr.stats_actual_fetches,
        "in_flight_deduplications": mgr.stats_in_flight_dedupes
    }
    
    with open("pipeline_stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
        
    print("Pipeline complete!")
    print(json.dumps(stats, indent=2))

if __name__ == '__main__':
    run_pipeline()
