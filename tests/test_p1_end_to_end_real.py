import pytest
import os
from pathlib import Path
from core.agent_interface import build_intermediate_artifact_payload
from core.normalization import save_normalized_collection_records, build_normalized_collection_records
from core.intelligence import refresh_intelligence_views, load_manifest_entries
from core.promotion import apply_promotion_overrides
from agents.proposal_agent import _extract_and_check_claims

def test_p1_full_pipeline_integration(tmp_path: Path, monkeypatch):
    import core.problem_extraction
    def mock_extract(text, area):
        return {"problem": text[:150], "problem_type": "identified_limitation", "confidence": "Medium"}
    monkeypatch.setattr("core.problem_extraction._refine_with_llm", lambda p, e: p)
    monkeypatch.setattr("agents.literature_agent.run_agent", lambda m, a, i: {"problem": "...", "problem_intelligence": mock_extract(i["content"], a)})
    # Wait, build_intermediate_artifact_payload just runs _extract_problem... Let's just mock _extract_problem
    def mock_extract_full(text, title, bundle):
        return {
            "selected_problem": text, "problem_type": "identified_limitation",
            "problem_evidence": [text], "problem_confidence": "Medium",
            "intelligence_mode": "deterministic"
        }
    monkeypatch.setattr("core.agent_interface.extract_problem_intelligence", mock_extract_full)

    def mock_run_agent(agent_name, mode, area=None, input_data=None):
        if agent_name == "synthesis":
            return {
                "problem": "Synthesized problem from cluster",
                "idea": "Synthesized idea",
                "research_gap": "Remaining gap",
                "confidence": "Medium",
                "synthesized_file": "cluster_123.md"
            }
        return {}
    monkeypatch.setattr("core.agent_registry.run_registered_agent", mock_run_agent)
    monkeypatch.setattr("core.llm_provider.generate", lambda p, m=None: "Mocked LLM Response")

    outputs_root = tmp_path / "outputs"
    intermediate_dir = outputs_root / "intermediate"
    intermediate_dir.mkdir(parents=True)
    
    from tests.test_batch4_normalization import make_artifact_md, create_artifact
    
    def make_artifact_with_area(*args, **kwargs):
        md = make_artifact_md(*args, **kwargs)
        return md.replace("- Layer: Literature", "- Layer: Literature\n- Research Area: ZK-IoV")

    # 1. Collection Output
    content1 = "The fundamental challenge in proof generation is that verification latency becomes impractical above 100K transactions."
    create_artifact(outputs_root, "a.md", make_artifact_with_area(
        title="Paper A", source="http://paper-a", problem="verification latency becomes impractical above 100K transactions.", evidence_snippets=[content1]
    ))
    
    content2 = "High transaction throughput bottleneck causes verification latency increases beyond acceptable bounds."
    create_artifact(outputs_root, "b.md", make_artifact_with_area(
        title="Repo B", source="http://repo-b", problem="High transaction throughput bottleneck causes verification latency increases beyond acceptable bounds.", evidence_snippets=[content2]
    ))
    
    content3 = "Latency remains stable only when limited to permissioned nodes, resolving the bottleneck."
    create_artifact(outputs_root, "c.md", make_artifact_with_area(
        title="Company C", source="http://company-c", problem="Latency remains stable only when limited to permissioned nodes, resolving the bottleneck.", evidence_snippets=[content3]
    ))
    
    # 2. Normalization
    save_normalized_collection_records(outputs_root, outputs_root)
    # The outputs_root/synthesis directory is populated by refresh_intelligence_views
    
    # 3. Clustering first; synthesis is a separate production stage.
    refresh_intelligence_views(outputs_root)

    from agents.synthesis_agent import run_agent as run_synthesis_agent
    synthesis_result = run_synthesis_agent(
        "configured_scan",
        area="ZK-IoV",
        input_data={
            "outputs_root": str(outputs_root),
            "output_dir": str(outputs_root / "synthesis" / "generated"),
            "manifest_dir": str(outputs_root / "synthesis" / "manifest"),
        },
    )
    assert synthesis_result["status"] in {"success", "partial_success"}
    assert synthesis_result.get("items_saved", 0) >= 1

    # Recompute intelligence views now that synthesis artifacts exist.
    refresh_intelligence_views(outputs_root)

    manifest_dir = outputs_root / "synthesis" / "manifest"
    entries = load_manifest_entries(manifest_dir)
    assert len(entries) >= 1
    
    # Find the synthesized cluster
    entry = entries[0]
    
    # Verify ResearchCandidate elements made it to the entry dict via build_synthesis_payload & promotion
    assert entry.get("promotion") is not None
    assert entry["promotion"]["status"] in {
        "CANDIDATE_PROBLEM",
        "CORROBORATED_PROBLEM",
        "RESEARCH_CANDIDATE",
        "HIGH_PRIORITY_RESEARCH_CANDIDATE",
    }
    
    # Check contradiction / evidence assessment exists for the synthesized candidate.
    # The fixture is intentionally deterministic and may form a one-record synthesis
    # cluster depending on clustering thresholds; corroboration is tested separately.
    assessment = entry.get("evidence_assessment", {})
    assert assessment.get("evidence_input_count", 0) >= 1
    
    # Exercise the novelty-search stage explicitly; the refresh view only
    # refreshes rankings/views and does not create a novelty field itself.
    from core.novelty_search import perform_novelty_search
    novelty = perform_novelty_search(entry.get("problem", ""), use_external=False)
    assert novelty.get("novelty_status") in {"NO_RESULT", "UNKNOWN", "WEAK_OVERLAP", "STRONG_OVERLAP", "POTENTIAL_GAP", "NO_SEARCH"}
    
    # 4. Proposal Validation
    proposal_payload = {
        "problem": entry.get("problem"),
        "source": "http://paper-a",
        "evidence": content1,
        "proposed_method": "Use a new sharding method.",
        "evaluation_plan": "..."
    }
    claim_res = _extract_and_check_claims(proposal_payload)
    assert claim_res["unsupported_claim_flag"] is False
