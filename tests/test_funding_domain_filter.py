from datetime import UTC, datetime, timedelta

from run_pipeline import get_all_open_funding_calls


def _candidate(*, domain="DePIN", term="depin", source_domains=None, raw_score=3.0):
    deadline = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    record = {
        "call_id": f"CALL-{domain}",
        "title": f"{domain} Research Innovation Challenge",
        "program_name": f"{domain} Research Innovation Challenge",
        "status": "OPEN_CALL",
        "deadline": deadline,
        "funding_amount": "$25,000",
        "eligibility": "Eligible research organizations",
        "source_url": "https://funding.example.org/calls/innovation",
        "evidence": {
            "source_status": "posted",
            "source_status_provenance": "test fixture",
        },
        "domain_relevance": [{
            "domain": domain,
            "score": 0.75,
            "raw_score": raw_score,
            "matched_terms": [term],
        }],
    }
    if source_domains is not None:
        record["source_research_domains"] = source_domains
    return {"outputs": [record]}


def test_strict_funding_gate_accepts_strong_call_domain_matching_its_source():
    selected, rejected = get_all_open_funding_calls(
        _candidate(source_domains=["DePIN"]), require_domain_match=True
    )

    assert len(selected) == 1
    assert selected[0]["matched_rif_domains"] == ["DePIN"]
    assert rejected == []


def test_strict_funding_gate_rejects_generic_keywords_without_strong_domain_evidence():
    selected, rejected = get_all_open_funding_calls(
        _candidate(term="iot", raw_score=0.8, source_domains=["DePIN"]),
        require_domain_match=True,
    )

    assert selected == []
    assert len(rejected) == 1
    assert "domain-specific evidence" in rejected[0]["reasons"][0]


def test_strict_funding_gate_rejects_call_when_detected_domain_conflicts_with_source_mapping():
    selected, rejected = get_all_open_funding_calls(
        _candidate(domain="DePIN", term="depin", source_domains=["ESG"]),
        require_domain_match=True,
    )

    assert selected == []
    assert len(rejected) == 1
    assert "does not match the configured source" in rejected[0]["reasons"][0]


def test_strict_funding_gate_rejects_configured_source_without_domain_mapping():
    selected, rejected = get_all_open_funding_calls(
        _candidate(source_domains=[]), require_domain_match=True
    )

    assert selected == []
    assert len(rejected) == 1
    assert "no canonical RIF research-domain mapping" in rejected[0]["reasons"][0]


def test_manual_candidate_without_source_metadata_still_needs_strong_domain_evidence():
    selected, rejected = get_all_open_funding_calls(
        _candidate(source_domains=None), require_domain_match=True
    )

    assert len(selected) == 1
    assert selected[0]["matched_rif_domains"] == ["DePIN"]
    assert rejected == []
