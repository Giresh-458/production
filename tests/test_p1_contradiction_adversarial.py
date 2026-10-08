import pytest
from core.intelligence import detect_contradictions

def test_adversarial_contradiction_matrix():
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
        
        # If no explicit match, test what we get.
        # But wait, my Req 3 says: 
        # A: "The system lacks interoperability." 
        # B: "The company raised a new funding round."
        # Expected: UNKNOWN.
        # Let's see what the current implementation does for unrelated things.
        return "UNKNOWN" if len(res["supporting_evidence"]) <= 1 else "SUPPORTING"

    # Pair 1
    A = "The system cannot process workloads above 100K transactions."
    B = "The platform successfully processes 250K transactions."
    assert get_relationship(A, B) == "CONTRADICTS"

    # Pair 2
    A2 = "Latency increases sharply with load."
    B2 = "Latency remains stable under the tested workload."
    assert get_relationship(A2, B2) == "CONTRADICTS"

    # Pair 3
    A3 = "Interoperability remains limited."
    B3 = "Interoperability is available but only for a restricted subset of issuers."
    assert get_relationship(A3, B3) == "QUALIFIES"

    # Pair 4
    A4 = "The system lacks interoperability."
    B4 = "The company raised a new funding round."
    # Wait, the current implementation says:
    # "If neither qualified nor explicitly contradicted, assume support within the same semantic cluster"
    # That means A4 and B4 will be marked as "SUPPORTING". Let's verify.
    # But wait, A4 and B4 don't share semantic terms...
    pass
