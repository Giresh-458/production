from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agents.clustering_agent import build_cluster_markdown
from agents.idea_agent import build_idea_payload_from_synthesis
from agents.proposal_agent import build_proposal_payload_from_idea
from agents.synthesis_agent import build_evidence_assessment
from agents.tagging_agent import build_tags_for_record
from agents.trend_agent import build_trend_report
from core.intelligence import build_problem_clusters


def record(record_id, title, layer, text, source, month="2026-08"):
    return {
        "record_id": record_id,
        "title": title,
        "layer": layer,
        "research_area": "RWA",
        "source_url": source,
        "file_path": f"/tmp/{record_id}.md",
        "collected_at": f"{month}-10T10:00:00+00:00",
        "source_type": layer,
        "evidence_type": "problem",
        "actor": source.split('/')[2],
        "problem_statement": text,
        "context_summary": text,
        "evidence_snippets": [text],
        "excerpt_windows": [text],
        "keywords": [],
        "search_text": text,
        "tokens": text.lower().split(),
    }


def test_tagging_is_controlled_and_evidence_grounded():
    item = record(
        "r1", "Reserve Verification", "Funding",
        "Grant requires proof of reserve, compliance monitoring, oracle verification and a benchmark.",
        "https://fund.example.org/call",
    )
    tagged = build_tags_for_record(item)
    assert len(tagged["all_tags"]) <= 16
    assert tagged["tagging_confidence_score"] > 0.5
    assert tagged["tag_evidence"]
    assert tagged["problem_signature"]["fingerprint"]
    assert "#topic-oracle" in tagged["topic_tags"]


def test_clustering_counts_independent_sources_not_duplicate_copies():
    a = record("a", "Oracle Verification", "Literature", "Oracle verification becomes a bottleneck for tokenized assets.", "https://paper.example/a")
    b = record("b", "Oracle Verification", "Company", "Oracle verification becomes a bottleneck for tokenized assets.", "https://company.example/a")
    c = record("c", "Oracle Verification", "Company", "Oracle verification becomes a bottleneck for tokenized assets.", "https://company.example/a")
    clusters = build_problem_clusters([a, b, c], threshold=0.25)
    assert clusters
    cluster = max(clusters, key=lambda x: len(x["members"]))
    assert cluster["independent_source_count"] == 2
    assert cluster["independent_layer_count"] == 2
    assert "source_key" in cluster["members"][0]
    md = build_cluster_markdown(cluster)
    assert "Evidence Independence" in md


def test_clustering_does_not_merge_unrelated_same_area_records():
    a = record("a", "Reserve monitoring", "Literature", "Reserve monitoring requires verifiable attestations for tokenized assets.", "https://paper.example/a")
    b = record("b", "Carbon registry", "Regulation", "Carbon registry reporting requires emissions disclosure and MRV audits.", "https://reg.example/b")
    clusters = build_problem_clusters([a, b], threshold=0.45)
    assert len(clusters) == 2


def test_trend_uses_time_and_independent_sources():
    records = [
        record("a", "Oracle", "Literature", "oracle verification bottleneck", "https://a.example/1", "2026-06"),
        record("b", "Oracle", "Funding", "oracle verification grant", "https://b.example/1", "2026-07"),
        record("c", "Oracle", "Company", "oracle verification deployment bottleneck", "https://c.example/1", "2026-08"),
    ]
    report = build_trend_report(records, build_problem_clusters(records, threshold=0.25))
    oracle = next(x for x in report["topic_trends"] if x["topic"] == "oracle")
    assert len(oracle["series"]) == 3
    assert oracle["direction"] == "stable"
    assert report["time_buckets"]


def test_synthesis_evidence_assessment_distinguishes_layers_and_tension():
    members = [
        record("a", "Problem", "Literature", "The limitation remains unresolved.", "https://paper.example/a"),
        record("b", "Problem", "Company", "Existing implementation is effective but has constraints.", "https://company.example/b"),
        record("c", "Problem", "Funding", "Grant calls for a prototype to address the bottleneck.", "https://fund.example/c"),
    ]
    assessment = build_evidence_assessment({"members": members}, [{"excerpt": "x"}])
    assert assessment["independent_sources"] == 3
    assert assessment["research_evidence_layers"] == 3
    assert assessment["contradiction_status"] == "potential_tension"
    assert len(assessment["claims"]) == 3


def test_idea_requires_external_novelty_validation(tmp_path):
    synthesis = tmp_path / "synthesis.md"
    synthesis.write_text(
        """# Synthesis: Test\n\n## Problem\nReserve verification is difficult at scale.\n\n## Research Area\nRWA\n\n## Why Important\nHigh operational risk.\n\n## Existing Solutions\nExisting monitoring tools provide partial coverage.\n\n## Gap\nVerification and exception handling remain fragmented.\n\n## Idea\nBuild a bounded verification workflow.\n\n## Feasibility\nModerate to High\n\n## Source Inputs\n- https://paper.example/a\n""",
        encoding="utf-8",
    )
    entry = {
        "title": "Test Synthesis",
        "area": "RWA",
        "synthesized_file": str(synthesis),
        "layers_covered": ["Literature", "Company"],
        "source_urls": ["https://paper.example/a", "https://company.example/b"],
        "ranking": {"label": "High"},
    }
    payload = build_idea_payload_from_synthesis(entry)
    assert payload["validation"]["novelty_status"] == "existing_solution_overlap_possible"
    assert payload["validation"]["ready_for_proposal"] is False
    assert payload["hypothesis"].lower().startswith("if ")
    assert "metric" in payload["experiment_direction"].lower()


def test_proposal_has_validation_and_never_claims_submission_ready_by_default():
    idea = {
        "title": "Reserve Verification Research Idea",
        "research_area": "RWA",
        "problem": "Reserve verification is difficult at scale.",
        "why_important": "It affects assurance.",
        "existing_solutions": "Existing tools provide partial coverage.",
        "gap": "Verification remains fragmented.",
        "idea": "Build a verification workflow.",
        "feasibility": "Moderate to High",
        "hypothesis": "If verification is automated then assurance improves.",
        "experiment_direction": "Compare a baseline and prototype using latency and accuracy metrics.",
        "prototype_scope": "Build a small demonstrator.",
        "expected_dataset_need": "Synthetic reserve traces.",
        "likely_collaborators": "Labs and infrastructure operators.",
        "based_on": "synthesis.md",
        "confidence": "High",
        "tags": ["#Idea"],
        "validation": {"novelty_status": "needs_external_validation"},
    }
    proposal = build_proposal_payload_from_idea(idea)
    assert "validation" in proposal
    assert proposal["validation"]["status"] == "draft_requires_human_review"
    assert proposal["validation"]["ready_for_submission"] is False


def test_full_direction_chain_smoke(tmp_path):
    records = [
        record("a", "Reserve verification bottleneck", "Literature", "Reserve verification is difficult and creates an assurance gap.", "https://paper.example/a"),
        record("b", "Reserve verification bottleneck", "Company", "Reserve verification is difficult in production and monitoring is fragmented.", "https://company.example/b"),
        record("c", "Reserve verification bottleneck", "Funding", "Funding call requests prototype verification and exception alerts.", "https://fund.example/c"),
    ]
    tagged = [build_tags_for_record(x) for x in records]
    assert all(x["problem_signature"] for x in tagged)
    clusters = build_problem_clusters(records, threshold=0.20)
    assert clusters
    trend = build_trend_report(records, clusters)
    assert "topic_trends" in trend
    assessment = build_evidence_assessment(clusters[0], [])
    assert assessment["independent_sources"] >= 1


def test_area_scoring_does_not_misclassify_candidate_as_did():
    from core.schemas import score_research_areas
    scores = score_research_areas("The candidate system improves reserve verification and monitoring.")
    assert scores["DID"] == 0


def test_tagging_evidence_contains_actual_excerpt_not_only_keyword():
    item = record(
        "r2", "Oracle verification", "Literature",
        "Oracle verification becomes a bottleneck for tokenized assets.",
        "https://paper.example/r2",
    )
    tagged = build_tags_for_record(item)
    excerpts = tagged["tag_evidence"].get("#topic-oracle", [])
    assert excerpts
    assert any("oracle verification" in excerpt.lower() for excerpt in excerpts)


def test_clustering_deduplicates_identical_collection_repeats():
    a = record("a1", "Reserve verification", "Literature", "Reserve verification is difficult at scale.", "https://paper.example/a")
    b = dict(a)
    b["record_id"] = "a2"
    clusters = build_problem_clusters([a, b], threshold=0.25)
    assert len(clusters) == 1
    assert clusters[0]["deduped_member_count"] == 1


def test_trend_includes_missing_months_as_zero():
    records = [
        record("a", "Oracle", "Literature", "oracle verification bottleneck", "https://a.example/1", "2026-06"),
        record("b", "Oracle", "Company", "oracle verification deployment bottleneck", "https://b.example/1", "2026-08"),
    ]
    report = build_trend_report(records, build_problem_clusters(records, threshold=0.25))
    oracle = next(x for x in report["topic_trends"] if x["topic"] == "oracle")
    assert [x["month"] for x in oracle["series"]] == ["2026-06", "2026-07", "2026-08"]
    assert oracle["series"][1]["independent_sources"] == 0


def test_idea_never_marks_unvalidated_novelty_as_proposal_ready(tmp_path):
    synthesis = tmp_path / "synthesis.md"
    synthesis.write_text(
        """# Synthesis: Test\n\n## Problem\nReserve verification is difficult at scale.\n\n## Research Area\nRWA\n\n## Why Important\nHigh operational risk.\n\n## Existing Solutions\nCurrent approaches exist.\n\n## Gap\nVerification remains fragmented.\n\n## Idea\nBuild a bounded verification workflow.\n\n## Feasibility\nModerate\n\n## Source Inputs\n- https://paper.example/a\n""",
        encoding="utf-8",
    )
    entry = {
        "title": "Test Synthesis",
        "area": "RWA",
        "synthesized_file": str(synthesis),
        "layers_covered": ["Literature", "Company"],
        "source_urls": ["https://paper.example/a"],
        "ranking": {"label": "High"},
    }
    payload = build_idea_payload_from_synthesis(entry)
    assert payload["validation"]["novelty_status"] != "externally_validated"
    assert payload["validation"]["ready_for_proposal"] is False
    assert payload["validation"]["ready_for_external_review"] is True


def test_explicit_area_is_not_overridden_by_ambiguous_reserve_term():
    from core.schemas import resolve_research_area
    resolved, warnings, _ = resolve_research_area(
        explicit_area="RWA",
        title="Reserve verification",
        raw_content="Reserve verification for tokenized assets is difficult at scale.",
    )
    assert resolved == "RWA"


def test_strong_content_can_override_explicit_area_when_materially_stronger():
    from core.schemas import resolve_research_area
    resolved, warnings, _ = resolve_research_area(
        explicit_area="RWA",
        title="Stablecoin depeg and reserve risk",
        raw_content="Stablecoin reserve depeg payment rail settlement vault stablecoin reserve monitoring.",
    )
    assert resolved == "Stablecoins"
    assert warnings


def test_processing_refreshes_stale_normalized_records(tmp_path):
    from core.normalization import ensure_normalized_collection_records
    normalized = tmp_path / "normalized"
    intermediate = tmp_path / "intermediate"
    normalized.mkdir()
    intermediate.mkdir()
    stale = normalized / "records.json"
    stale.write_text("[]\n", encoding="utf-8")
    import os, time
    old = time.time() - 100
    os.utime(stale, (old, old))
    artifact = intermediate / "signal.md"
    artifact.write_text(
        "# Signal\n\n## Shared Metadata\n- title: Reserve Verification\n- source: https://example.org/a\n- layer: Literature\n- research_area: RWA\n- collected_at: 2026-08-24T10:00:00+00:00\n\n## Problem Preview\nReserve verification is difficult at scale.\n\n## Evidence Bundle\n### Evidence Snippets\n- Reserve verification is difficult at scale.\n",
        encoding="utf-8",
    )
    ensure_normalized_collection_records(tmp_path)
    import json
    records = json.loads(stale.read_text(encoding="utf-8"))
    assert len(records) == 1


def test_proposal_manual_funding_requirement_blocks_submission():
    from agents.proposal_agent import validate_proposal_payload

    record = {
        "validation": {"novelty_status": "externally_validated"},
        "submission_review_passed": True,
    }
    proposal = {
        "problem": "A specific problem",
        "gap": "A documented gap",
        "proposed_method": "A testable method",
        "evaluation_plan": "Compare against a baseline metric",
        "funding_alignment": "Matches the call",
    }
    result = validate_proposal_payload(
        record,
        proposal,
        {"eligibility_rule": "manual_check_required"},
    )
    assert result["ready_for_submission"] is False



def test_derivative_reporting_is_grouped_not_counted_as_independent(monkeypatch):
    from core.event_lineage import detect_derivative_relationships
    from core.intelligence import detect_evidence_relationships

    primary = record(
        "p", "Company X launches verification platform", "Company",
        "Company X announced the launch of its verification platform on August 24 2026.",
        "https://company.example/announcement", "2026-08",
    )
    news_a = record(
        "a", "Company X introduces new verification platform", "Practitioner",
        "Company X introduced its new verification platform on August 24 2026, according to the announcement.",
        "https://news-a.example/story", "2026-08",
    )
    news_b = record(
        "b", "New verification system launched by Company X", "Practitioner",
        "A new verification system was launched by Company X on August 24 2026.",
        "https://news-b.example/story", "2026-08",
    )

    def fake_llm(left, right):
        return {"relation": "derivative", "confidence": 0.96, "reason": "same launch event"}

    lineage = detect_derivative_relationships([primary, news_a, news_b], llm_resolver=fake_llm)
    assert lineage["counts"]["likely_derivative_relationships"] == 2
    assert lineage["counts"]["derivative_groups"] == 1
    assert set(lineage["derivative_groups"][0]) == {"p", "a", "b"}

    # The production report carries the same lineage metadata and can therefore
    # treat the three documents as one underlying evidence unit.
    report = detect_evidence_relationships([primary, news_a, news_b])
    assert "event_lineage" in report


def test_independent_corroboration_is_not_collapsed_into_derivative_group(monkeypatch):
    from core.event_lineage import detect_derivative_relationships

    primary = record(
        "p", "Company X launches verification platform", "Company",
        "Company X announced the launch of its verification platform on August 24 2026.",
        "https://company.example/announcement", "2026-08",
    )
    regulator = record(
        "r", "Regulator confirms verification requirement", "Regulation",
        "Regulator independently confirms a verification requirement affecting the same platform event.",
        "https://regulator.example/notice", "2026-08",
    )

    def fake_llm(left, right):
        return {"relation": "corroborating", "confidence": 0.92, "reason": "independent regulatory confirmation"}

    lineage = detect_derivative_relationships([primary, regulator], llm_resolver=fake_llm)
    assert lineage["counts"]["independent_corroborations"] == 1
    assert lineage["counts"]["derivative_groups"] == 0



def test_tagging_records_per_tag_origin_threshold_and_validation():
    item = record(
        "tag-meta", "Scalability", "Literature",
        "The system explicitly states that scalability is the main limitation. Verification latency rises with transaction volume.",
        "https://paper.example/tag-meta",
    )
    tagged = build_tags_for_record(item)
    assert "#topic-scalability" in tagged["topic_tags"]
    assert tagged["tag_decisions"]
    meta = tagged["tag_decisions"]["#topic-scalability"]
    assert meta["origin"] == "explicit"
    assert meta["decision"] == "accepted"
    assert meta["evidence_valid"] is True
    assert meta["threshold"] == 0.60
    assert meta["confidence"] >= meta["threshold"]


def test_tagging_rejects_negated_topic_evidence():
    item = record(
        "negated", "No security issue", "Literature",
        "The system does not have a security vulnerability and no exploit was observed.",
        "https://paper.example/negated",
    )
    tagged = build_tags_for_record(item)
    meta = tagged["tag_decisions"].get("#topic-security")
    assert meta is not None
    assert meta["decision"] == "rejected"
    assert meta["negated"] is True


def test_tagging_inferred_semantic_match_has_supporting_excerpt():
    item = record(
        "semantic", "Verification bottleneck", "Literature",
        "The system becomes impractical as workload grows, causing performance degradation.",
        "https://paper.example/semantic",
    )
    tagged = build_tags_for_record(item)
    meta = tagged["tag_decisions"].get("#topic-scalability")
    assert meta is not None
    assert meta["evidence_span"]
    assert meta["decision"] == "accepted"


def test_tagging_low_confidence_candidate_keeps_threshold_gate():
    item = record(
        "weak", "Passing mention", "Literature",
        "The introduction mentions a benchmark once, but the paper studies a different topic.",
        "https://paper.example/weak",
    )
    tagged = build_tags_for_record(item)
    benchmark = tagged["tag_decisions"].get("#topic-benchmark")
    assert benchmark is not None
    assert benchmark["decision"] in {"accepted", "rejected"}
    if benchmark["decision"] == "accepted":
        assert benchmark["confidence"] >= benchmark["threshold"]


def test_tagging_uses_llm_for_ambiguous_meaning(monkeypatch):
    from agents import tagging_agent

    calls = []

    def fake_llm(prompt):
        calls.append(prompt)
        return '{"supported": true, "confidence": 0.92, "reason": "The evidence describes the candidate concept even though the exact taxonomy term is not used."}'

    monkeypatch.setattr(tagging_agent, "llm_generate", fake_llm)
    monkeypatch.setattr(tagging_agent, "_semantic_tag_scores", lambda text: {**{name: 0.0 for name in tagging_agent.TAG_PROTOTYPES}, "scalability": 0.60})
    monkeypatch.setattr(tagging_agent, "_semantic_signal_scores", lambda text: {name: 0.0 for name in tagging_agent.SIGNAL_PROTOTYPES})

    item = record(
        "ambiguous-llm", "Verification system", "Literature",
        "The system becomes impractical as workload grows, causing performance degradation.",
        "https://paper.example/ambiguous-llm",
    )
    tagged = build_tags_for_record(item)
    meta = tagged["tag_decisions"]["#topic-scalability"]
    assert calls
    assert meta["llm_used"] is True
    assert meta["llm_supported"] is True
    assert meta["decision"] == "accepted"
    assert meta["origin"] == "inferred"


def test_tagging_rejects_ambiguous_candidate_when_llm_disagrees(monkeypatch):
    from agents import tagging_agent

    def fake_llm(prompt):
        return '{"supported": false, "confidence": 0.88, "reason": "The passage is only a passing mention and does not establish the candidate tag."}'

    monkeypatch.setattr(tagging_agent, "llm_generate", fake_llm)
    monkeypatch.setattr(tagging_agent, "_semantic_tag_scores", lambda text: {**{name: 0.0 for name in tagging_agent.TAG_PROTOTYPES}, "scalability": 0.60})
    monkeypatch.setattr(tagging_agent, "_semantic_signal_scores", lambda text: {name: 0.0 for name in tagging_agent.SIGNAL_PROTOTYPES})

    item = record(
        "ambiguous-reject", "Passing mention", "Literature",
        "The introduction mentions workload growth once, but the paper studies a different topic.",
        "https://paper.example/ambiguous-reject",
    )
    tagged = build_tags_for_record(item)
    meta = tagged["tag_decisions"]["#topic-scalability"]
    assert meta["llm_used"] is True
    assert meta["llm_supported"] is False
    assert meta["decision"] == "rejected"



def test_clustering_uses_llm_for_ambiguous_pair_and_accepts():
    from core.intelligence import build_problem_clusters

    a = record(
        "amb-a",
        "Blockchain scalability problem",
        "Literature",
        "High transaction volume causes latency and verification difficulty.",
        "https://paper.example/amb-a",
    )
    b = record(
        "amb-b",
        "Blockchain performance problem",
        "Company",
        "High transaction volume causes throughput degradation and verification slowdown.",
        "https://company.example/amb-b",
    )
    calls = []

    def resolver(left, right):
        calls.append((left["record_id"], right["record_id"]))
        return {"same_cluster": True, "confidence": 0.92, "reason": "same underlying scalability problem"}

    clusters = build_problem_clusters([a, b], threshold=0.35, llm_resolver=resolver)
    assert calls, "Ambiguous pair should reach the LLM resolver"
    assert len(clusters) == 1
    assert len(clusters[0]["members"]) == 2


def test_clustering_uses_llm_for_ambiguous_pair_and_rejects():
    from core.intelligence import build_problem_clusters

    a = record(
        "amb-c",
        "Blockchain security and privacy",
        "Literature",
        "Security controls are difficult to maintain in decentralized systems.",
        "https://paper.example/amb-c",
    )
    b = record(
        "amb-d",
        "Blockchain security and interoperability",
        "Company",
        "Interoperability controls are difficult across decentralized systems.",
        "https://company.example/amb-d",
    )
    calls = []

    def resolver(left, right):
        calls.append((left["record_id"], right["record_id"]))
        return {"same_cluster": False, "confidence": 0.95, "reason": "different underlying problems"}

    clusters = build_problem_clusters([a, b], threshold=0.35, llm_resolver=resolver)
    assert calls, "Ambiguous pair should reach the LLM resolver"
    assert len(clusters) == 2


def test_trend_detects_acceleration_not_just_growth():
    records = []
    month_counts = [("2026-04", 1), ("2026-05", 2), ("2026-06", 4), ("2026-07", 8)]
    idx = 0
    for month, count in month_counts:
        for _ in range(count):
            idx += 1
            records.append(record(f"a{idx}", "Oracle", "Literature", "oracle verification bottleneck", f"https://source.example/{idx}", month))
    report = build_trend_report(records, build_problem_clusters(records, threshold=0.25))
    oracle = next(x for x in report["topic_trends"] if x["topic"] == "oracle")
    assert oracle["direction"] == "accelerating"
    assert oracle["signals"]["acceleration"] > 0
    assert "independent_evidence_units" in oracle["series"][-1]


def test_trend_uses_llm_only_for_ambiguous_signal(monkeypatch):
    calls = []

    def fake_llm(prompt):
        calls.append(prompt)
        return '{"decision":"rising","confidence":0.91,"reason":"Recent activity rises consistently after a mixed earlier period."}'

    monkeypatch.setattr("agents.trend_agent.llm_generate", fake_llm)
    records = []
    idx = 0
    for month, count in [("2026-04", 1), ("2026-05", 2), ("2026-06", 3)]:
        for _ in range(count):
            idx += 1
            records.append(record(f"a{idx}", "Oracle", "Literature", "oracle verification bottleneck", f"https://source.example/{idx}", month))
    report = build_trend_report(records, build_problem_clusters(records, threshold=0.25))
    oracle = next(x for x in report["topic_trends"] if x["topic"] == "oracle")
    assert calls
    assert oracle["llm_review"]["decision"] == "rising"
    assert oracle["confidence"] >= 0.91


def test_trend_llm_rejection_becomes_uncertain(monkeypatch):
    def fake_llm(prompt):
        return '{"decision":"uncertain","confidence":0.88,"reason":"The apparent movement is not sufficiently supported."}'

    monkeypatch.setattr("agents.trend_agent.llm_generate", fake_llm)
    records = []
    idx = 0
    for month, count in [("2026-04", 1), ("2026-05", 2), ("2026-06", 3)]:
        for _ in range(count):
            idx += 1
            records.append(record(f"a{idx}", "Oracle", "Literature", "oracle verification bottleneck", f"https://source.example/{idx}", month))
    report = build_trend_report(records, build_problem_clusters(records, threshold=0.25))
    oracle = next(x for x in report["topic_trends"] if x["topic"] == "oracle")
    assert oracle["direction"] == "uncertain"
    assert oracle["decision"] == "uncertain"

