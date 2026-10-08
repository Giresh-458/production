from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
import contextvars
from urllib.parse import urlparse

from core.http_client import canonicalize_url

import yaml

from core.file_lock import with_registry_lock, atomic_write_json

from core.schemas import ALLOWED_RESEARCH_AREAS, AREA_KEYWORDS, normalize_area

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def extract_yaml_limits(data: Any) -> dict[str, dict[str, Any]]:
    limits = {}
    def _traverse(node: Any):
        if isinstance(node, dict):
            if "url" in node and isinstance(node.get("limits"), dict):
                url = str(node["url"])
                can_url = canonicalize_url(url)
                limit_dict = {}
                for k in ["max_pages", "max_documents", "max_depth", "max_seconds"]:
                    if k in node["limits"]:
                        limit_dict[k] = int(node["limits"][k])
                limit_dict["_source_name"] = str(node.get("name", ""))
                limit_dict["_original_url"] = url
                
                limits[url] = limit_dict
                limits[can_url] = limit_dict
            for val in node.values():
                _traverse(val)
        elif isinstance(node, list):
            for item in node:
                _traverse(item)
    _traverse(data)
    return limits

SOURCES_DIR = PROJECT_ROOT / "sources"
SOURCE_REGISTRY_PATH = SOURCES_DIR / "source_registry.json"
SOURCE_FILE_AGENT_MAP = {
    "company_sources.yaml": "company",
    "data_sources.yaml": "data_availability",
    "failure_sources.yaml": "failure",
    "funding_sources.yaml": "funding",
    "hackathon_sources.yaml": "hackathon",
    "investment_sources.yaml": "investment",
    "expert_sources.yaml": "expert",
    "labs_sources.yaml": "lab",
    "opensource_sources.yaml": "opensource",
    "practitioner_sources.yaml": "practitioner",
    "regulation_sources.yaml": "regulation",
}
DEFAULT_FETCH_FREQUENCY = {
    "company": "weekly",
    "data_availability": "weekly",
    "failure": "daily",
    "funding": "weekly",
    "hackathon": "weekly",
    "investment": "weekly",
    "expert": "weekly",
    "lab": "monthly",
    "opensource": "weekly",
    "practitioner": "weekly",
    "regulation": "monthly",
}
REFRESH_PLAN_PATH = PROJECT_ROOT / "outputs" / "workflow" / "source_refresh_plan.json"
SOURCE_DISCOVERY_PATH = PROJECT_ROOT / "outputs" / "workflow" / "source_discovery_candidates.json"
SOURCE_REVIEW_QUEUE_PATH = PROJECT_ROOT / "outputs" / "workflow" / "source_review_queue.json"
SOURCE_HEALTH_SUMMARY_PATH = PROJECT_ROOT / "outputs" / "workflow" / "source_health_summary.json"
SOURCE_PRUNE_REPORT_PATH = PROJECT_ROOT / "outputs" / "workflow" / "source_prune_report.json"

# Per-agent execution context. Batch orchestration sets these before invoking an agent.
# This lets the existing source loaders share one authoritative refresh/area policy
# without changing every agent-specific source dataclass.
current_force_source_refresh = contextvars.ContextVar("current_force_source_refresh", default=False)
current_source_area = contextvars.ContextVar("current_source_area", default=None)
SOURCE_STATUSES = {"candidate", "approved", "active", "paused", "stale", "deprecated"}
ACTIVE_SOURCE_STATUSES = {"approved", "active", "stale"}
STATUS_TRANSITIONS = {
    "candidate": {"approved", "paused", "deprecated"},
    "approved": {"active", "paused", "deprecated"},
    "active": {"paused", "stale", "deprecated"},
    "paused": {"approved", "active", "deprecated"},
    "stale": {"active", "paused", "deprecated"},
    "deprecated": set(),
}
FREQUENCY_TO_DELTA = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=90),
}


@dataclass(slots=True)
class RegistrySyncResult:
    total_entries: int
    active_entries: int
    deprecated_entries: int
    registry_path: str


@dataclass(slots=True)
class RefreshPlanResult:
    total_entries: int
    due_entries: int
    skipped_entries: int
    refresh_plan_path: str


@dataclass(slots=True)
class SourceLifecycleResult:
    source_id: str
    old_status: str
    new_status: str
    registry_path: str


@dataclass(slots=True)
class SourceDiscoveryResult:
    total_candidates: int
    new_candidates: int
    existing_candidates: int
    discovery_path: str


@dataclass(slots=True)
class SourceReviewQueueResult:
    total_entries: int
    pending_entries: int
    decided_entries: int
    review_queue_path: str


@dataclass(slots=True)
class SourceReviewResult:
    review_id: str
    action: str
    review_status: str
    source_id: str
    queue_path: str
    registry_path: str


@dataclass(slots=True)
class SourceHealthSummaryResult:
    total_entries: int
    due_entries: int
    pending_review_entries: int
    summary_path: str


@dataclass(slots=True)
class SourcePruneResult:
    pruned_count: int
    dry_run: bool
    prune_report_path: str
    registry_path: str


@dataclass(slots=True)
class SourceEvidenceRescoreResult:
    updated_count: int
    summary_path: str
    registry_path: str


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _slugify(value: str) -> str:
    return "-".join(part for part in "".join(ch.lower() if ch.isalnum() else "-" for ch in value).split("-") if part)


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _load_registry(path: Path = SOURCE_REGISTRY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": "", "entries": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"generated_at": "", "entries": []}


def _save_registry(payload: dict[str, Any], path: Path = SOURCE_REGISTRY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _save_refresh_plan(payload: dict[str, Any], path: Path = REFRESH_PLAN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _save_source_discovery(payload: dict[str, Any], path: Path = SOURCE_DISCOVERY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _load_review_queue(path: Path = SOURCE_REVIEW_QUEUE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": "", "entries": [], "stats": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"generated_at": "", "entries": [], "stats": {}}


def _save_review_queue(payload: dict[str, Any], path: Path = SOURCE_REVIEW_QUEUE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _save_source_health_summary(payload: dict[str, Any], path: Path = SOURCE_HEALTH_SUMMARY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _save_source_prune_report(payload: dict[str, Any], path: Path = SOURCE_PRUNE_REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _extract_area_hints_from_texts(texts: list[str]) -> list[str]:
    hints: set[str] = set()
    lowered_text = "\n".join(texts).lower()
    for area in ALLOWED_RESEARCH_AREAS:
        if area.lower() in lowered_text:
            hints.add(area)
    alias_candidates = {
        "rwa": "RWA",
        "esg": "ESG",
        "esg_carbon": "ESG",
        "zk_iov": "ZK-IoV",
        "zk-iov": "ZK-IoV",
        "did": "DID",
        "depin": "DePIN",
        "mev": "MEV",
        "stablecoins": "Stablecoins",
        "stablecoin": "Stablecoins",
    }
    for token, area in alias_candidates.items():
        if token in lowered_text:
            hints.add(area)
    for area, keywords in AREA_KEYWORDS.items():
        if any(keyword.lower() in lowered_text for keyword in keywords):
            hints.add(area)
    return sorted(hints)


def _estimate_trust_score(source_type: str, url: str) -> float:
    lowered = f"{source_type} {url}".lower()
    if any(term in lowered for term in ("w3c", "iso", "gov", "europa", "rbi", "sebi", "fatf", "bis", "iosco", "registry")):
        return 0.9
    if any(term in lowered for term in ("github", "docs", "developer", "research", "transparency")):
        return 0.8
    if any(term in lowered for term in ("blog", "forum", "platform", "events", "hackathon")):
        return 0.65
    return 0.7


def _estimate_relevance_score(area_hints: list[str], focus: str) -> float:
    score = 0.45
    if area_hints:
        score += min(0.1 * len(area_hints), 0.3)
    if focus:
        score += 0.15
    return round(min(score, 0.95), 2)


from core.http_client import canonicalize_url

def _normalize_url(url: str) -> str:
    return canonicalize_url(url)



def _candidate_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path:
        last = path.split("/")[-1].replace("-", " ").replace("_", " ").strip()
        if last:
            return last.title()
    host = parsed.netloc.lower().replace("www.", "")
    return host.split(".")[0].replace("-", " ").title()


def _refresh_delta(entry: dict[str, Any]) -> timedelta:
    frequency = str(entry.get("fetch_frequency", "weekly")).strip().lower()
    base = FREQUENCY_TO_DELTA.get(frequency, timedelta(days=7))
    relevance = float(entry.get("relevance_score", 0.7) or 0.7)
    trust = float(entry.get("trust_score", 0.7) or 0.7)

    multiplier = 1.0
    if relevance >= 0.85 or trust >= 0.9:
        multiplier = 0.75
    elif relevance < 0.6:
        multiplier = 1.25

    adjusted_seconds = max(int(base.total_seconds() * multiplier), 3600)
    return timedelta(seconds=adjusted_seconds)


def _status_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(SOURCE_STATUSES)}
    for entry in entries:
        status = str(entry.get("status", "active")).strip().lower() or "active"
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
    return counts


def _rebuild_registry_stats(payload: dict[str, Any]) -> None:
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    status_counts = _status_counts(entries)
    payload["stats"] = {
        "total_entries": len(entries),
        "active_entries": sum(1 for entry in entries if str(entry.get("status", "")).strip().lower() == "active"),
        "deprecated_entries": sum(1 for entry in entries if str(entry.get("status", "")).strip().lower() == "deprecated"),
        "status_counts": status_counts,
    }


def _make_source_id(agent_name: str, name: str, url: str) -> str:
    digest = hashlib.sha1(f"{agent_name}|{name}|{url}".encode("utf-8")).hexdigest()[:10]
    return f"{agent_name}:{_slugify(name) or 'source'}:{digest}"


def _make_review_id(agent_name: str, name: str, url: str) -> str:
    digest = hashlib.sha1(f"review|{agent_name}|{name}|{url}".encode("utf-8")).hexdigest()[:10]
    return f"review:{agent_name}:{_slugify(name) or 'source'}:{digest}"


def _looks_like_source_entry(node: dict[str, Any]) -> bool:
    return isinstance(node, dict) and bool(node.get("name")) and bool(node.get("url"))


def _flatten_sources(
    *,
    node: Any,
    agent_name: str,
    source_file: str,
    path_segments: list[str],
    records: list[dict[str, Any]],
) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _flatten_sources(
                node=item,
                agent_name=agent_name,
                source_file=source_file,
                path_segments=[*path_segments, f"[{index}]"],
                records=records,
            )
        return

    if not isinstance(node, dict):
        return

    if _looks_like_source_entry(node):
        name = str(node.get("name", "")).strip()
        url = str(node.get("url", "")).strip()
        source_type = str(node.get("source_type") or node.get("type") or "configured_source").strip()
        focus = str(node.get("focus", "")).strip()
        area_hints = _extract_area_hints_from_texts([focus, name, " ".join(path_segments)])
        source_path = ".".join(segment for segment in path_segments if segment)
        config_payload = {
            "name": name,
            "url": url,
            "source_type": source_type,
            "focus": focus,
            "area_hints": area_hints,
            "source_file": source_file,
            "source_path": source_path,
            "config": {key: value for key, value in node.items() if key != "url"},
        }
        records.append(
            {
                "source_id": _make_source_id(agent_name, name, url),
                "agent_name": agent_name,
                "name": name,
                "url": url,
                "source_type": source_type or "configured_source",
                "area_hints": area_hints,
                "trust_score": _estimate_trust_score(source_type, url),
                "relevance_score": _estimate_relevance_score(area_hints, focus),
                "status": "active",
                "last_checked": "",
                "last_changed": "",
                "etag": "",
                "last_modified": "",
                "fetch_frequency": DEFAULT_FETCH_FREQUENCY.get(agent_name, "weekly"),
                "content_hash": "",
                "config_hash": _hash_json(config_payload),
                "discovery_method": "configured_seed",
                "notes": focus,
                "source_file": source_file,
                "source_path": source_path,
                "active_config": True,
            }
        )
        return

    for key, value in node.items():
        _flatten_sources(
            node=value,
            agent_name=agent_name,
            source_file=source_file,
            path_segments=[*path_segments, str(key)],
            records=records,
        )


def extract_configured_sources(source_file: Path, agent_name: str) -> list[dict[str, Any]]:
    if not source_file.exists():
        return []
    try:
        payload = yaml.safe_load(source_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []

    records: list[dict[str, Any]] = []
    _flatten_sources(
        node=payload,
        agent_name=agent_name,
        source_file=source_file.name,
        path_segments=[],
        records=records,
    )
    return records


@with_registry_lock
def sync_source_registry(
    *,
    sources_dir: Path = SOURCES_DIR,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    agent_filter: str | None = None,
) -> RegistrySyncResult:
    existing_payload = _load_registry(registry_path)
    existing_entries = {
        str(entry.get("source_id")): entry
        for entry in existing_payload.get("entries", [])
        if isinstance(entry, dict) and entry.get("source_id")
    }

    now = _iso_now()
    current_entries: dict[str, dict[str, Any]] = {}
    for source_file_name, agent_name in SOURCE_FILE_AGENT_MAP.items():
        if agent_filter and agent_name != agent_filter:
            continue
        source_file = sources_dir / source_file_name
        for entry in extract_configured_sources(source_file, agent_name):
            source_id = entry["source_id"]
            existing = existing_entries.get(source_id, {})
            config_changed = existing.get("config_hash") != entry["config_hash"]
            merged = {
                **entry,
                "status": existing.get("status", "active"),
                "last_checked": existing.get("last_checked", ""),
                "last_changed": existing.get("last_changed") or now,
                "etag": existing.get("etag", ""),
                "last_modified": existing.get("last_modified", ""),
                "fetch_frequency": existing.get("fetch_frequency", entry["fetch_frequency"]),
                "content_hash": existing.get("content_hash", ""),
                "discovery_method": existing.get("discovery_method", "configured_seed"),
                "notes": existing.get("notes", entry["notes"]),
                "active_config": True,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }
            if config_changed or not existing:
                merged["last_changed"] = now
            current_entries[source_id] = merged

    if not agent_filter:
        for source_id, existing in existing_entries.items():
            if source_id in current_entries:
                continue
            deprecated = dict(existing)
            deprecated["status"] = "deprecated"
            deprecated["active_config"] = False
            deprecated["updated_at"] = now
            current_entries[source_id] = deprecated

    elif agent_filter:
        for source_id, existing in existing_entries.items():
            if existing.get("agent_name") != agent_filter:
                current_entries.setdefault(source_id, existing)
            elif source_id not in current_entries:
                deprecated = dict(existing)
                deprecated["status"] = "deprecated"
                deprecated["active_config"] = False
                deprecated["updated_at"] = now
                current_entries[source_id] = deprecated

    entries = sorted(current_entries.values(), key=lambda item: (item.get("agent_name", ""), item.get("name", ""), item.get("url", "")))
    payload = {
        "generated_at": now,
        "entries": entries,
    }
    _rebuild_registry_stats(payload)
    _save_registry(payload, registry_path)
    return RegistrySyncResult(
        total_entries=payload["stats"]["total_entries"],
        active_entries=payload["stats"]["active_entries"],
        deprecated_entries=payload["stats"]["deprecated_entries"],
        registry_path=str(registry_path.resolve()),
    )


@with_registry_lock
def record_source_check_result(
    *,
    source_id: str,
    checked_at: str | None = None,
    content_hash: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    changed: bool | None = None,
    status: str | None = None,
    notes: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> bool:
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    when = checked_at or _iso_now()
    updated = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source_id") != source_id:
            continue
        entry["last_checked"] = when
        if status:
            entry["status"] = status
        if notes:
            entry["notes"] = notes
        if content_hash is not None:
            entry["content_hash"] = content_hash
        if etag is not None:
            entry["etag"] = etag
        if last_modified is not None:
            entry["last_modified"] = last_modified
        if changed:
            entry["last_changed"] = when
        entry["updated_at"] = when
        updated = True
        break
    if updated:
        payload["generated_at"] = when
        _rebuild_registry_stats(payload)
        _save_registry(payload, registry_path)
    return updated


def load_source_registry(registry_path: Path = SOURCE_REGISTRY_PATH) -> list[dict[str, Any]]:
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    return [entry for entry in entries if isinstance(entry, dict)]


def _append_review_history(entry: dict[str, Any], *, action: str, rationale: str) -> None:
    history = entry.setdefault("review_history", [])
    if not isinstance(history, list):
        history = []
        entry["review_history"] = history
    history.append(
        {
            "reviewed_at": _iso_now(),
            "action": action,
            "rationale": rationale,
        }
    )


def _validate_transition(old_status: str, new_status: str) -> None:
    if new_status not in SOURCE_STATUSES:
        raise ValueError(f"Unsupported source status: {new_status}")
    if old_status == new_status:
        return
    allowed = STATUS_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise ValueError(f"Invalid source status transition: {old_status} -> {new_status}")


@with_registry_lock
def set_source_status(
    *,
    source_id: str,
    new_status: str,
    notes: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceLifecycleResult:
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    when = _iso_now()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source_id") != source_id:
            continue
        old_status = str(entry.get("status", "active")).strip().lower() or "active"
        target_status = str(new_status).strip().lower()
        _validate_transition(old_status, target_status)
        entry["status"] = target_status
        if notes:
            entry["notes"] = notes
            _append_review_history(entry, action=f"status:{target_status}", rationale=notes)
        entry["updated_at"] = when
        payload["generated_at"] = when
        _rebuild_registry_stats(payload)
        _save_registry(payload, registry_path)
        return SourceLifecycleResult(
            source_id=source_id,
            old_status=old_status,
            new_status=target_status,
            registry_path=str(registry_path.resolve()),
        )
    raise KeyError(f"Unknown source_id: {source_id}")


@with_registry_lock
def add_candidate_source(
    *,
    agent_name: str,
    name: str,
    url: str,
    source_type: str = "candidate_source",
    area_hints: list[str] | None = None,
    notes: str = "",
    discovery_method: str = "manual_candidate",
    fetch_frequency: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceLifecycleResult:
    normalized_agent = str(agent_name).strip().lower()
    hints = [area for area in (normalize_area(area) for area in (area_hints or [])) if area]
    when = _iso_now()
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    source_id = _make_source_id(normalized_agent, name, url)
    for entry in entries:
        if isinstance(entry, dict) and entry.get("source_id") == source_id:
            old_status = str(entry.get("status", "candidate")).strip().lower() or "candidate"
            if old_status == "deprecated":
                _validate_transition(old_status, "candidate")  # will raise; deprecated revival not allowed yet
            entry.update(
                {
                    "notes": notes or entry.get("notes", ""),
                    "updated_at": when,
                }
            )
            if notes:
                _append_review_history(entry, action="candidate_refresh", rationale=notes)
            payload["generated_at"] = when
            _rebuild_registry_stats(payload)
            _save_registry(payload, registry_path)
            return SourceLifecycleResult(
                source_id=source_id,
                old_status=old_status,
                new_status=old_status,
                registry_path=str(registry_path.resolve()),
            )

    entries.append(
        {
            "source_id": source_id,
            "agent_name": normalized_agent,
            "name": name.strip(),
            "url": url.strip(),
            "source_type": source_type.strip() or "candidate_source",
            "area_hints": hints,
            "trust_score": _estimate_trust_score(source_type, url),
            "relevance_score": _estimate_relevance_score(hints, notes),
            "status": "candidate",
            "last_checked": "",
            "last_changed": when,
            "etag": "",
            "last_modified": "",
            "fetch_frequency": (fetch_frequency or DEFAULT_FETCH_FREQUENCY.get(normalized_agent, "weekly")).strip(),
            "content_hash": "",
            "config_hash": "",
            "discovery_method": discovery_method,
            "notes": notes,
            "review_history": [],
            "source_file": "",
            "source_path": "",
            "active_config": False,
            "created_at": when,
            "updated_at": when,
        }
    )
    payload["generated_at"] = when
    _rebuild_registry_stats(payload)
    _save_registry(payload, registry_path)
    return SourceLifecycleResult(
        source_id=source_id,
        old_status="missing",
        new_status="candidate",
        registry_path=str(registry_path.resolve()),
    )


@with_registry_lock
def promote_candidate_source(
    *,
    source_id: str,
    activate: bool = False,
    notes: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceLifecycleResult:
    result = set_source_status(
        source_id=source_id,
        new_status="approved",
        notes=notes,
        registry_path=registry_path,
    )
    if activate:
        result = set_source_status(
            source_id=source_id,
            new_status="active",
            notes=notes,
            registry_path=registry_path,
        )
    return result


@with_registry_lock
def pause_source(
    *,
    source_id: str,
    notes: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceLifecycleResult:
    return set_source_status(source_id=source_id, new_status="paused", notes=notes, registry_path=registry_path)


@with_registry_lock
def deprecate_source(
    *,
    source_id: str,
    notes: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceLifecycleResult:
    return set_source_status(source_id=source_id, new_status="deprecated", notes=notes, registry_path=registry_path)


@with_registry_lock
def mark_stale_sources(
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    agent_filter: str | None = None,
    staleness_multiplier: float = 2.0,
) -> int:
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    now = datetime.now(UTC)
    updated = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if agent_filter and entry.get("agent_name") != agent_filter:
            continue
        status = str(entry.get("status", "active")).strip().lower() or "active"
        if status not in {"approved", "active"}:
            continue
        last_checked = _parse_iso(str(entry.get("last_checked", "")))
        if not last_checked:
            continue
        threshold = _refresh_delta(entry) * staleness_multiplier
        if now - last_checked >= threshold:
            entry["status"] = "stale"
            entry["updated_at"] = _iso_now()
            updated += 1
    if updated:
        payload["generated_at"] = _iso_now()
        _rebuild_registry_stats(payload)
        _save_registry(payload, registry_path)
    return updated


def evaluate_source_refresh(
    entry: dict[str, Any],
    *,
    reference_time: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    now = reference_time or datetime.now(UTC)
    status = str(entry.get("status", "active")).strip().lower() or "active"
    last_checked = _parse_iso(str(entry.get("last_checked", "")))
    last_changed = _parse_iso(str(entry.get("last_changed", "")))
    delta = _refresh_delta(entry)
    next_check_at = (last_checked + delta) if last_checked else now

    refresh_due = False
    reason = "scheduled_skip"
    if force:
        refresh_due = True
        reason = "forced_refresh"
    elif status not in ACTIVE_SOURCE_STATUSES:
        refresh_due = False
        reason = f"inactive_status:{status}"
    elif not last_checked:
        refresh_due = True
        reason = "never_checked"
    elif now >= next_check_at:
        refresh_due = True
        reason = "refresh_interval_elapsed"
    elif entry.get("etag") or entry.get("last_modified"):
        refresh_due = False
        reason = "header_tracked_waiting_for_interval"
    elif not str(entry.get("content_hash", "")).strip():
        refresh_due = False
        reason = "recently_checked_without_fingerprint"
    
    staleness_seconds = 0
    if last_checked:
        staleness_seconds = max(int((now - last_checked).total_seconds()), 0)
    elif last_changed:
        staleness_seconds = max(int((now - last_changed).total_seconds()), 0)

    priority_score = round(
        min(
            1.0,
            float(entry.get("relevance_score", 0.7) or 0.7) * 0.6
            + float(entry.get("trust_score", 0.7) or 0.7) * 0.25
            + (0.15 if refresh_due else 0.0),
        ),
        3,
    )
    return {
        "source_id": entry.get("source_id"),
        "agent_name": entry.get("agent_name"),
        "name": entry.get("name"),
        "url": entry.get("url"),
        "status": status,
        "fetch_frequency": entry.get("fetch_frequency", "weekly"),
        "last_checked": entry.get("last_checked", ""),
        "last_changed": entry.get("last_changed", ""),
        "last_modified": entry.get("last_modified", ""),
        "etag": entry.get("etag", ""),
        "content_hash": entry.get("content_hash", ""),
        "refresh_due": refresh_due,
        "refresh_reason": reason,
        "next_check_at": next_check_at.astimezone(UTC).isoformat(),
        "staleness_seconds": staleness_seconds,
        "priority_score": priority_score,
        "relevance_score": entry.get("relevance_score", 0.0),
        "trust_score": entry.get("trust_score", 0.0),
        "area_hints": entry.get("area_hints", []),
        "source_file": entry.get("source_file", ""),
        "source_path": entry.get("source_path", ""),
    }


@with_registry_lock
def build_source_refresh_plan(
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    output_path: Path = REFRESH_PLAN_PATH,
    agent_filter: str | None = None,
    due_only: bool = False,
    force: bool = False,
) -> RefreshPlanResult:
    entries = load_source_registry(registry_path)
    now = datetime.now(UTC)
    decisions: list[dict[str, Any]] = []
    for entry in entries:
        if agent_filter and entry.get("agent_name") != agent_filter:
            continue
        decision = evaluate_source_refresh(entry, reference_time=now, force=force)
        if due_only and not decision["refresh_due"]:
            continue
        decisions.append(decision)

    decisions.sort(
        key=lambda item: (
            not item["refresh_due"],
            -float(item["priority_score"]),
            item["agent_name"] or "",
            item["name"] or "",
        )
    )
    payload = {
        "generated_at": now.astimezone(UTC).isoformat(),
        "filters": {
            "agent_filter": agent_filter,
            "due_only": due_only,
            "force": force,
        },
        "stats": {
            "total_entries": len(decisions),
            "due_entries": sum(1 for item in decisions if item["refresh_due"]),
            "skipped_entries": sum(1 for item in decisions if not item["refresh_due"]),
        },
        "entries": decisions,
    }
    _save_refresh_plan(payload, output_path)
    return RefreshPlanResult(
        total_entries=payload["stats"]["total_entries"],
        due_entries=payload["stats"]["due_entries"],
        skipped_entries=payload["stats"]["skipped_entries"],
        refresh_plan_path=str(output_path.resolve()),
    )


def apply_refresh_policy(
    agent_name: str,
    sources: list[Any],
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    reference_time: datetime | None = None,
    force: bool = False,
) -> list[Any]:
    """Return the subset of *sources* that should be fetched right now.

    When *force* is ``True`` every active source is returned regardless of
    its refresh schedule — this supports explicit manual / forced scans.

    Otherwise the function respects the refresh schedule:
    * Sources whose URL is tracked in the registry and whose refresh is
      **due** are included.
    * Sources whose URL is **not** tracked in the registry at all are
      included (they have no schedule to honour yet).
    * Sources whose URL is tracked but **not** due are excluded — this is
      the key fix: the old code fell back to returning all active sources
      when no source was due, defeating the refresh schedule entirely.
    """
    # A live pipeline run must be able to re-check sources even when the registry
    # says they were fetched recently. The orchestration layer requests this via
    # a context variable; direct agent calls retain the original behaviour.
    force = bool(force or current_force_source_refresh.get())
    requested_area = current_source_area.get()

    entries = load_source_registry(registry_path)
    entry_by_url: dict[str, list[dict[str, Any]]] = {}
    entry_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("agent_name") != agent_name or not entry.get("url"):
            continue
        url_key = canonicalize_url(str(entry.get("url", "")).strip()) if entry.get("url") else ""
        name_key = str(entry.get("name", "")).strip().casefold()
        entry_by_url.setdefault(url_key, []).append(entry)
        entry_by_key.setdefault((url_key, name_key), []).append(entry)

    def _source_name(source: Any) -> str:
        keys = ("name", "source_name", "company_name", "institution", "investor_or_organization", "challenge_name", "project_name")
        if isinstance(source, dict):
            for key in keys:
                value = str(source.get(key, "") or "").strip()
                if value:
                    return value
        else:
            for key in keys:
                value = str(getattr(source, key, "") or "").strip()
                if value:
                    return value
        return ""

    result: list[Any] = []
    for source in sources:
        if isinstance(source, dict):
            url = str(source.get("url", "")).strip()
        else:
            url = str(getattr(source, "url", "")).strip()
        source_name = _source_name(source)
        url_key = canonicalize_url(url) if url else ""
        matching_entries = entry_by_key.get((url_key, source_name.casefold()), []) if url_key and source_name else []
        if not matching_entries and url_key:
            matching_entries = entry_by_url.get(url_key, [])

        # Area routing is based on the named registry record when available.
        # This avoids cross-domain leakage when several configured sources share
        # the same host or endpoint URL.
        if requested_area and matching_entries:
            requested = normalize_area(str(requested_area))

            def _registry_entry_matches_area(entry: dict[str, Any]) -> bool:
                # Prefer the configured YAML category when it maps cleanly to a
                # canonical domain (e.g. *.cps.[n] -> DigitalHealthCPS). This is
                # stronger than inferred keyword hints and avoids broad terms such
                # as "sensor", "security", or "research" leaking other domains.
                source_path = str(entry.get("source_path", ""))
                category_aliases = {
                    "rwa": "RWA", "esg_carbon": "ESG", "esg_reporting": "ESG",
                    "zk_iov": "ZK-IoV", "did": "DID", "depin": "DePIN", "mev": "MEV",
                    "stablecoins": "Stablecoins", "cps": "DigitalHealthCPS",
                    "digitalhealthcps": "DigitalHealthCPS", "cps_failures": "DigitalHealthCPS",
                    "cps_challenges": "DigitalHealthCPS", "cps_investment": "DigitalHealthCPS",
                    "cps_experts": "DigitalHealthCPS", "cps_standards": "DigitalHealthCPS",
                    "cps_practitioners": "DigitalHealthCPS", "cps_data": "DigitalHealthCPS",
                    "cps_sources": "DigitalHealthCPS", "cps_labs": "DigitalHealthCPS",
                    "climate_carbon": "ESG", "global_finance": "RWA",
                    "global_payments": "Stablecoins", "stablecoin": "Stablecoins",
                }
                configured_area = None
                known_category = False
                for part in source_path.split("."):
                    candidate = part.strip().lower()
                    if candidate.startswith("["):
                        continue
                    if candidate in category_aliases:
                        configured_area = category_aliases[candidate]
                        known_category = True
                        break
                if known_category:
                    return configured_area == requested
                # Hints are advisory only for genuinely generic source buckets.
                hints = {normalize_area(str(h)) for h in (entry.get("area_hints") or [])}
                hints.discard(None)
                if hints and len(hints) == 1:
                    return requested in hints
                source_blob = " ".join([
                    str(entry.get("name", "")),
                    str(entry.get("notes", "")),
                    str(entry.get("focus", "")),
                ])
                return __import__("core.schemas", fromlist=["score_research_areas"]).score_research_areas(source_blob).get(requested, 0) >= 2

            if not any(_registry_entry_matches_area(entry) for entry in matching_entries):
                continue

        if not url or not matching_entries:
            # No registry record: include it so new/manual sources remain usable.
            result.append(source)
            continue

        active_entries = [
            entry for entry in matching_entries
            if str(entry.get("status", "active")).strip().lower() in ACTIVE_SOURCE_STATUSES
        ]
        if not active_entries:
            continue

        if force:
            result.append(source)
            continue

        if any(evaluate_source_refresh(entry, reference_time=reference_time)["refresh_due"] for entry in active_entries):
            result.append(source)

    # Funding discovery is a current-state gate: returning an empty source set
    # because every registry timestamp is recent can make a live pipeline report
    # "no funding opportunities" without inspecting the web at all.  Funding
    # therefore falls back to all active configured sources when its scheduled
    # subset is empty. Other agents keep the normal scheduled policy.
    if not result and agent_name == "funding" and not force:
        result = [
            source for source in sources
            if any(
                str(entry.get("status", "active")).strip().lower() in ACTIVE_SOURCE_STATUSES
                for entry in entry_by_url.get(
                    str(source.get("url", "")).strip() if isinstance(source, dict) else str(getattr(source, "url", "")).strip(),
                    [],
                )
            )
        ]
    return result


@with_registry_lock
def mark_agent_sources_checked(
    agent_name: str,
    *,
    attempted_urls: set[str] | None = None,
    checked_at: str | None = None,
    status: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> int:
    """Mark only the sources that were actually attempted during the run.

    Parameters
    ----------
    agent_name:
        The agent whose sources should be considered.
    attempted_urls:
        The set of source URLs that the agent actually attempted during this
        run.  Only matching registry entries will have their ``last_checked``
        timestamp updated.  Pass ``None`` (the default) to fall back to the
        previous behaviour and stamp **all** active sources for the agent —
        this is intentionally kept for manual and non-scan invocations where
        per-source tracking is not available.
    checked_at:
        ISO-8601 timestamp to record.  Defaults to *now*.
    status:
        Optional lifecycle status override (e.g. ``"active"``).
    registry_path:
        Path to the registry JSON file.

    Returns
    -------
    int
        Number of registry entries that were updated.
    """
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    when = checked_at or _iso_now()
    updated_count = 0

    # Pre-normalize the attempted URL set once for O(1) lookup.
    # For automated scans, an omitted/empty attempt set must never stamp every
    # source as checked. Manual registry maintenance can still pass an explicit
    # non-empty set.
    normalized_attempted: set[str] = {_normalize_url(u) for u in (attempted_urls or set()) if u}
    if not normalized_attempted:
        return 0

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("agent_name") != agent_name:
            continue
        if str(entry.get("status", "active")).strip().lower() == "deprecated":
            continue

        # When a URL set was provided, only stamp entries whose URL was
        # actually attempted during this run.
        if normalized_attempted is not None:
            entry_url = _normalize_url(str(entry.get("url", "")).strip())
            if entry_url and entry_url not in normalized_attempted:
                continue

        entry["last_checked"] = when
        entry["updated_at"] = when
        if status:
            entry["status"] = status
        updated_count += 1
    if updated_count:
        payload["generated_at"] = when
        _save_registry(payload, registry_path)
    return updated_count



def build_source_discovery_candidates(
    normalized_records: list[dict[str, Any]],
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    agent_filter: str | None = None,
    min_support: int = 1,
) -> dict[str, Any]:
    registry_entries = load_source_registry(registry_path)
    known_urls = {_normalize_url(str(entry.get("url", "")).strip()) for entry in registry_entries if str(entry.get("url", "")).strip()}
    existing_candidate_urls = {
        _normalize_url(str(entry.get("url", "")).strip())
        for entry in registry_entries
        if str(entry.get("status", "")).strip().lower() == "candidate" and str(entry.get("url", "")).strip()
    }

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in normalized_records:
        agent_name = str(record.get("agent_name", "")).strip().lower() or str(record.get("layer", "")).strip().lower()
        if agent_filter and agent_name != agent_filter:
            continue
        primary_source = _normalize_url(str(record.get("source_url", "")).strip())
        candidate_pairs: list[tuple[str, str]] = []
        if primary_source and primary_source not in known_urls:
            candidate_pairs.append((primary_source, "primary_source_candidate"))
        for raw_url in record.get("linked_urls", []) or []:
            candidate_url = _normalize_url(str(raw_url).strip())
            if not candidate_url or candidate_url == primary_source:
                continue
            candidate_pairs.append((candidate_url, "linked_evidence_candidate"))

        for candidate_url, discovery_method in candidate_pairs:
            parsed = urlparse(candidate_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if candidate_url in known_urls:
                continue

            key = (agent_name, candidate_url)
            current = grouped.setdefault(
                key,
                {
                    "agent_name": agent_name,
                    "name": _candidate_name_from_url(candidate_url),
                    "url": candidate_url,
                    "source_type": str(record.get("source_type", "")).strip() or "discovered_source",
                    "area_hints": set(),
                    "support_count": 0,
                    "supporting_files": set(),
                    "supporting_layers": set(),
                    "supporting_actors": set(),
                    "supporting_titles": set(),
                    "supporting_source_urls": set(),
                    "discovery_method": discovery_method,
                    "notes": set(),
                    "existing_candidate": candidate_url in existing_candidate_urls,
                },
            )
            current["support_count"] += 1
            area = normalize_area(record.get("research_area"))
            if area:
                current["area_hints"].add(area)
            if str(record.get("file_path", "")).strip():
                current["supporting_files"].add(str(record.get("file_path", "")).strip())
            if str(record.get("layer", "")).strip():
                current["supporting_layers"].add(str(record.get("layer", "")).strip())
            if str(record.get("actor", "")).strip() and str(record.get("actor", "")).strip().lower() != "unknown":
                current["supporting_actors"].add(str(record.get("actor", "")).strip())
            if str(record.get("title", "")).strip():
                current["supporting_titles"].add(str(record.get("title", "")).strip())
            if primary_source:
                current["supporting_source_urls"].add(primary_source)
            focus = str(record.get("focus", "")).strip()
            if focus:
                current["notes"].add(focus)

    candidates: list[dict[str, Any]] = []
    for candidate in grouped.values():
        if int(candidate["support_count"]) < min_support:
            continue
        area_hints = sorted(candidate["area_hints"])
        notes_text = " | ".join(sorted(candidate["notes"]))[:300]
        score = round(
            min(
                1.0,
                0.35
                + min(int(candidate["support_count"]), 5) * 0.1
                + min(len(candidate["supporting_layers"]), 3) * 0.08
                + min(len(area_hints), 3) * 0.05,
            ),
            3,
        )
        candidates.append(
            {
                "agent_name": candidate["agent_name"],
                "name": candidate["name"],
                "url": candidate["url"],
                "source_type": candidate["source_type"],
                "area_hints": area_hints,
                "support_count": candidate["support_count"],
                "supporting_files": sorted(candidate["supporting_files"]),
                "supporting_layers": sorted(candidate["supporting_layers"]),
                "supporting_actors": sorted(candidate["supporting_actors"]),
                "supporting_titles": sorted(candidate["supporting_titles"]),
                "supporting_source_urls": sorted(candidate["supporting_source_urls"]),
                "discovery_score": score,
                "trust_score": _estimate_trust_score(candidate["source_type"], candidate["url"]),
                "relevance_score": _estimate_relevance_score(area_hints, notes_text),
                "existing_candidate": candidate["existing_candidate"],
                "discovery_method": candidate["discovery_method"],
                "notes": notes_text,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["existing_candidate"],
            -float(item["discovery_score"]),
            -int(item["support_count"]),
            item["agent_name"],
            item["name"],
        )
    )
    return {
        "generated_at": _iso_now(),
        "filters": {
            "agent_filter": agent_filter,
            "min_support": min_support,
        },
        "stats": {
            "total_candidates": len(candidates),
            "new_candidates": sum(1 for item in candidates if not item["existing_candidate"]),
            "existing_candidates": sum(1 for item in candidates if item["existing_candidate"]),
        },
        "entries": candidates,
    }


def save_source_discovery_candidates(
    normalized_records: list[dict[str, Any]],
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    output_path: Path = SOURCE_DISCOVERY_PATH,
    agent_filter: str | None = None,
    min_support: int = 1,
) -> SourceDiscoveryResult:
    payload = build_source_discovery_candidates(
        normalized_records,
        registry_path=registry_path,
        agent_filter=agent_filter,
        min_support=min_support,
    )
    _save_source_discovery(payload, output_path)
    stats = payload["stats"]
    return SourceDiscoveryResult(
        total_candidates=stats["total_candidates"],
        new_candidates=stats["new_candidates"],
        existing_candidates=stats["existing_candidates"],
        discovery_path=str(output_path.resolve()),
    )


def register_discovered_candidates(
    *,
    discovery_path: Path = SOURCE_DISCOVERY_PATH,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    agent_filter: str | None = None,
    limit: int | None = None,
) -> list[SourceLifecycleResult]:
    if not discovery_path.exists():
        return []
    try:
        payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    results: list[SourceLifecycleResult] = []
    entries = [item for item in payload.get("entries", []) if isinstance(item, dict)]
    for item in entries:
        if agent_filter and str(item.get("agent_name", "")).strip().lower() != agent_filter:
            continue
        if item.get("existing_candidate"):
            continue
        results.append(
            add_candidate_source(
                agent_name=str(item.get("agent_name", "")).strip(),
                name=str(item.get("name", "")).strip(),
                url=str(item.get("url", "")).strip(),
                source_type=str(item.get("source_type", "")).strip() or "candidate_source",
                area_hints=list(item.get("area_hints", []) or []),
                notes=str(item.get("notes", "")).strip(),
                discovery_method=str(item.get("discovery_method", "")).strip() or "linked_evidence_candidate",
                registry_path=registry_path,
            )
        )
        if limit is not None and len(results) >= limit:
            break
    return results


def _review_status_from_registry_status(status: str) -> str:
    lowered = str(status or "").strip().lower()
    if lowered in {"approved", "active"}:
        return "approved"
    if lowered == "paused":
        return "paused"
    if lowered == "deprecated":
        return "rejected"
    return "pending"


def build_source_review_queue(
    *,
    discovery_path: Path = SOURCE_DISCOVERY_PATH,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    output_path: Path = SOURCE_REVIEW_QUEUE_PATH,
    agent_filter: str | None = None,
) -> SourceReviewQueueResult:
    registry_entries = load_source_registry(registry_path)
    queue_payload = _load_review_queue(output_path)
    prior_entries = {str(item.get("review_id", "")).strip(): item for item in queue_payload.get("entries", []) if isinstance(item, dict)}
    registry_by_source_id = {str(entry.get("source_id", "")).strip(): entry for entry in registry_entries if isinstance(entry, dict)}
    registry_by_key = {
        (str(entry.get("agent_name", "")).strip().lower(), _normalize_url(str(entry.get("url", "")).strip())): entry
        for entry in registry_entries
        if isinstance(entry, dict) and _normalize_url(str(entry.get("url", "")).strip())
    }

    discovery_entries: list[dict[str, Any]] = []
    if discovery_path.exists():
        try:
            payload = json.loads(discovery_path.read_text(encoding="utf-8"))
            discovery_entries = [item for item in payload.get("entries", []) if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            discovery_entries = []

    queue_entries: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()

    def upsert_queue_entry(item: dict[str, Any], *, registry_entry: dict[str, Any] | None, discovery_entry: dict[str, Any] | None) -> None:
        agent_name = str(item.get("agent_name", "")).strip().lower()
        if agent_filter and agent_name != agent_filter:
            return
        name = str(item.get("name", "")).strip() or _candidate_name_from_url(str(item.get("url", "")).strip())
        url = _normalize_url(str(item.get("url", "")).strip())
        if not url:
            return
        review_id = _make_review_id(agent_name, name, url)
        prior = prior_entries.get(review_id, {})
        source_id = ""
        if registry_entry:
            source_id = str(registry_entry.get("source_id", "")).strip()
        area_hints = sorted(
            {
                area
                for area in [normalize_area(value) for value in list(item.get("area_hints", []) or [])]
                if area
            }
        )
        review_status = str(prior.get("review_status", "")).strip().lower() or _review_status_from_registry_status(
            str(registry_entry.get("status", "") if registry_entry else "candidate")
        )
        rationale = str(prior.get("rationale", "")).strip()
        history = prior.get("history", [])
        if not isinstance(history, list):
            history = []
        queue_entries.append(
            {
                "review_id": review_id,
                "source_id": source_id,
                "agent_name": agent_name,
                "name": name,
                "url": url,
                "source_type": str(item.get("source_type", "")).strip() or "candidate_source",
                "area_hints": area_hints,
                "trust_score": float(item.get("trust_score", registry_entry.get("trust_score", 0.7) if registry_entry else 0.7) or 0.7),
                "relevance_score": float(item.get("relevance_score", registry_entry.get("relevance_score", 0.7) if registry_entry else 0.7) or 0.7),
                "support_count": int(item.get("support_count", 0) or 0),
                "discovery_score": float(item.get("discovery_score", 0.0) or 0.0),
                "discovery_method": str(item.get("discovery_method", registry_entry.get("discovery_method", "") if registry_entry else "")).strip() or "candidate_source",
                "supporting_layers": list(item.get("supporting_layers", [])) if discovery_entry else list(prior.get("supporting_layers", [])),
                "supporting_titles": list(item.get("supporting_titles", [])) if discovery_entry else list(prior.get("supporting_titles", [])),
                "supporting_files": list(item.get("supporting_files", [])) if discovery_entry else list(prior.get("supporting_files", [])),
                "registry_status": str(registry_entry.get("status", "")).strip().lower() if registry_entry else "",
                "review_status": review_status,
                "rationale": rationale,
                "history": history,
                "created_at": str(prior.get("created_at", "")).strip() or _iso_now(),
                "updated_at": _iso_now(),
            }
        )
        seen_review_ids.add(review_id)

    for discovery_entry in discovery_entries:
        agent_name = str(discovery_entry.get("agent_name", "")).strip().lower()
        url = _normalize_url(str(discovery_entry.get("url", "")).strip())
        registry_entry = registry_by_key.get((agent_name, url))
        upsert_queue_entry(discovery_entry, registry_entry=registry_entry, discovery_entry=discovery_entry)

    for registry_entry in registry_entries:
        if not isinstance(registry_entry, dict):
            continue
        status = str(registry_entry.get("status", "")).strip().lower()
        if status != "candidate":
            continue
        upsert_queue_entry(registry_entry, registry_entry=registry_entry, discovery_entry=None)

    for review_id, prior in prior_entries.items():
        if review_id in seen_review_ids:
            continue
        if agent_filter and str(prior.get("agent_name", "")).strip().lower() != agent_filter:
            continue
        queue_entries.append(prior)

    queue_entries.sort(key=lambda item: (str(item.get("review_status", "")) != "pending", -float(item.get("discovery_score", 0.0) or 0.0), str(item.get("agent_name", "")), str(item.get("name", ""))))
    pending_entries = sum(1 for item in queue_entries if str(item.get("review_status", "")).strip().lower() == "pending")
    decided_entries = len(queue_entries) - pending_entries
    payload = {
        "generated_at": _iso_now(),
        "filters": {"agent_filter": agent_filter},
        "stats": {
            "total_entries": len(queue_entries),
            "pending_entries": pending_entries,
            "decided_entries": decided_entries,
        },
        "entries": queue_entries,
    }
    _save_review_queue(payload, output_path)
    return SourceReviewQueueResult(
        total_entries=len(queue_entries),
        pending_entries=pending_entries,
        decided_entries=decided_entries,
        review_queue_path=str(output_path.resolve()),
    )


def rescore_source(
    *,
    source_id: str,
    trust_score: float | None = None,
    relevance_score: float | None = None,
    notes: str | None = None,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceLifecycleResult:
    payload = _load_registry(registry_path)
    entries = payload.get("entries", [])
    when = _iso_now()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source_id") != source_id:
            continue
        old_status = str(entry.get("status", "candidate")).strip().lower() or "candidate"
        if trust_score is not None:
            entry["trust_score"] = round(max(min(float(trust_score), 1.0), 0.0), 2)
        if relevance_score is not None:
            entry["relevance_score"] = round(max(min(float(relevance_score), 1.0), 0.0), 2)
        if notes:
            entry["notes"] = notes
            _append_review_history(entry, action="rescore", rationale=notes)
        entry["updated_at"] = when
        payload["generated_at"] = when
        _rebuild_registry_stats(payload)
        _save_registry(payload, registry_path)
        return SourceLifecycleResult(
            source_id=source_id,
            old_status=old_status,
            new_status=old_status,
            registry_path=str(registry_path.resolve()),
        )
    raise KeyError(f"Unknown source_id: {source_id}")


def review_source_candidate(
    *,
    review_id: str,
    action: str,
    rationale: str,
    activate: bool = False,
    trust_score: float | None = None,
    relevance_score: float | None = None,
    queue_path: Path = SOURCE_REVIEW_QUEUE_PATH,
    registry_path: Path = SOURCE_REGISTRY_PATH,
) -> SourceReviewResult:
    normalized_action = str(action).strip().lower()
    if normalized_action not in {"approve", "reject", "pause", "rescore"}:
        raise ValueError("action must be one of approve, reject, pause, rescore")

    queue_payload = _load_review_queue(queue_path)
    entries = [item for item in queue_payload.get("entries", []) if isinstance(item, dict)]
    target: dict[str, Any] | None = None
    for item in entries:
        if str(item.get("review_id", "")).strip() == review_id:
            target = item
            break
    if target is None:
        raise KeyError(f"Unknown review_id: {review_id}")

    source_id = str(target.get("source_id", "")).strip()
    if not source_id:
        lifecycle = add_candidate_source(
            agent_name=str(target.get("agent_name", "")).strip(),
            name=str(target.get("name", "")).strip(),
            url=str(target.get("url", "")).strip(),
            source_type=str(target.get("source_type", "")).strip() or "candidate_source",
            area_hints=list(target.get("area_hints", []) or []),
            notes=rationale,
            discovery_method=str(target.get("discovery_method", "")).strip() or "review_queue_candidate",
            registry_path=registry_path,
        )
        source_id = lifecycle.source_id
        target["source_id"] = source_id

    if normalized_action == "approve":
        promote_candidate_source(source_id=source_id, activate=activate, notes=rationale, registry_path=registry_path)
        review_status = "approved"
    elif normalized_action == "reject":
        deprecate_source(source_id=source_id, notes=rationale, registry_path=registry_path)
        review_status = "rejected"
    elif normalized_action == "pause":
        pause_source(source_id=source_id, notes=rationale, registry_path=registry_path)
        review_status = "paused"
    else:
        rescore_source(
            source_id=source_id,
            trust_score=trust_score,
            relevance_score=relevance_score,
            notes=rationale,
            registry_path=registry_path,
        )
        review_status = str(target.get("review_status", "pending")).strip().lower() or "pending"

    target["review_status"] = review_status
    target["rationale"] = rationale
    target["updated_at"] = _iso_now()
    target["history"] = list(target.get("history", [])) if isinstance(target.get("history", []), list) else []
    target["history"].append(
        {
            "reviewed_at": _iso_now(),
            "action": normalized_action,
            "rationale": rationale,
            "activate": activate,
            "trust_score": trust_score,
            "relevance_score": relevance_score,
        }
    )
    registry_entries = load_source_registry(registry_path)
    registry_entry = next((entry for entry in registry_entries if str(entry.get("source_id", "")).strip() == source_id), None)
    if registry_entry:
        target["registry_status"] = str(registry_entry.get("status", "")).strip().lower()
        target["trust_score"] = float(registry_entry.get("trust_score", target.get("trust_score", 0.7)) or 0.7)
        target["relevance_score"] = float(registry_entry.get("relevance_score", target.get("relevance_score", 0.7)) or 0.7)

    queue_payload["generated_at"] = _iso_now()
    queue_payload["stats"] = {
        "total_entries": len(entries),
        "pending_entries": sum(1 for item in entries if str(item.get("review_status", "")).strip().lower() == "pending"),
        "decided_entries": sum(1 for item in entries if str(item.get("review_status", "")).strip().lower() != "pending"),
    }
    _save_review_queue(queue_payload, queue_path)
    return SourceReviewResult(
        review_id=review_id,
        action=normalized_action,
        review_status=review_status,
        source_id=source_id,
        queue_path=str(queue_path.resolve()),
        registry_path=str(registry_path.resolve()),
    )


def _source_reference_time(entry: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "last_checked", "last_changed", "created_at"):
        parsed = _parse_iso(str(entry.get(key, "")).strip())
        if parsed is not None:
            return parsed
    return None


def export_source_health_summary(
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    refresh_plan_path: Path = REFRESH_PLAN_PATH,
    review_queue_path: Path = SOURCE_REVIEW_QUEUE_PATH,
    output_path: Path = SOURCE_HEALTH_SUMMARY_PATH,
    agent_filter: str | None = None,
) -> SourceHealthSummaryResult:
    entries = load_source_registry(registry_path)
    if agent_filter:
        entries = [entry for entry in entries if str(entry.get("agent_name", "")).strip().lower() == agent_filter]

    now = datetime.now(UTC)
    status_counts = _status_counts(entries)
    by_agent: dict[str, dict[str, Any]] = {}
    due_entries = 0
    attention_entries: list[dict[str, Any]] = []
    trust_total = 0.0
    relevance_total = 0.0
    for entry in entries:
        agent_name = str(entry.get("agent_name", "")).strip().lower() or "unknown"
        bucket = by_agent.setdefault(
            agent_name,
            {"total_entries": 0, "status_counts": {}, "due_for_refresh": 0, "average_trust_score": 0.0, "average_relevance_score": 0.0},
        )
        bucket["total_entries"] += 1
        status = str(entry.get("status", "active")).strip().lower() or "active"
        bucket["status_counts"][status] = bucket["status_counts"].get(status, 0) + 1
        trust = float(entry.get("trust_score", 0.7) or 0.7)
        relevance = float(entry.get("relevance_score", 0.7) or 0.7)
        trust_total += trust
        relevance_total += relevance
        bucket["average_trust_score"] += trust
        bucket["average_relevance_score"] += relevance
        decision = evaluate_source_refresh(entry, reference_time=now)
        if decision["refresh_due"]:
            due_entries += 1
            bucket["due_for_refresh"] += 1
        attention_score = 0
        if status in {"candidate", "stale", "paused"}:
            attention_score += 3
        if decision["refresh_due"]:
            attention_score += 2
        if relevance < 0.6:
            attention_score += 1
        if attention_score:
            attention_entries.append(
                {
                    "source_id": entry.get("source_id"),
                    "agent_name": agent_name,
                    "name": entry.get("name"),
                    "status": status,
                    "refresh_reason": decision["refresh_reason"],
                    "attention_score": attention_score,
                    "trust_score": trust,
                    "relevance_score": relevance,
                    "url": entry.get("url"),
                }
            )

    for bucket in by_agent.values():
        total = max(int(bucket["total_entries"]), 1)
        bucket["average_trust_score"] = round(bucket["average_trust_score"] / total, 3)
        bucket["average_relevance_score"] = round(bucket["average_relevance_score"] / total, 3)

    pending_review_entries = 0
    if review_queue_path.exists():
        try:
            review_payload = json.loads(review_queue_path.read_text(encoding="utf-8"))
            pending_review_entries = int(review_payload.get("stats", {}).get("pending_entries", 0) or 0)
        except (OSError, json.JSONDecodeError):
            pending_review_entries = 0

    refresh_stats: dict[str, Any] = {}
    if refresh_plan_path.exists():
        try:
            refresh_payload = json.loads(refresh_plan_path.read_text(encoding="utf-8"))
            refresh_stats = dict(refresh_payload.get("stats", {}))
        except (OSError, json.JSONDecodeError):
            refresh_stats = {}

    payload = {
        "generated_at": _iso_now(),
        "filters": {"agent_filter": agent_filter},
        "counts": {
            "total_entries": len(entries),
            "due_entries": due_entries,
            "pending_review_entries": pending_review_entries,
            "candidate_entries": status_counts.get("candidate", 0),
            "stale_entries": status_counts.get("stale", 0),
            "deprecated_entries": status_counts.get("deprecated", 0),
        },
        "status_counts": status_counts,
        "refresh_plan_stats": refresh_stats,
        "averages": {
            "trust_score": round(trust_total / len(entries), 3) if entries else 0.0,
            "relevance_score": round(relevance_total / len(entries), 3) if entries else 0.0,
        },
        "by_agent": by_agent,
        "attention_queue": sorted(attention_entries, key=lambda item: (-int(item["attention_score"]), item["agent_name"], item["name"]))[:50],
    }
    _save_source_health_summary(payload, output_path)
    return SourceHealthSummaryResult(
        total_entries=len(entries),
        due_entries=due_entries,
        pending_review_entries=pending_review_entries,
        summary_path=str(output_path.resolve()),
    )


def prune_sources(
    *,
    registry_path: Path = SOURCE_REGISTRY_PATH,
    output_path: Path = SOURCE_PRUNE_REPORT_PATH,
    agent_filter: str | None = None,
    statuses: list[str] | None = None,
    older_than_days: int = 30,
    dry_run: bool = True,
) -> SourcePruneResult:
    payload = _load_registry(registry_path)
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    target_statuses = {str(item).strip().lower() for item in (statuses or ["deprecated"])}
    now = datetime.now(UTC)
    threshold = timedelta(days=max(int(older_than_days), 0))
    kept_entries: list[dict[str, Any]] = []
    pruned_entries: list[dict[str, Any]] = []

    for entry in entries:
        agent_name = str(entry.get("agent_name", "")).strip().lower()
        status = str(entry.get("status", "")).strip().lower()
        if agent_filter and agent_name != agent_filter:
            kept_entries.append(entry)
            continue
        reference_time = _source_reference_time(entry)
        old_enough = True if reference_time is None else now - reference_time >= threshold
        if status in target_statuses and old_enough:
            pruned_entries.append(
                {
                    "source_id": entry.get("source_id"),
                    "agent_name": agent_name,
                    "name": entry.get("name"),
                    "status": status,
                    "url": entry.get("url"),
                    "reference_time": reference_time.isoformat() if reference_time else "",
                }
            )
            continue
        kept_entries.append(entry)

    report = {
        "generated_at": _iso_now(),
        "filters": {
            "agent_filter": agent_filter,
            "statuses": sorted(target_statuses),
            "older_than_days": older_than_days,
            "dry_run": dry_run,
        },
        "stats": {
            "pruned_count": len(pruned_entries),
            "kept_count": len(kept_entries),
        },
        "entries": pruned_entries,
    }
    _save_source_prune_report(report, output_path)

    if not dry_run and pruned_entries:
        payload["entries"] = kept_entries
        payload["generated_at"] = _iso_now()
        _rebuild_registry_stats(payload)
        _save_registry(payload, registry_path)

    return SourcePruneResult(
        pruned_count=len(pruned_entries),
        dry_run=dry_run,
        prune_report_path=str(output_path.resolve()),
        registry_path=str(registry_path.resolve()),
    )


def rescore_sources_from_recent_evidence(
    *,
    outputs_root: Path = PROJECT_ROOT / "outputs",
    registry_path: Path = SOURCE_REGISTRY_PATH,
    output_path: Path = SOURCE_HEALTH_SUMMARY_PATH,
    agent_filter: str | None = None,
    lookback_days: int = 120,
) -> SourceEvidenceRescoreResult:
    records_path = outputs_root / "normalized" / "records.json"
    if not records_path.exists():
        try:
            from core.normalization import save_normalized_collection_records

            save_normalized_collection_records(outputs_root, outputs_root)
        except Exception:
            pass
    try:
        records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else []
    except (OSError, json.JSONDecodeError):
        records = []
    normalized_records = [item for item in records if isinstance(item, dict)]

    payload = _load_registry(registry_path)
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    now = datetime.now(UTC)
    threshold = now - timedelta(days=max(int(lookback_days), 1))
    updated_count = 0
    for entry in entries:
        agent_name = str(entry.get("agent_name", "")).strip().lower()
        if agent_filter and agent_name != agent_filter:
            continue
        entry_url = _normalize_url(str(entry.get("url", "")).strip())
        if not entry_url:
            continue
        support = [
            record
            for record in normalized_records
            if _normalize_url(str(record.get("source_url", "")).strip()) == entry_url
            and (not agent_filter or str(record.get("agent_name", "")).strip().lower() == agent_filter)
            and (_parse_iso(str(record.get("collected_at", "")).strip()) or now) >= threshold
        ]
        if not support:
            continue
        support_count = len(support)
        supported_areas = {
            normalize_area(record.get("research_area"))
            for record in support
            if normalize_area(record.get("research_area"))
        }
        area_overlap = len(set(entry.get("area_hints", []) or []) & supported_areas)
        new_relevance = round(
            min(
                0.95,
                max(
                    float(entry.get("relevance_score", 0.7) or 0.7),
                    0.45 + min(support_count, 5) * 0.08 + min(area_overlap, 3) * 0.04,
                ),
            ),
            2,
        )
        new_trust = round(
            min(
                0.95,
                max(
                    float(entry.get("trust_score", 0.7) or 0.7),
                    float(entry.get("trust_score", 0.7) or 0.7) + min(support_count, 4) * 0.02,
                ),
            ),
            2,
        )
        if new_relevance == float(entry.get("relevance_score", 0.7) or 0.7) and new_trust == float(entry.get("trust_score", 0.7) or 0.7):
            continue
        entry["relevance_score"] = new_relevance
        entry["trust_score"] = new_trust
        entry["updated_at"] = _iso_now()
        _append_review_history(
            entry,
            action="rescore_recent_evidence",
            rationale=f"Updated from {support_count} recent normalized record(s); area_overlap={area_overlap}.",
        )
        updated_count += 1

    if updated_count:
        payload["entries"] = entries
        payload["generated_at"] = _iso_now()
        _rebuild_registry_stats(payload)
        _save_registry(payload, registry_path)

    summary = export_source_health_summary(
        registry_path=registry_path,
        output_path=output_path,
        agent_filter=agent_filter,
    )
    return SourceEvidenceRescoreResult(
        updated_count=updated_count,
        summary_path=summary.summary_path,
        registry_path=str(registry_path.resolve()),
    )
