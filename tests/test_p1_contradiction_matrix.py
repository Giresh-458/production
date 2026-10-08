import pytest
from core.intelligence import detect_contradictions

def test_contradiction_comprehensive_matrix():
    def get_relationship(a_text, b_text):
        entries = [
            {"problem_statement": a_text, "source_url": "urlA"},
            {"problem_statement": b_text, "source_url": "urlB"}
        ]
        res = detect_contradictions(entries)
        if len(res["contradicting_evidence"]) > 0:
            return "CONTRADICTS"
        if len(res["qualifying_evidence"]) > 0:
            return "QUALIFIES"
        if len(res["unknown_evidence"]) > 0:
            return "UNKNOWN"
        return "SUPPORTING"

    # 1. Direct contradiction (polarity)
    assert get_relationship(
        "The system supports 100K transactions.",
        "The system cannot support 100K transactions."
    ) == "CONTRADICTS"

    # 2. Numerical contradiction
    assert get_relationship(
        "Latency remains below 100 ms.",
        "Latency exceeds 200 ms."
    ) == "CONTRADICTS"

    # 3. Qualification
    assert get_relationship(
        "Interoperability is unavailable.",
        "Interoperability is available for a limited set of issuers."
    ) == "QUALIFIES"
    
    # 4. Unrelated claims
    assert get_relationship(
        "The system lacks interoperability.",
        "The company raised a new funding round."
    ) == "UNKNOWN"

    # 5. Ambiguous claims (e.g. no polarity words matched) -> default to SUPPORTING if topics overlap but no explicit contradiction
    assert get_relationship(
        "The verification process happens in the node.",
        "The verification process runs locally."
    ) == "SUPPORTING"
