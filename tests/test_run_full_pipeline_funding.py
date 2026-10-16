"""Regression tests for the documented full-pipeline funding handoff."""

from pathlib import Path

from core.funding_selection import DEFAULT_DB_PATH, FundingCallContext


def test_full_pipeline_analyzes_funding_and_selects_from_the_same_database(tmp_path, monkeypatch):
    import run_full_pipeline
    import core.funding_selection as funding_selection

    monkeypatch.chdir(tmp_path)
    calls = []
    selection_db_paths = []

    context = FundingCallContext(
        funding_call_id="CALL-TEST-01",
        funding_body="Example Funder",
        program_name="Example Research Grant",
        call_id="CALL-TEST-01",
        call_title="Example Research Grant",
        status="OPEN_CALL",
        opening_date="2026-01-01",
        deadline="2030-12-31",
        funding_amount="$10,000",
        currency="USD",
        duration=None,
        eligible_costs=None,
        eligibility="Eligible applicants must be legal entities established in an eligible country.",
        geography="Global",
        trl=None,
        research_priorities=["DePIN"],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        application_url="https://example.org/calls/test/apply",
        source_url="https://example.org/calls/test",
        source_name="Example Funder",
        publication_date=None,
        last_updated="2026-10-16",
        research_area="DePIN",
        selection_mode="autonomous",
        selection_score=0.9,
        selection_reasons=["test fixture"],
    )
    selection_payload = context.to_dict()
    selection_payload["selected_funding_call_id"] = context.funding_call_id

    def fake_run_registered_agent(agent, mode, area=None, input_data=None, **kwargs):
        calls.append({
            "agent": agent,
            "mode": mode,
            "area": area,
            "input_data": input_data or {},
            "kwargs": kwargs,
        })
        return {"status": "success", "errors": [], "outputs": [], "items_saved": 0}

    def fake_execute_selection(mode, profile, db_path):
        selection_db_paths.append(Path(db_path))
        return selection_payload

    monkeypatch.setattr(run_full_pipeline, "run_registered_agent", fake_run_registered_agent)
    monkeypatch.setattr(funding_selection, "execute_selection", fake_execute_selection)
    monkeypatch.setattr(
        "core.normalization.save_normalized_collection_records",
        lambda *args, **kwargs: [],
    )

    run_full_pipeline.run_pipeline()

    funding_call = next(call for call in calls if call["agent"] == "funding")
    assert funding_call["input_data"]["analysis_mode"] == "analyze"
    assert Path(funding_call["input_data"]["db_path"]) == DEFAULT_DB_PATH
    assert selection_db_paths == [DEFAULT_DB_PATH]
