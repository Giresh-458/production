from pathlib import Path


def test_literature_unscoped_has_safe_output_label():
    from agents import literature_agent
    assert "effective_area = area or \"Unscoped\"" in Path(literature_agent.__file__).read_text()


def test_local_novelty_never_claims_no_overlap():
    from core.novelty_search import LocalKnowledgeBaseSearcher
    result = LocalKnowledgeBaseSearcher().search("some research problem")
    assert result["novelty_status"] == "NO_RESULT"
    assert result["search_coverage"] == 0.0
    assert result["search_mode"] == "local"


def test_cross_cluster_groups_are_bounded_and_preserve_evidence():
    from agents.synthesis_agent import build_cross_cluster_groups
    clusters = []
    for i in range(20):
        clusters.append({
            "cluster_id": f"c{i}", "area": "Unscoped",
            "representative_problem": f"privacy telemetry problem {i}",
            "supporting_layers": ["Literature" if i % 2 else "Data"],
            "signal_types": ["gap"], "evidence_types": ["web"],
            "actors": [], "top_terms": ["privacy", "telemetry"],
            "members": [{"id": i}], "evidence_links": [f"https://example.org/{i}"],
        })
    groups = build_cross_cluster_groups(clusters, max_groups=12, group_size=4)
    assert 1 <= len(groups) <= 12
    links = {u for g in groups for u in g["evidence_links"]}
    assert len(links) == 20
