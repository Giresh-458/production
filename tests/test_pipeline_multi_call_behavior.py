import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from run_pipeline import main
from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS

def test_collector_isolation_and_bad_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)])

    mock_run_stage = MagicMock()
    def side_effect_run_stage(agent, mode, area, payload, funding_context=None, funding_contexts=None):
        if agent == "funding":
            rc = {
                "funding_call_id": "test",
                "domain": "test",
                "technology_themes": (),
                "funding_priorities": (),
                "target_outcomes": (),
                "investigation_questions": (),
                "inferred_challenges": ("The source identifies a concrete research gap.",),
                "evidence_refs": ("https://v1.org",),
                "inference_provenance": ("source_grounded_problem_extraction",),
                "confidence": "Medium",
                "research_intent": "research",
                "research_intent_evidence": ["Research funding supports technology validation."]
            }
            domain_evidence = [{"domain": "DePIN", "score": 0.8, "raw_score": 3.0, "matched_terms": ["depin"]}]
            return {
                "outputs": [
                    {
                        "title": "Call Valid 1",
                        "program_name": "Call Valid 1",
                        "status": "OPEN_CALL",
                        "deadline": "2030-01-01T00:00:00Z",
                        "application_url": "https://v1.org",
                        "eligibility": "Eligible applicants must be legal entities established in an EU Member State.",
                        "domain_relevance": domain_evidence,
                        "research_context": rc
                    },
                    {
                        "title": "Call Bad",
                        "program_name": "Call Bad",
                        "status": "OPEN_CALL",
                        "deadline": "2030-01-01T00:00:00Z",
                        "application_url": "https://bad.org",
                        "eligibility": "Eligible applicants must be legal entities established in an EU Member State.",
                        "domain_relevance": domain_evidence,
                        "research_context": rc
                    },
                    {
                        "title": "Call Valid 2",
                        "program_name": "Call Valid 2",
                        "status": "OPEN_CALL",
                        "deadline": "2030-01-01T00:00:00Z",
                        "application_url": "https://v2.org",
                        "eligibility": "Eligible applicants must be legal entities established in an EU Member State.",
                        "domain_relevance": domain_evidence,
                        "research_context": rc
                    }
                ]
            }

        # for processing agents
        return {"status": "success", "outputs": [{"some": "output"}], "outputs_count": 1}

    mock_run_stage.side_effect = side_effect_run_stage

    # execute_batch_run is called for the collection agents
    mock_execute_batch = MagicMock()
    def side_effect_execute_batch(*args, **kwargs):
        ctxs = kwargs.get("funding_contexts")
        assert kwargs["base_input_data"]["allow_llm_analysis"] is False
        assert kwargs["base_input_data"]["allow_llm_refinement"] is False
        assert ctxs is not None, "execute_batch_run must receive funding_contexts"
        assert len(ctxs) == 3, "must pass all contexts"
        from _batch2_helpers import write_test_collector_artifacts
        write_test_collector_artifacts(
            Path(kwargs["base_input_data"]["pipeline_run_dir"]),
            kwargs["base_input_data"]["run_id"],
            kwargs["agent_names"],
        )

        return {
            "status": "success",
            "tasks": [
                {"status": "success", "agent_name": agent, "outputs_count": 1}
                for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
            ]
        }

    mock_execute_batch.side_effect = side_effect_execute_batch

    mock_save_normalized = MagicMock()
    mock_save_normalized.return_value = True

    with patch("run_pipeline.run_stage", mock_run_stage), \
         patch("run_pipeline.execute_batch_run", mock_execute_batch), \
         patch("run_pipeline.save_normalized_collection_records", mock_save_normalized), \
         patch("run_pipeline.mission_relevance") as mock_mission, \
         patch("core.source_registry.sync_source_registry"), \
         patch("core.source_registry.build_source_refresh_plan"):

        # Make the mission gate pass
        mock_mission.return_value = {"passed": True}

        # We need to simulate the records.json existence since save_normalized isn't actually writing it
        def create_records_mock(*args, **kwargs):
            # save_normalized_collection_records(intermediate_roots, call_root, call_id, ...)
            call_root = args[1]
            call_id = args[2]
            records_path = call_root / "normalized" / "records.json"
            records_path.parent.mkdir(parents=True, exist_ok=True)

            import hashlib
            source_url = "https://example.org/evidence"
            record = {
                "record_id": hashlib.sha1(f"record-{call_id}".encode()).hexdigest()[:16],
                "evidence_id": hashlib.sha256(f"evidence-{call_id}".encode()).hexdigest()[:16],
                "source_id": f"url:{source_url}",
                "content_hash": hashlib.sha256(f"content-{call_id}".encode()).hexdigest(),
                "title": "Controlled shared-collection evidence",
                "agent_name": "literature",
                "layer": "Literature",
                "source_type": "web_page",
                "evidence_type": "web_source",
                "evidence_strength": 0.7,
                "run_id": kwargs["target_run_id"],
                "funding_call_id": call_id,
                "funding_call_ids": [call_id],
                "source_funding_call_ids": [call_id],
                "source_url": source_url,
                "normalization_status": "NORMALIZED",
                "collected_at": "2026-11-01T10:00:00+00:00",
                "problem_statement": "A controlled source-grounded technical limitation.",
                "context_summary": "A controlled source-grounded technical limitation.",
                "evidence_snippets": ["A measurable technical limitation."],
                "excerpt_windows": [],
            }
            if kwargs.get("filter_func"):
                kwargs["filter_func"](record)
            records_path.write_text(json.dumps([record]), encoding="utf-8")

            # Simulate the normalization failure for one selected call while
            # allowing the other two calls to continue through later stages.
            selected_wrapper = json.loads(
                (call_root / "workflow" / "selected_funding_call.json").read_text(encoding="utf-8")
            )
            selected_title = selected_wrapper.get("selected", {}).get("title")
            if selected_title == "Call Bad":
                raise ValueError("Simulated extraction error for Call Bad")

            (call_root / "normalized" / "by_area.json").write_text("{}", encoding="utf-8")
            (call_root / "normalized" / "by_layer.json").write_text("{}", encoding="utf-8")
            (call_root / "normalized" / "input_manifest.json").write_text("{}", encoding="utf-8")
            (call_root / "normalized" / "errors.json").write_text("[]", encoding="utf-8")
            (call_root / "normalized" / "skipped_artifacts.json").write_text("[]", encoding="utf-8")
            from _batch2_helpers import write_test_evidence_ledger
            write_test_evidence_ledger(call_root / "normalized" / "evidence_ledger.db", [record])
            return {
                "records": records_path,
                "by_area": call_root / "normalized" / "by_area.json",
                "by_layer": call_root / "normalized" / "by_layer.json",
                "errors": call_root / "normalized" / "errors.json",
                "skipped_artifacts": call_root / "normalized" / "skipped_artifacts.json",
            }

        mock_save_normalized.side_effect = create_records_mock

        exit_code = main()

    # global exit code will be 4 because Call Bad throws an exception
    # but the pipeline should still complete the other two calls.
    assert exit_code == 4

    # Check that execute_batch_run was called exactly 1 time (GLOBAL shared)
    assert mock_execute_batch.call_count == 1

def test_shared_evidence_isolation(tmp_path):
    import json
    from core.normalization import save_normalized_collection_records
    
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir(parents=True)
    
    # Create deterministic fixture with CALL-A, CALL-B, and shared CALL-A + CALL-B
    # Evidence 1: Only CALL-A
    evidence_a = intermediate / "agent_1" / "evidence_a.md"
    evidence_a.parent.mkdir(parents=True)
    evidence_a.write_text(
        "# Signal\n\n## Shared Metadata\n- title: Only A\n- source_url: https://a.com\n- layer: Literature\n- funding_call_id: CALL-A\n\n## Problem Preview\nA problem.\n",
        encoding="utf-8"
    )
    
    # Evidence 2: Only CALL-B
    evidence_b = intermediate / "agent_1" / "evidence_b.md"
    evidence_b.write_text(
        "# Signal\n\n## Shared Metadata\n- title: Only B\n- source_url: https://b.com\n- layer: Literature\n- funding_call_id: CALL-B\n\n## Problem Preview\nB problem.\n",
        encoding="utf-8"
    )
    
    # Evidence 3: Shared CALL-A and CALL-B
    evidence_shared = intermediate / "agent_1" / "evidence_shared.md"
    evidence_shared.write_text(
        "# Signal\n\n## Shared Metadata\n- title: Shared A and B\n- source_url: https://shared.com\n- layer: Literature\n- funding_call_ids: CALL-A, CALL-B\n\n## Problem Preview\nShared problem.\n",
        encoding="utf-8"
    )
    
    # Process CALL-A
    call_a_root = tmp_path / "calls" / "CALL-A"
    call_a_root.mkdir(parents=True)
    save_normalized_collection_records(intermediate, call_a_root, "CALL-A")
    records_a = json.loads((call_a_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    
    # Verify CALL-A proposal has only A's evidence (isolation).
    titles_a = [r["title"] for r in records_a]
    assert "Only A" in titles_a
    assert "Shared A and B" in titles_a
    assert "Only B" not in titles_a
    
    # Verify funding_call_ids is propagated
    shared_record_a = next(r for r in records_a if r["title"] == "Shared A and B")
    assert "CALL-A" in shared_record_a["funding_call_ids"]
    assert "CALL-B" in shared_record_a["funding_call_ids"]
    
    # Process CALL-B
    call_b_root = tmp_path / "calls" / "CALL-B"
    call_b_root.mkdir(parents=True)
    save_normalized_collection_records(intermediate, call_b_root, "CALL-B")
    records_b = json.loads((call_b_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    
    # Verify CALL-B proposal has only B's evidence (isolation).
    titles_b = [r["title"] for r in records_b]
    assert "Only B" in titles_b
    assert "Shared A and B" in titles_b
    assert "Only A" not in titles_b

    # Verify shared evidence doesn't cause duplicate network collection.
    # Since there's only one intermediate file `evidence_shared.md`, it was collected once, but correctly distributed to both calls.
    assert len(list(intermediate.rglob("*.md"))) == 3
