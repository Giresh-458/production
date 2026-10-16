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


def _domain(ranked, name):
    return next(item for item in ranked if item["domain"] == name)


def test_ordinary_english_did_does_not_score_as_decentralized_identity():
    ranked = rank_research_areas(
        "The company did submit its report and did deliver the project on time."
    )
    did = _domain(ranked, "DID")
    assert did["raw_score"] == 0
    assert did["matched_terms"] == []


def test_decentralized_identity_phrases_still_score_as_did():
    ranked = rank_research_areas(
        "Decentralized identity wallets issue verifiable credentials and support self-sovereign identity."
    )
    did = _domain(ranked, "DID")
    assert did["raw_score"] >= 3.0
    assert did["score"] > 0
    assert did["matched_terms"]


def test_ziov_requires_both_zero_knowledge_and_vehicle_network_evidence():
    crypto_only = rank_research_areas(
        "Zero-knowledge proofs improve cryptographic privacy and verification."
    )
    vehicle_only = rank_research_areas(
        "V2X communication supports connected-vehicle networks and vehicular safety."
    )
    combined = rank_research_areas(
        "Zero-knowledge proofs protect V2X messages in connected-vehicle networks."
    )

    assert _domain(crypto_only, "ZK-IoV")["raw_score"] == 0
    assert _domain(crypto_only, "ZK-IoV")["matched_terms"] == []
    assert _domain(vehicle_only, "ZK-IoV")["raw_score"] == 0
    assert _domain(vehicle_only, "ZK-IoV")["matched_terms"] == []
    assert _domain(combined, "ZK-IoV")["raw_score"] >= 2.5
    assert _domain(combined, "ZK-IoV")["matched_terms"]
