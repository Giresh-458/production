from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

STOPWORDS = {
    'the','and','for','with','that','this','from','into','their','there','which','using','used','use','are','was','were',
    'have','has','had','will','can','may','more','than','also','such','these','those','about','over','under','through',
    'research','system','systems','technology','technical','study','paper','project','solution','solutions','approach',
}


def clean(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


def tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r'[a-z0-9][a-z0-9_-]{2,}', clean(text).lower()) if t not in STOPWORDS}


def url_key(url: str) -> str:
    raw = clean(url)
    if not raw:
        return ''
    parsed = urlparse(raw)
    if parsed.netloc:
        return parsed.netloc.lower() + parsed.path.rstrip('/').lower()
    return raw.lower()


def source_key(record: dict[str, Any]) -> str:
    for key in ('source_url', 'file_path', 'record_id', '_source_file'):
        value = clean(record.get(key))
        if value:
            return url_key(value) if key == 'source_url' else value.lower()
    return ''


def actor_key(record: dict[str, Any]) -> str:
    actor = clean(record.get('actor')).lower()
    return '' if actor in {'', 'unknown', 'manual'} else actor


def evidence_text(record: dict[str, Any]) -> str:
    parts = [
        clean(record.get('title')),
        clean(record.get('problem_statement') or record.get('problem')),
        clean(record.get('context_summary')),
        clean(record.get('focus')),
        ' '.join(clean(x) for x in record.get('evidence_snippets', []) if clean(x)),
        ' '.join(clean(x) for x in record.get('excerpt_windows', []) if clean(x)),
        ' '.join(clean(x) for x in record.get('keywords', []) if clean(x)),
    ]
    return ' '.join(x for x in parts if x)


def problem_signature(record: dict[str, Any]) -> dict[str, Any]:
    text = evidence_text(record)
    tokens = tokenize(text)
    area = clean(record.get('research_area')) or 'Unscoped'
    topic_tags = [clean(x).replace('#topic-', '') for x in record.get('topic_tags', []) if clean(x).startswith('#topic-')]
    signal_tags = [clean(x).replace('#signal-type-', '') for x in record.get('signal_type_tags', []) if clean(x).startswith('#signal-type-')]
    keywords = sorted(tokens)[:16]
    canonical = f"{area.lower()}|{'|'.join(sorted(set(topic_tags)))}|{'|'.join(keywords)}"
    return {
        'area': area,
        'topic_facets': sorted(set(topic_tags)),
        'signal_facets': sorted(set(signal_tags)),
        'keywords': keywords,
        'fingerprint': hashlib.sha256(canonical.encode()).hexdigest()[:20],
    }


def independent_source_count(records: list[dict[str, Any]]) -> int:
    return len({source_key(r) for r in records if source_key(r)})


def independent_layer_count(records: list[dict[str, Any]]) -> int:
    return len({clean(r.get('layer')).lower() for r in records if clean(r.get('layer'))})


def corroboration_profile(records: list[dict[str, Any]]) -> dict[str, Any]:
    layers = sorted({clean(r.get('layer')) for r in records if clean(r.get('layer'))})
    sources = set(source_key(r) for r in records if source_key(r))
    actors = sorted({actor_key(r) for r in records if actor_key(r)})
    research_layers = [x for x in layers if x.lower() not in {'investment', 'hackathon'}]
    lineage_groups = {}
    for record in records:
        group = clean(record.get('event_lineage_group'))
        source = source_key(record)
        if group:
            lineage_groups.setdefault(group, set()).add(source or f"record:{id(record)}")
    derivative_group_sources = set().union(*lineage_groups.values()) if lineage_groups else set()
    # A likely-derivative group is one underlying evidence unit, not N independent sources.
    # Independent sources outside such groups remain individual evidence units.
    grouped_source_count = len(lineage_groups)
    ungrouped_source_count = len(sources - derivative_group_sources)
    independent_evidence_units = grouped_source_count + ungrouped_source_count
    return {
        'independent_sources': len(sources),
        'independent_evidence_units': independent_evidence_units,
        'independent_layers': len(layers),
        'research_evidence_layers': len(research_layers),
        'layers': layers,
        'actors': actors,
        'independence_ratio': round(independent_evidence_units / max(len(records), 1), 3),
    }


def parse_date(value: Any) -> datetime | None:
    raw = clean(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def temporal_buckets(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        dt = parse_date(record.get('collected_at') or record.get('published_at') or record.get('date'))
        if dt:
            buckets[dt.strftime('%Y-%m')].append(record)
    return dict(sorted(buckets.items()))
