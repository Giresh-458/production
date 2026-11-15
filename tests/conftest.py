import pytest
import core.llm_provider as llm_provider

@pytest.fixture(autouse=True)
def mock_llm_provider(monkeypatch):
    def fake_generate(prompt: str, model: str | None = None) -> str:
        return '{"same_cluster": false, "confidence": 0.5, "reason": "mocked", "supported": true, "aligned": true}'
    monkeypatch.setattr(llm_provider, "generate", fake_generate)


@pytest.fixture
def seed_normalized_ledger():
    """Create a schema-current cache ledger matching synthetic test records."""
    def seed(normalized_dir, records):
        import hashlib
        import json
        from pathlib import Path

        from core.evidence_ledger import (
            get_ledger_connection,
            upsert_canonical_evidence,
            upsert_independence_group,
        )
        from core.normalization import NORMALIZATION_SCHEMA_VERSION

        normalized_dir = Path(normalized_dir)
        normalized_dir.mkdir(parents=True, exist_ok=True)
        connection = get_ledger_connection(normalized_dir / "evidence_ledger.db")
        try:
            for index, record in enumerate(records):
                record["normalization_schema_version"] = NORMALIZATION_SCHEMA_VERSION
                evidence_id = str(record["evidence_id"])
                source_id = str(record.get("source_id") or hashlib.sha256(
                    f"test-source:{evidence_id}".encode("utf-8")
                ).hexdigest()[:16])
                content_hash = str(record.get("content_hash") or hashlib.sha256(
                    f"test-content:{evidence_id}".encode("utf-8")
                ).hexdigest())
                group_id = str(record.get("independence_group") or source_id)
                upsert_independence_group(connection, group_id, source_id, content_hash)
                snippets = record.get("evidence_snippets", [])
                upsert_canonical_evidence(connection, {
                    "evidence_id": evidence_id,
                    "record_id": str(record.get("record_id") or evidence_id),
                    "funding_call_id": str(record.get("funding_call_id") or ""),
                    "run_id": str(record.get("run_id") or ""),
                    "agent": str(record.get("agent_name") or "test"),
                    "layer": str(record.get("layer") or ""),
                    "source_id": source_id,
                    "source_url": str(record.get("source_url") or ""),
                    "canonical_url": str(record.get("canonical_url") or ""),
                    "source_type": str(record.get("source_type") or "test"),
                    "source_name": str(record.get("actor") or "test"),
                    "title": str(record.get("title") or f"Record {index}"),
                    "problem": str(record.get("problem_statement") or ""),
                    "problem_signature": "test",
                    "evidence": "\n".join(snippets),
                    "evidence_excerpt": "\n".join(record.get("excerpt_windows", [])),
                    "evidence_type": str(record.get("evidence_type") or "test"),
                    "research_area": str(record.get("research_area") or "Unscoped"),
                    "published_at": record.get("published_at"),
                    "retrieved_at": str(record.get("collected_at") or ""),
                    "updated_at": None,
                    "content_hash": content_hash,
                    "source_quality": 0.5,
                    "evidence_strength": 0.5,
                    "confidence": 0.5,
                    "independence_group": group_id,
                    "independence_status": "INDEPENDENT",
                    "normalization_status": str(record.get("normalization_status") or "NORMALIZED"),
                    "provenance": {},
                    "raw_record_id": str(record.get("file_path") or evidence_id),
                })
            connection.commit()
        finally:
            connection.close()

        (normalized_dir / "records.json").write_text(
            json.dumps(records, indent=2), encoding="utf-8"
        )
        (normalized_dir / "errors.json").write_text("[]", encoding="utf-8")

    return seed
