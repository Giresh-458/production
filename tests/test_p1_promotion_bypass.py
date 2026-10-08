import pytest
from core.promotion import compute_promotion_decision

def test_anti_score_bypass():
    # Candidate with very high numerical score, one weak source, no corroboration, no gap
    entry_bypass = {
        "ranking": {"breakdown": {"specificity": 10, "source_trust": 1, "prototype_feasibility": 10}}, # High score components
        "independent_source_count": 1,
        "layers_covered": ["Literature"],
        "source_urls": ["weak-url-1"],
        "novelty_status": "STRONG_OVERLAP" # no gap
    }
    res_bypass = compute_promotion_decision(entry_bypass, {})
    assert res_bypass["status"] in {"CANDIDATE_PROBLEM", "SIGNAL", "watchlist", "hold"} # anything below RESEARCH_CANDIDATE
    
    # Candidate with strong evidence, two independent sources, cross-layer support, meaningful gap, feasible eval
    entry_strong = {
        "ranking": {"breakdown": {"specificity": 8, "source_trust": 8, "prototype_feasibility": 8}},
        "independent_source_count": 2,
        "layers_covered": ["Literature", "OpenSource"],
        "source_urls": ["url1", "url2"],
        "novelty_status": "POTENTIAL_GAP"
    }
    res_strong = compute_promotion_decision(entry_strong, {})
    assert res_strong["status"] in {"RESEARCH_CANDIDATE", "HIGH_PRIORITY_RESEARCH_CANDIDATE"}
