import sqlite3
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, UTC

def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def get_ledger_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = _dict_factory
    _init_schema(conn)
    return conn

def _init_schema(conn: sqlite3.Connection) -> None:
    # 1. canonical_evidence table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canonical_evidence (
            evidence_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            funding_call_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            layer TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            problem TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            evidence TEXT NOT NULL,
            evidence_excerpt TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            research_area TEXT NOT NULL,
            published_at TEXT,
            retrieved_at TEXT NOT NULL,
            updated_at TEXT,
            content_hash TEXT NOT NULL,
            source_quality REAL NOT NULL,
            evidence_strength REAL NOT NULL,
            confidence REAL NOT NULL,
            independence_group TEXT NOT NULL,
            independence_status TEXT NOT NULL,
            normalization_status TEXT NOT NULL,
            provenance TEXT NOT NULL,
            raw_record_id TEXT NOT NULL
        )
    """)

    # 2. claims table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            claim_id TEXT PRIMARY KEY,
            claim_text TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            problem_signature TEXT NOT NULL
        )
    """)

    # 3. evidence_claims relationship
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evidence_claims (
            evidence_id TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            claim_relationship TEXT NOT NULL,
            PRIMARY KEY (evidence_id, claim_id)
        )
    """)

    # 4. entities table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            aliases TEXT NOT NULL,
            external_ids TEXT NOT NULL
        )
    """)

    # 5. evidence_entities relationship
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evidence_entities (
            evidence_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            PRIMARY KEY (evidence_id, entity_id)
        )
    """)

    # 6. independence_groups table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS independence_groups (
            group_id TEXT PRIMARY KEY,
            parent_source_id TEXT,
            event_id TEXT
        )
    """)

    conn.commit()

def upsert_canonical_evidence(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO canonical_evidence (
            evidence_id, record_id, funding_call_id, run_id, agent, layer,
            source_id, source_url, canonical_url, source_type, source_name,
            title, problem, problem_signature, evidence, evidence_excerpt,
            evidence_type, research_area, published_at, retrieved_at, updated_at,
            content_hash, source_quality, evidence_strength, confidence,
            independence_group, independence_status, normalization_status,
            provenance, raw_record_id
        ) VALUES (
            :evidence_id, :record_id, :funding_call_id, :run_id, :agent, :layer,
            :source_id, :source_url, :canonical_url, :source_type, :source_name,
            :title, :problem, :problem_signature, :evidence, :evidence_excerpt,
            :evidence_type, :research_area, :published_at, :retrieved_at, :updated_at,
            :content_hash, :source_quality, :evidence_strength, :confidence,
            :independence_group, :independence_status, :normalization_status,
            :provenance, :raw_record_id
        )
        ON CONFLICT(evidence_id) DO UPDATE SET
            record_id=excluded.record_id,
            funding_call_id=excluded.funding_call_id,
            run_id=excluded.run_id,
            agent=excluded.agent,
            layer=excluded.layer,
            source_id=excluded.source_id,
            source_url=excluded.source_url,
            canonical_url=excluded.canonical_url,
            source_type=excluded.source_type,
            source_name=excluded.source_name,
            title=excluded.title,
            problem=excluded.problem,
            problem_signature=excluded.problem_signature,
            evidence=excluded.evidence,
            evidence_excerpt=excluded.evidence_excerpt,
            evidence_type=excluded.evidence_type,
            research_area=excluded.research_area,
            published_at=excluded.published_at,
            retrieved_at=excluded.retrieved_at,
            updated_at=excluded.updated_at,
            content_hash=excluded.content_hash,
            source_quality=excluded.source_quality,
            evidence_strength=excluded.evidence_strength,
            confidence=excluded.confidence,
            independence_group=excluded.independence_group,
            independence_status=excluded.independence_status,
            normalization_status=excluded.normalization_status,
            provenance=excluded.provenance,
            raw_record_id=excluded.raw_record_id
    """, {
        **record,
        'provenance': json.dumps(record.get('provenance', {})),
    })

def upsert_claim(conn: sqlite3.Connection, claim: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO claims (claim_id, claim_text, claim_type, problem_signature)
        VALUES (:claim_id, :claim_text, :claim_type, :problem_signature)
        ON CONFLICT(claim_id) DO UPDATE SET
            claim_text=excluded.claim_text,
            claim_type=excluded.claim_type,
            problem_signature=excluded.problem_signature
    """, claim)

def link_evidence_claim(conn: sqlite3.Connection, evidence_id: str, claim_id: str, relationship: str = "SUPPORTS") -> None:
    conn.execute("""
        INSERT OR IGNORE INTO evidence_claims (evidence_id, claim_id, claim_relationship)
        VALUES (?, ?, ?)
    """, (evidence_id, claim_id, relationship))

def upsert_entity(conn: sqlite3.Connection, entity: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO entities (entity_id, canonical_name, entity_type, aliases, external_ids)
        VALUES (:entity_id, :canonical_name, :entity_type, :aliases, :external_ids)
        ON CONFLICT(entity_id) DO UPDATE SET
            canonical_name=excluded.canonical_name,
            entity_type=excluded.entity_type,
            aliases=excluded.aliases,
            external_ids=excluded.external_ids
    """, {
        **entity,
        'aliases': json.dumps(entity.get('aliases', [])),
        'external_ids': json.dumps(entity.get('external_ids', {}))
    })

def link_evidence_entity(conn: sqlite3.Connection, evidence_id: str, entity_id: str) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO evidence_entities (evidence_id, entity_id)
        VALUES (?, ?)
    """, (evidence_id, entity_id))
