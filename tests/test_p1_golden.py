import pytest
from core.problem_extraction import _validate_refinement
from core.semantic_retrieval import compute_cluster_score
from core.novelty_search import perform_novelty_search
from core.promotion import compute_promotion_decision
from agents.proposal_agent import _extract_and_check_claims

# 2. LLM refinement needs a stronger adversarial test
def test_p1_golden_case_c_unsupported_facts_rejection():
    # Adding unsupported industry/domain
    evidence1 = "Verification latency exceeds 2 seconds above 100K transactions."
    refined1 = "Healthcare payment verification becomes unsafe above 100K transactions."
    assert _validate_refinement(refined1, evidence1) == False

    # Adding unsupported metric
    evidence2 = "System latency increases beyond 100K transactions."
    refined2 = "The architecture has a 2-second latency SLA."
    assert _validate_refinement(refined2, evidence2) == False
    
    # Valid compression
    evidence3 = "System latency increases beyond 100K transactions."
    refined3 = "Latency rises over 100K transactions."
    assert _validate_refinement(refined3, evidence3) == True

    # Valid abstraction / semantic paraphrase
    evidence4 = "Verification latency exceeds acceptable limits beyond 100K transactions."
    refined4 = "High transaction volumes create a verification-latency bottleneck."
    assert _validate_refinement(refined4, evidence4) == True

    # Factual expansion
    refined5 = "High transaction volumes make verification unsafe for medical payments."
    assert _validate_refinement(refined5, evidence4) == False

# 4. Novelty search must clearly distinguish screening from validation
def test_p1_golden_case_d_novelty_status_logic_local():
    res = perform_novelty_search("test problem", use_external=False)
    assert res["search_mode"] == "local"
    assert res["novelty_status"] == "NO_RESULT"

def test_p1_golden_case_e_novelty_status_logic_external():
    res = perform_novelty_search("random unknown problem", use_external=True)
    assert res["search_mode"] in {"external", "unavailable"}
    assert res["novelty_status"] != "novel" # Should never be converted to novel

# 6. Semantic clustering needs an actual comparison test
def test_p1_golden_case_f_semantic_clustering():
    text1_a = "Verification becomes too slow when transaction throughput increases."
    text1_b = "High-volume transaction verification cannot maintain acceptable latency."
    score1 = compute_cluster_score(text1_a, "h1a", "limitation", "infra", text1_b, "h1b", "limitation", "infra")
    
    text2_a = "Verification becomes too slow at high transaction volume."
    text2_b = "Credential revocation interoperability remains inconsistent across issuers."
    score2 = compute_cluster_score(text2_a, "h2a", "limitation", "infra", text2_b, "h2b", "limitation", "identity")
    
    assert score1 > score2
    assert score1 > 0.6
    assert score2 < 0.4

# 11. Promotion rules need boundary tests
def test_p1_golden_case_g_promotion_rules():
    # Case A: One weak source -> CANDIDATE_PROBLEM
    entry_a = {
        "ranking": {"breakdown": {"specificity": 5, "source_trust": 5, "prototype_feasibility": 5}},
        "independent_source_count": 1,
        "source_urls": ["url1"]
    }
    res_a = compute_promotion_decision(entry_a, {})
    assert res_a["status"] == "CANDIDATE_PROBLEM"

    # Case B: Two copied versions of one source -> NOT corroborated
    entry_b = {
        "ranking": {"breakdown": {"specificity": 5}},
        "independent_source_count": 1,
        "source_urls": ["url1", "url2"] # but they map to 1 independent source
    }
    res_b = compute_promotion_decision(entry_b, {})
    assert res_b["status"] == "CANDIDATE_PROBLEM"

    # Case C: Two independent credible sources -> CORROBORATED_PROBLEM
    entry_c = {
        "ranking": {"breakdown": {"specificity": 5, "prototype_feasibility": 2}},
        "independent_source_count": 2,
        "source_urls": ["url1", "url2"]
    }
    res_c = compute_promotion_decision(entry_c, {})
    assert res_c["status"] == "CORROBORATED_PROBLEM"

    # Case D: Strong corroboration but no meaningful remaining gap -> NOT RESEARCH_CANDIDATE
    entry_d = {
        "ranking": {"breakdown": {"specificity": 5, "prototype_feasibility": 5}},
        "independent_source_count": 2,
        "source_urls": ["url1", "url2"],
        "novelty_status": "STRONG_OVERLAP" # no unresolved gap
    }
    res_d = compute_promotion_decision(entry_d, {})
    assert res_d["status"] == "CORROBORATED_PROBLEM"

    # Case E: Strong evidence + unresolved gap + feasible evaluation -> RESEARCH_CANDIDATE
    entry_e = {
        "ranking": {"breakdown": {"specificity": 5, "prototype_feasibility": 5, "source_trust": 5}},
        "independent_source_count": 2,
        "source_urls": ["url1", "url2"],
        "novelty_status": "POTENTIAL_GAP",
        "layers_covered": ["layer1"] # only 1 layer, so not high priority
    }
    res_e = compute_promotion_decision(entry_e, {})
    assert res_e["status"] == "RESEARCH_CANDIDATE"

    # Case F: Strong evidence + cross-layer corroboration + feasible evaluation + meaningful remaining gap -> HIGH_PRIORITY_RESEARCH_CANDIDATE
    entry_f = {
        "ranking": {"breakdown": {"specificity": 5, "prototype_feasibility": 8, "source_trust": 8}},
        "independent_source_count": 2,
        "source_urls": ["url1", "url2"],
        "novelty_status": "POTENTIAL_GAP",
        "layers_covered": ["layer1", "layer2"]
    }
    res_f = compute_promotion_decision(entry_f, {})
    assert res_f["status"] == "HIGH_PRIORITY_RESEARCH_CANDIDATE"

# 12 & 13. Proposal unsupported-claim handling needs to be a HARD acceptance gate
def test_p1_golden_case_h_proposal_gate():
    # Test 1: Unsupported evidence claim (no source)
    payload = {
        "problem": "Latency is high",
        "source": "", # missing source!
        "evidence": "Latency is high at large transaction volumes.",
        "proposed_method": "Fix it",
        "evaluation_plan": "Test it"
    }
    res = _extract_and_check_claims(payload)
    assert res["unsupported_claim_flag"] == True
    assert res.get("validation", {}).get("status") == "needs_review"

    # Test 2: Valid source but adversarial claim (unsupported facts)
    payload2 = {
        "problem": "Hospital patients experience unsafe delays due to the system.",
        "source": "http://source.com",
        "evidence": "Latency is high at large transaction volumes.",
        "proposed_method": "Fix it",
        "evaluation_plan": "Test it"
    }
    res2 = _extract_and_check_claims(payload2)
    assert res2["unsupported_claim_flag"] == True
    assert res2.get("validation", {}).get("status") == "needs_review"

    # Test 3: Valid source and supported claim
    payload3 = {
        "problem": "High transaction volume is associated with increased latency.",
        "source": "http://source.com",
        "evidence": "Latency is high at large transaction volumes.",
        "proposed_method": "Fix it",
        "evaluation_plan": "Test it"
    }
    res3 = _extract_and_check_claims(payload3)
    assert res3["unsupported_claim_flag"] == False

