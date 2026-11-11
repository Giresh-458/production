from __future__ import annotations

from pathlib import Path
from typing import Any


def write_test_evidence_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    """Write exact canonical ledger rows and enrich the matching JSON fixtures."""
    import hashlib
    import json
    from core.evidence_ledger import get_ledger_connection, upsert_canonical_evidence
    from core.normalization import canonicalize_url, _canonical_evidence_ledger_record

    selected_call_id = ""
    selected_run_id = ""
    selected_path = path.parent.parent / "workflow" / "selected_funding_call.json"
    try:
        selected_wrapper = json.loads(selected_path.read_text(encoding="utf-8"))
        selected = selected_wrapper.get("selected", {}) if isinstance(selected_wrapper, dict) else {}
        if isinstance(selected, dict):
            selected_call_id = str(selected.get("call_id") or selected.get("funding_call_id") or "")
        if isinstance(selected_wrapper, dict):
            selected_run_id = str(selected_wrapper.get("run_id") or "")
    except (OSError, json.JSONDecodeError):
        pass

    for index, record in enumerate(records):
        title = str(record.get("title") or f"Cache fixture {index + 1}")
        source_url = str(record.get("source_url") or f"https://example.org/cache-fixture/{index + 1}")
        canonical_url = str(record.get("canonical_url") or canonicalize_url(source_url))
        funding_call_id = str(record.get("funding_call_id") or selected_call_id)
        run_id = str(record.get("run_id") or selected_run_id)
        problem = str(record.get("problem_statement") or record.get("problem") or "Cache fixture problem statement.")
        context_summary = str(record.get("context_summary") or problem)
        snippets = record.get("evidence_snippets")
        if not isinstance(snippets, list):
            snippets = [str(snippets)] if snippets else [problem]
        snippets = [str(value) for value in snippets if str(value).strip()]
        excerpt_windows = record.get("excerpt_windows")
        if not isinstance(excerpt_windows, list):
            excerpt_windows = []
        collected_at = str(record.get("collected_at") or "2026-11-11T10:00:00+00:00")
        record_id = str(record.get("record_id") or record.get("evidence_id") or f"cache-record-{index + 1}")
        content_hash = str(record.get("content_hash") or hashlib.sha256(
            (title + source_url + problem + context_summary + "\\n".join(snippets)).encode("utf-8")
        ).hexdigest())
        source_id = str(record.get("source_id") or f"url:{canonical_url}")
        actor = str(record.get("actor") or record.get("source_name") or "Test source")
        machine_metadata = record.get("machine_metadata")
        if not isinstance(machine_metadata, dict):
            machine_metadata = {}
        record.update({
            "evidence_id": str(record.get("evidence_id") or hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:16]),
            "record_id": record_id,
            "funding_call_id": funding_call_id,
            "funding_call_ids": record.get("funding_call_ids") if isinstance(record.get("funding_call_ids"), list) else ([funding_call_id] if funding_call_id else []),
            "source_funding_call_ids": record.get("source_funding_call_ids") if isinstance(record.get("source_funding_call_ids"), list) else ([funding_call_id] if funding_call_id else []),
            "run_id": run_id,
            "agent_name": str(record.get("agent_name") or "literature"),
            "layer": str(record.get("layer") or ""),
            "source_id": source_id,
            "source_url": source_url,
            "canonical_url": canonical_url,
            "source_type": str(record.get("source_type") or "web_page"),
            "actor": actor,
            "title": title,
            "problem_statement": problem,
            "context_summary": context_summary,
            "evidence_snippets": snippets,
            "excerpt_windows": [str(value) for value in excerpt_windows],
            "evidence_type": str(record.get("evidence_type") or "web_source"),
            "research_area": str(record.get("research_area") or "Unscoped"),
            "collected_at": collected_at,
            "content_hash": content_hash,
            "evidence_strength": float(record.get("evidence_strength", 0.7) or 0.7),
            "normalization_status": str(record.get("normalization_status") or "NORMALIZED"),
            "machine_metadata": machine_metadata,
            "named_entities": record.get("named_entities") if isinstance(record.get("named_entities"), list) else [],
            "file_path": str(record.get("file_path") or (path.parent.parent / "intermediate" / f"{record_id}.md")),
            "source_id_variants": record.get("source_id_variants") if isinstance(record.get("source_id_variants"), list) else [source_id],
            "source_url_variants": record.get("source_url_variants") if isinstance(record.get("source_url_variants"), list) else [source_url],
            "title_variants": record.get("title_variants") if isinstance(record.get("title_variants"), list) else [title],
            "content_versions": record.get("content_versions") if isinstance(record.get("content_versions"), list) else [content_hash],
            "observation_count": int(record.get("observation_count", 1) or 1),
            "first_seen": str(record.get("first_seen") or collected_at),
            "last_seen": str(record.get("last_seen") or collected_at),
        })

    records_path = path.parent / "records.json"
    if records_path.is_file():
        records_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    content_hash_to_group: dict[str, str] = {}
    for record in records:
        content_hash = str(record.get("content_hash") or "")
        if content_hash and content_hash not in content_hash_to_group:
            content_hash_to_group[content_hash] = str(record.get("source_id") or "")

    connection = get_ledger_connection(path)
    try:
        for record in records:
            upsert_canonical_evidence(
                connection,
                _canonical_evidence_ledger_record(record, content_hash_to_group),
            )
        connection.commit()
    finally:
        connection.close()


def write_cache_test_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    """Compatibility wrapper for cache tests; always writes the full ledger contract."""
    write_test_evidence_ledger(path, records)


def write_test_collector_artifacts(run_root: Path, run_id: str, agents: list[str] | set[str]) -> None:
    """Create current-run, unassigned artifacts for orchestration-only pipeline tests."""
    for agent in sorted(set(agents)):
        artifact_dir = run_root / "intermediate" / agent
        artifact_dir.mkdir(parents=True, exist_ok=True)
        import hashlib
        import json

        safe_agent = "".join(character if character.isalnum() else "-" for character in agent)
        title = f"Unassigned test evidence from {agent}"
        source_url = f"https://example.org/collector-fixtures/{safe_agent}"
        raw_content = "This fixture verifies the collector artifact contract without claiming ownership by a funding call."
        document_identity = hashlib.sha256(f"{source_url}|{title}".encode("utf-8")).hexdigest()
        content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        collected_at = "2026-11-01T10:00:00+00:00"
        machine_payload = {
            "shared_header": {
                "title": title,
                "source": source_url,
                "layer": agent.replace("_", " ").title(),
                "agent_name": agent,
                "run_id": run_id,
                "research_area": "Unscoped",
                "collected_at": collected_at,
            },
            "provenance": {
                "source_url": source_url,
                "collected_at": collected_at,
                "agent_name": agent,
                "document_identity": document_identity,
                "content_hash": content_hash,
            },
            "evidence_bundle": {
                "page_title": title,
                "section_headings": [],
                "key_paragraphs": [],
                "bullet_lists": [],
                "quoted_passages": [],
                "links_found": [],
                "named_entities_or_programs": [],
                "evidence_snippets": [
                    "The fixture contains a source-grounded evidence excerpt for artifact-contract testing."
                ],
                "excerpt_windows": [],
            },
            "raw_content": raw_content,
        }
        machine_payload_json = json.dumps(machine_payload, ensure_ascii=False)
        artifact = artifact_dir / f"unassigned-{safe_agent}.md"
        artifact.write_text(
            "\n".join([
                f"# Test collection artifact: {agent}",
                "",
                "## Shared Metadata",
                f"- title: {title}",
                f"- source: {source_url}",
                f"- layer: {agent.replace('_', ' ').title()}",
                f"- agent_name: {agent}",
                f"- run_id: {run_id}",
                "- research_area: Unscoped",
                "- collected_at: 2026-11-01T10:00:00+00:00",
                "",
                "## Provenance",
                f"- source_url: {source_url}",
                "- collected_at: 2026-11-01T10:00:00+00:00",
                f"- agent_name: {agent}",
                f"- document_identity: {document_identity}",
                f"- content_hash: {content_hash}",
                "",
                "## Problem Preview",
                raw_content,
                "",
                "## Evidence Bundle",
                "### Evidence Snippets",
                "- The fixture contains a source-grounded evidence excerpt for artifact-contract testing.",
                "",
                "## Machine-Readable Payload",
                "```json",
                machine_payload_json,
                "```",
                "",
                "## Raw Content",
                raw_content,
                "",
            ]),
            encoding="utf-8",
        )



def build_valid_funding_context(
    call_id: str = "CALL-BATCH2-001",
    *,
    source_url: str = "https://example.org/funding-call",
    application_url: str = "https://example.org/apply",
):
    """Build a schema-valid, source-grounded Batch-1 context for deterministic tests."""
    from core.funding_selection import FundingCallContext
    from core.research_schemas import FundingResearchContext

    return FundingCallContext(
        funding_call_id=call_id,
        funding_body="Batch-1 Test Agency",
        program_name="Privacy-preserving settlement research",
        call_id=call_id,
        call_title="Privacy-preserving settlement research",
        status="OPEN_CALL",
        opening_date="2026-01-01",
        deadline="2027-12-31",
        funding_amount="1000000",
        currency="EUR",
        duration="24 months",
        eligible_costs="Personnel and equipment",
        eligibility="Universities and research organizations may apply.",
        geography="EU",
        trl="TRL3-TRL5",
        research_priorities=["zero knowledge verification"],
        required_partners="Research and industry partners",
        deliverables="Prototype and benchmark report",
        evaluation_criteria="Excellence and implementation",
        application_url=application_url,
        source_url=source_url,
        source_name="Batch-1 Test Agency",
        publication_date="2026-01-01",
        last_updated="2026-01-01",
        research_area="RWA",
        selection_mode="autonomous",
        selection_score=0.95,
        selection_reasons=["Validated Batch-1 fixture"],
        research_context=FundingResearchContext(
            funding_call_id=call_id,
            domain="RWA",
            technology_themes=("zero knowledge verification",),
            funding_priorities=("zero knowledge verification",),
            target_outcomes=("scalable secure settlement",),
            investigation_questions=("How can verification throughput scale securely?",),
            inferred_challenges=("Verification throughput degrades under high transaction volume.",),
            evidence_refs=(source_url,),
            inference_provenance=("source_grounded_problem_extraction",),
            confidence="High",
            research_intent="research",
            research_intent_evidence=("The official call supports technical research and validation.",),
            selected_domains=("RWA",),
            domain_relevance=(),
        ),
    )



def write_cache_test_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    """Write the minimal canonical-evidence table needed by normalization cache tests."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_evidence (
                evidence_id TEXT PRIMARY KEY,
                funding_call_id TEXT,
                run_id TEXT,
                source_url TEXT,
                content_hash TEXT
            )
            """
        )
        connection.execute("DELETE FROM canonical_evidence")
        connection.executemany(
            """
            INSERT INTO canonical_evidence (
                evidence_id, funding_call_id, run_id, source_url, content_hash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    str(record.get("evidence_id") or ""),
                    str(record.get("funding_call_id") or ""),
                    str(record.get("run_id") or ""),
                    str(record.get("source_url") or ""),
                    str(record.get("content_hash") or ""),
                )
                for record in records
            ],
        )
        connection.commit()
    finally:
        connection.close()


def write_test_intermediate_artifact(
    run_root: Path,
    *,
    run_id: str,
    funding_call_id: str,
    title: str,
    source_url: str,
    content: str,
    research_area: str,
    collected_at: str,
    evidence_snippets: list[str] | None = None,
    doi: str | None = None,
) -> Path:
    """Write a complete, auditable Batch-2 artifact for pipeline integration tests."""
    from datetime import datetime
    from core.agent_interface import build_intermediate_artifact_payload, save_intermediate_markdown

    timestamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    payload, _ = build_intermediate_artifact_payload(
        agent="literature",
        layer="Literature",
        mode="configured_scan",
        area=research_area,
        title=title,
        source=source_url,
        content=content,
        metadata={
            "source_type": "Research publication",
            "evidence_type": "Source excerpt",
            **({"doi": doi} if doi else {}),
        },
        collected_at=timestamp,
        allow_llm_refinement=False,
    )
    payload["shared_header"]["funding_call_id"] = funding_call_id
    payload["shared_header"]["funding_call_ids"] = [funding_call_id]
    payload["shared_header"]["run_id"] = run_id
    if evidence_snippets is not None:
        snippets = list(evidence_snippets)
        if len(snippets) < 2:
            snippets.append("A second supporting source excerpt retained for deterministic contract coverage.")
        payload["evidence_bundle"]["evidence_snippets"] = snippets
    payload["agent_specific_body"].update({
        "authors": "Batch-2 Test Author",
        "score": 0.8,
        "relevance_score": 0.8,
        "impact_score": 0.8,
        "recency_score": 0.8,
        "problem_clarity_score": 0.8,
    })
    # These tests isolate mission-gate and deduplication behavior. Collection
    # quality is exercised independently by the 11-agent contract suite.
    payload["metadata"]["quality_gate"]["passed"] = True
    return save_intermediate_markdown(
        payload=payload,
        output_dir=run_root / "intermediate" / "literature",
    )
