from agents.funding_agent import FundingDocument, analyze_document
from core.schemas import ALLOWED_RESEARCH_AREAS
from run_pipeline import select_collection_areas


def _doc(text: str) -> FundingDocument:
    return FundingDocument(
        title="Domain Test Call",
        organization="Test",
        source="Manual",
        source_type="Manual",
        content=text,
        year=2026,
    )


def test_funding_agent_detects_all_eight_canonical_domains():
    cases = {
        "RWA": "tokenized real-world asset infrastructure and oracle verification for tokenized securities",
        "ESG": "carbon credit MRV, emissions measurement, sustainability registry and climate reporting",
        "ZK-IoV": "zero-knowledge proofs for V2X internet of vehicles and privacy-preserving vehicular communication",
        "DID": "decentralized identity wallets and verifiable credentials with attestation",
        "DePIN": "decentralized physical infrastructure, wireless sensor networks and distributed compute",
        "MEV": "transaction ordering, block builders, relays and MEV mitigation",
        "Stablecoins": "stablecoin payment settlement, reserve management and depeg controls",
        "DigitalHealthCPS": "digital healthcare cyber-physical systems, medical devices, clinical validation and remote monitoring",
    }
    assert ALLOWED_RESEARCH_AREAS == set(cases)
    for expected, text in cases.items():
        analysis = analyze_document(_doc(text))
        assert analysis.focus_area == expected
        assert analysis.research_context["domain"] == expected
        assert expected in analysis.research_context["selected_domains"]


def test_selected_collection_areas_use_material_relevance_only():
    ranking = [
        {"domain": "DigitalHealthCPS", "score": 0.76},
        {"domain": "DID", "score": 0.14},
        {"domain": "RWA", "score": 0.03},
        {"domain": "ESG", "score": 0.02},
    ]
    assert select_collection_areas({"domain_relevance": ranking}, "DigitalHealthCPS") == ["DigitalHealthCPS", "DID"]
