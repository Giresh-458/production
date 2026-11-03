from __future__ import annotations

from datetime import UTC, datetime

from agents.lab_agent import LabDocument, LabPage
from core.agent_interface import (
    _document_content,
    _document_source,
    _document_agent_specific_body,
    build_intermediate_artifact_payload,
    save_intermediate_markdown,
)
from core.batch2 import audit_collector_artifacts
from core.normalization import _normalize_intermediate_sections, normalize_intermediate_artifact


def test_lab_pages_keep_real_source_url_and_exact_multiline_evidence(tmp_path):
    source_url = "https://www.nist.gov/programs-projects/cyber-physical-systemsinternet-things-testbed"
    page_content = (
        "NIST describes its cyber-physical systems testbed.\n"
        "## Research capabilities\n"
        "The second line is retained as source evidence."
    )
    document = LabDocument(
        name="NIST CPS / IoT Testbed",
        institution="National Institute of Standards and Technology",
        base_url="https://www.nist.gov/",
        source_type="Automatic",
        pages=[
            LabPage(
                url=source_url,
                title="NIST CPS / IoT Testbed",
                content=page_content,
            )
        ],
    )

    assert _document_source(document) == source_url
    raw_content = _document_content(document)
    assert "Page title: NIST CPS / IoT Testbed" in raw_content
    assert f"Source URL: {source_url}" in raw_content
    assert page_content in raw_content
    assert "LabPage(" not in raw_content

    payload, warnings = build_intermediate_artifact_payload(
        agent="lab",
        layer="Lab",
        mode="configured_scan",
        area="DePIN",
        title=document.name,
        source=_document_source(document),
        content=raw_content,
        metadata={"institution": document.institution},
        collected_at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    assert not warnings
    payload["shared_header"]["run_id"] = "batch2-lab-contract-test"

    payload["agent_specific_body"] = _document_agent_specific_body(document)
    artifact_path = save_intermediate_markdown(
        payload=payload,
        output_dir=tmp_path / "intermediate" / "lab",
    )
    parsed = _normalize_intermediate_sections(artifact_path)
    normalized = normalize_intermediate_artifact(artifact_path)

    assert parsed["raw_content"] == payload["raw_content"]
    assert normalized["source_url"] == source_url
    assert normalized["run_id"] == "batch2-lab-contract-test"

    audited_tasks, audit = audit_collector_artifacts(
        tmp_path,
        [
            {
                "agent_name": "lab",
                "status": "success",
                "outputs_count": 1,
                "output_paths": [str(artifact_path.resolve())],
            }
        ],
        expected_agents={"lab"},
        run_id="batch2-lab-contract-test",
    )
    assert audit["status"] == "passed"
    assert audit["agents"][0]["persisted_valid_artifacts"] == 1
    assert audited_tasks[0]["artifact_audit_issues"] == []
