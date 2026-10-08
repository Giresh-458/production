from core.schemas import rank_research_areas

def test_weak_single_token_score():
    # C. Weak single-token score
    text = "A paper about health"
    ranked = rank_research_areas(text)
    
    health_domain = next((r for r in ranked if r["domain"] == "DigitalHealthCPS"), None)
    assert health_domain is not None
    
    # Weight of "health" is 0.7. So total = 0.7.
    # normalization_floor = max(4.0, 0.7) = 4.0
    # score = 0.7 / 4.0 = 0.175
    assert health_domain["raw_score"] == 0.7, "Raw score should remain unchanged"
    assert health_domain["score"] < 0.30, "Score should be below 0.30 for weak token"
    assert health_domain["score"] == 0.175, "Score should be specifically normalized to max(4.0, total)"

def test_existing_canonical_routing_compatibility():
    # F. Existing canonical routing compatibility
    # Genuine technical evidence, raw_score >= 4.0
    text = "Research on decentralized physical infrastructure and compute network design and storage network depin"
    # depin: decentralized physical infrastructure (3.0), compute network (2.5), storage network (2.5), depin (3.0) -> total 11.0
    ranked = rank_research_areas(text)
    
    depin_domain = next((r for r in ranked if r["domain"] == "DePIN"), None)
    assert depin_domain is not None
    
    assert depin_domain["raw_score"] >= 4.0, "Raw score must be >= 4.0"
    
    # Since total > 4.0, normalization_floor = total, so the score should be 1.0 (if no other domains match)
    assert depin_domain["score"] > 0.8, "Canonical routing scores must retain existing relative behavior"
