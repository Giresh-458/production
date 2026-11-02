from __future__ import annotations

from pathlib import Path
from typing import Any


def write_test_evidence_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    """Create a real schema-valid ledger for tests that mock normalization output."""
    from core.evidence_ledger import get_ledger_connection, upsert_canonical_evidence

    connection = get_ledger_connection(path)
    try:
        for record in records:
            strength = float(record.get("evidence_strength", 0.7) or 0.7)
            source_url = str(record.get("source_url") or "https://example.org/test-evidence")
            source_id = str(record.get("source_id") or f"url:{source_url}")
            canonical = {
                "evidence_id": str(record["evidence_id"]),
                "record_id": str(record["record_id"]),
                "funding_call_id": str(record["funding_call_id"]),
                "run_id": str(record["run_id"]),
                "agent": str(record.get("agent_name") or "literature"),
                "layer": str(record.get("layer") or "Literature"),
                "source_id": source_id,
                "source_url": source_url,
                "canonical_url": str(record.get("canonical_url") or source_url),
                "source_type": str(record.get("source_type") or "web_page"),
                "source_name": str(record.get("actor") or record.get("source_name") or "Test source"),
                "title": str(record.get("title") or "Test evidence"),
                "problem": str(record.get("problem_statement") or ""),
                "problem_signature": "pending-synthesis",
                "evidence": "\n".join(record.get("evidence_snippets") or []),
                "evidence_excerpt": "\n".join(record.get("excerpt_windows") or []),
                "evidence_type": str(record.get("evidence_type") or "web_source"),
                "research_area": str(record.get("research_area") or "Unscoped"),
                "published_at": None,
                "retrieved_at": str(record["collected_at"]),
                "updated_at": None,
                "content_hash": str(record["content_hash"]),
                "source_quality": strength,
                "evidence_strength": strength,
                "confidence": strength,
                "independence_group": source_id,
                "independence_status": "INDEPENDENT",
                "normalization_status": str(record.get("normalization_status") or "NORMALIZED"),
                "provenance": {
                    "source_url": source_url,
                    "source_id": source_id,
                    "agent": str(record.get("agent_name") or "literature"),
                    "funding_call_id": str(record["funding_call_id"]),
                    "run_id": str(record["run_id"]),
                    "retrieved_at": str(record["collected_at"]),
                    "source_funding_call_ids": record.get("source_funding_call_ids", []),
                },
                "raw_record_id": str(record.get("file_path") or record["record_id"]),
            }
            upsert_canonical_evidence(connection, canonical)
        connection.commit()
    finally:
        connection.close()
