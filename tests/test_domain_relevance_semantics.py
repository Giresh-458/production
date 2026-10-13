from core.schemas import rank_research_areas

def test_weak_single_token_score():
    # Generic IoT language should be a weak signal for DePIN, not sufficient evidence.
    text = "A paper about iot"
    ranked = rank_research_areas(text)

    depin_domain = next((r for r in ranked if r["domain"] == "DePIN"), None)
    assert depin_domain is not None

    # Weight of "iot" is 0.8, with a normalization floor of 4.0.
    assert depin_domain["raw_score"] == 0.8
    assert depin_domain["score"] < 0.30
    assert depin_domain["score"] == 0.2

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
