from __future__ import annotations

from agents import synthesis_agent as sa


def assessment(*, independent=2, layers=2, positive=0, negative=2):
    return {
        "independent_evidence_units": independent,
        "research_evidence_layers": layers,
        "positive_signal_count": positive,
        "negative_signal_count": negative,
    }


def evidence():
    return [
        {"excerpt": "The method fails under high load and remains a bottleneck."},
        {"excerpt": "The limitation is unresolved in current evaluations."},
    ]


def test_clear_gap_is_decided_without_llm(monkeypatch):
    def fail_llm(_prompt):
        raise AssertionError("LLM should not be called for a clear case")

    monkeypatch.setattr(sa, "llm_generate", fail_llm)
    result = sa._synthesis_decision(assessment(), problem="verification scalability", evidence_inputs=evidence())
    assert result["decision"] == "provisional_gap_supported"
    assert result["llm_review"]["used"] is False


def test_ambiguous_gap_uses_llm(monkeypatch):
    calls = []

    def fake_llm(prompt):
        calls.append(prompt)
        return '{"gap_supported": true, "confidence": 0.88, "status": "supported", "reason": "Independent evidence supports the limitation."}'

    monkeypatch.setattr(sa, "llm_generate", fake_llm)
    result = sa._synthesis_decision(
        assessment(independent=1, layers=1, positive=1, negative=1),
        problem="verification scalability",
        evidence_inputs=evidence(),
    )
    assert result["decision"] == "provisional_gap_supported"
    assert result["llm_review"]["used"] is True
    assert calls and "verification scalability" in calls[0]


def test_ambiguous_gap_llm_rejection_blocks_gap(monkeypatch):
    monkeypatch.setattr(
        sa,
        "llm_generate",
        lambda prompt: '{"gap_supported": false, "confidence": 0.91, "status": "contested", "reason": "Existing solutions address the limitation."}',
    )
    result = sa._synthesis_decision(
        assessment(independent=1, layers=1, positive=2, negative=1),
        problem="verification scalability",
        evidence_inputs=evidence(),
    )
    assert result["decision"] == "contested_gap"
    assert result["gap_supported"] is False


def test_llm_unavailable_marks_ambiguity_uncertain(monkeypatch):
    def unavailable(_prompt):
        raise RuntimeError("Ollama unavailable")

    monkeypatch.setattr(sa, "llm_generate", unavailable)
    result = sa._synthesis_decision(
        assessment(independent=1, layers=1, positive=1, negative=1),
        problem="verification scalability",
        evidence_inputs=evidence(),
    )
    assert result["decision"] == "uncertain"
    assert result["gap_supported"] is False
    assert result["llm_review"]["available"] is False

def _cross_cluster_fixture(
    cluster_id: str,
    problem: str,
    top_terms: list[str],
    *,
    area: str = "Unscoped",
    funding_call_id: str = "CALL-A",
) -> dict:
    return {
        "cluster_id": cluster_id,
        "area": area,
        "representative_problem": problem,
        "supporting_layers": ["Literature"],
        "signal_types": ["general_signal"],
        "evidence_types": ["web"],
        "actors": [],
        "top_terms": top_terms,
        "members": [{"record_id": cluster_id}],
        "evidence_links": [f"https://example.org/{cluster_id}"],
        "funding_call_id": funding_call_id,
    }


def test_cross_cluster_groups_keep_unrelated_topics_separate():
    clusters = [
        _cross_cluster_fixture(
            "gender-gap",
            "Gender gap in educational attainment and participation",
            ["gender", "education", "attainment", "participation", "research", "problem"],
        ),
        _cross_cluster_fixture(
            "cyber-warfare",
            "Cyber warfare anomaly detection in military networks",
            ["cyber", "warfare", "anomaly", "detection", "military", "networks", "research", "problem"],
        ),
        _cross_cluster_fixture(
            "financial-literacy",
            "Financial literacy and household budgeting behavior",
            ["financial", "literacy", "household", "budgeting", "behavior", "research", "problem"],
        ),
    ]

    groups = sa.build_cross_cluster_groups(clusters, max_groups=12, group_size=4)

    assert len(groups) == 3
    assert all(len(group["source_cluster_ids"]) == 1 for group in groups)
    assert {group["source_cluster_ids"][0] for group in groups} == {
        "gender-gap", "cyber-warfare", "financial-literacy"
    }
    assert {link for group in groups for link in group["evidence_links"]} == {
        "https://example.org/gender-gap",
        "https://example.org/cyber-warfare",
        "https://example.org/financial-literacy",
    }


def test_cross_cluster_groups_combine_related_problems_with_substantive_overlap():
    clusters = [
        _cross_cluster_fixture(
            "grid-telemetry-a",
            "Privacy-preserving telemetry anomaly detection for smart grids",
            ["privacy", "telemetry", "anomaly", "detection", "smart", "grids"],
        ),
        _cross_cluster_fixture(
            "grid-telemetry-b",
            "Privacy-aware anomaly detection from smart-grid telemetry",
            ["privacy", "telemetry", "anomaly", "detection", "smart", "grid"],
        ),
        _cross_cluster_fixture(
            "financial-literacy",
            "Financial literacy and household budgeting behavior",
            ["financial", "literacy", "household", "budgeting", "behavior"],
        ),
    ]

    groups = sa.build_cross_cluster_groups(clusters, max_groups=12, group_size=4)

    related_groups = [group for group in groups if len(group["source_cluster_ids"]) > 1]
    assert len(related_groups) == 1
    assert set(related_groups[0]["source_cluster_ids"]) == {
        "grid-telemetry-a", "grid-telemetry-b"
    }
    assert len(groups) == 2


def test_cross_cluster_groups_do_not_mix_research_areas_or_funding_calls():
    clusters = [
        _cross_cluster_fixture(
            "area-a", "Privacy telemetry anomaly detection for smart grids",
            ["privacy", "telemetry", "anomaly", "detection", "smart", "grids"],
            area="Unscoped", funding_call_id="CALL-A",
        ),
        _cross_cluster_fixture(
            "area-b", "Privacy-aware anomaly detection from smart-grid telemetry",
            ["privacy", "telemetry", "anomaly", "detection", "smart", "grid"],
            area="RWA", funding_call_id="CALL-A",
        ),
        _cross_cluster_fixture(
            "call-b", "Privacy-preserving anomaly detection for smart-grid telemetry",
            ["privacy", "telemetry", "anomaly", "detection", "smart", "grids"],
            area="Unscoped", funding_call_id="CALL-B",
        ),
    ]

    groups = sa.build_cross_cluster_groups(clusters, max_groups=12, group_size=4)

    assert len(groups) == 3
    assert all(len(group["source_cluster_ids"]) == 1 for group in groups)
