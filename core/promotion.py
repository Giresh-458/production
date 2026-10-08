from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMOTION_OVERRIDES_PATH = PROJECT_ROOT / "outputs" / "synthesis" / "manifest" / "promotion_overrides.json"
ALLOWED_PROMOTION_STATUSES = {
    "watchlist",
    "active_research_candidate",
    "high-priority_proposal_candidate",
    "hold",
}


@dataclass(slots=True)
class PromotionOverrideResult:
    synthesized_file: str
    status: str
    overrides_path: str


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _priority_label_for_status(status: str) -> str:
    mapping = {
        "watchlist": "watch",
        "active_research_candidate": "medium",
        "high-priority_proposal_candidate": "high",
        "hold": "low",
    }
    return mapping.get(status, "low")


def load_promotion_overrides(path: Path = PROMOTION_OVERRIDES_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": "", "entries": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"generated_at": "", "entries": []}


def save_promotion_overrides(payload: dict[str, Any], path: Path = PROMOTION_OVERRIDES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def set_promotion_override(
    *,
    synthesized_file: str,
    status: str,
    rationale: str,
    path: Path = PROMOTION_OVERRIDES_PATH,
) -> PromotionOverrideResult:
    normalized_status = str(status).strip()
    if normalized_status not in ALLOWED_PROMOTION_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_PROMOTION_STATUSES)}")

    normalized_file = str(Path(synthesized_file).resolve())
    payload = load_promotion_overrides(path)
    entries = [item for item in payload.get("entries", []) if isinstance(item, dict)]
    entry = next((item for item in entries if str(item.get("synthesized_file", "")).strip() == normalized_file), None)
    if entry is None:
        entry = {"synthesized_file": normalized_file, "history": []}
        entries.append(entry)

    entry["status"] = normalized_status
    entry["rationale"] = rationale.strip()
    entry["updated_at"] = _iso_now()
    entry["history"] = list(entry.get("history", [])) if isinstance(entry.get("history", []), list) else []
    entry["history"].append(
        {
            "updated_at": entry["updated_at"],
            "status": normalized_status,
            "rationale": entry["rationale"],
        }
    )
    payload["generated_at"] = _iso_now()
    payload["entries"] = sorted(entries, key=lambda item: str(item.get("synthesized_file", "")))
    save_promotion_overrides(payload, path)
    return PromotionOverrideResult(
        synthesized_file=normalized_file,
        status=normalized_status,
        overrides_path=str(path.resolve()),
    )


def clear_promotion_override(
    *,
    synthesized_file: str,
    path: Path = PROMOTION_OVERRIDES_PATH,
) -> PromotionOverrideResult:
    normalized_file = str(Path(synthesized_file).resolve())
    payload = load_promotion_overrides(path)
    entries = [item for item in payload.get("entries", []) if isinstance(item, dict)]
    retained = [item for item in entries if str(item.get("synthesized_file", "")).strip() != normalized_file]
    payload["generated_at"] = _iso_now()
    payload["entries"] = retained
    save_promotion_overrides(payload, path)
    return PromotionOverrideResult(
        synthesized_file=normalized_file,
        status="cleared",
        overrides_path=str(path.resolve()),
    )


def apply_promotion_overrides(
    entries: Iterable[Dict[str, Any]],
    *,
    overrides_path: Path = PROMOTION_OVERRIDES_PATH,
) -> list[Dict[str, Any]]:
    payload = load_promotion_overrides(overrides_path)
    overrides = {
        str(item.get("synthesized_file", "")).strip(): item
        for item in payload.get("entries", [])
        if isinstance(item, dict) and str(item.get("synthesized_file", "")).strip()
    }
    updated_entries: list[Dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        synthesized_file = str(item.get("synthesized_file", "")).strip()
        override = overrides.get(synthesized_file)
        if override:
            promotion = dict(item.get("promotion", {})) if isinstance(item.get("promotion"), dict) else {}
            promotion["status"] = str(override.get("status", promotion.get("status", "watchlist"))).strip()
            promotion["priority_label"] = _priority_label_for_status(promotion["status"])
            promotion["promotion_ready"] = promotion["status"] != "hold"
            if str(override.get("rationale", "")).strip():
                promotion["rationale"] = str(override.get("rationale", "")).strip()
            promotion["manual_override"] = True
            promotion["manual_override_updated_at"] = str(override.get("updated_at", "")).strip()
            item["promotion"] = promotion
        updated_entries.append(item)
    return updated_entries


FUNDING_SIGNAL_KEYWORDS = (
    "funding",
    "grant",
    "grants",
    "sponsor",
    "sponsors",
    "investment",
    "investor",
    "portfolio",
    "hackathon",
    "challenge",
    "bounty",
    "builder program",
    "ecosystem fund",
    "proposal",
)

BENCHMARK_SIGNAL_KEYWORDS = (
    "dataset",
    "benchmark",
    "simulator",
    "test suite",
    "evaluation",
    "registry",
    "explorer",
    "transparency",
    "proof of reserve",
    "relay data",
    "telemetry",
    "trace",
    "conformance",
    "metrics",
    "dashboard",
)


def cleaned_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _joined_sections_text(sections: Dict[str, str]) -> str:
    return " ".join(cleaned_text(value) for value in sections.values() if cleaned_text(value)).lower()


def _keyword_score(text: str, keywords: Iterable[str], *, weight: int = 2, cap: int = 12) -> int:
    if not text:
        return 0
    score = 0
    for keyword in keywords:
        if keyword in text:
            score += weight
    return min(score, cap)


def compute_funding_relevance(entry: Dict[str, Any], sections: Dict[str, str]) -> int:
    layers = [cleaned_text(item) for item in entry.get("layers_covered", []) if cleaned_text(item)]
    base = 0
    if any(layer in {"Funding", "Investment", "Hackathon"} for layer in layers):
        base += 8
    if any(layer in {"Company", "Regulation"} for layer in layers):
        base += 3
    text = " ".join(
        [
            cleaned_text(entry.get("title", "")),
            _joined_sections_text(sections),
            " ".join(cleaned_text(item) for item in entry.get("source_urls", []) if cleaned_text(item)),
        ]
    ).lower()
    return min(base + _keyword_score(text, FUNDING_SIGNAL_KEYWORDS, weight=2, cap=12), 20)


def compute_benchmark_availability(entry: Dict[str, Any], sections: Dict[str, str]) -> int:
    layers = [cleaned_text(item) for item in entry.get("layers_covered", []) if cleaned_text(item)]
    base = 0
    if "DataAvailability" in layers:
        base += 8
    if any(layer in {"Failure", "OpenSource", "Literature"} for layer in layers):
        base += 3
    text = " ".join(
        [
            cleaned_text(sections.get("Idea", "")),
            cleaned_text(sections.get("Gap", "")),
            cleaned_text(sections.get("Evidence Inputs", "")),
            cleaned_text(sections.get("Why Important", "")),
            cleaned_text(sections.get("Existing Solutions", "")),
        ]
    ).lower()
    return min(base + _keyword_score(text, BENCHMARK_SIGNAL_KEYWORDS, weight=2, cap=12), 20)


def compute_promotion_decision(entry: Dict[str, Any], sections: Dict[str, str]) -> Dict[str, Any]:
    ranking = entry.get("ranking", {}) if isinstance(entry.get("ranking"), dict) else {}
    breakdown = ranking.get("breakdown", {}) if isinstance(ranking.get("breakdown"), dict) else {}
    
    independent_source_count = entry.get("independent_source_count", 0)
    layer_count = len({cleaned_text(item) for item in entry.get("layers_covered", []) if cleaned_text(item)})
    
    specificity = int(breakdown.get("specificity", 0))
    source_quality = int(breakdown.get("source_trust", 0))
    feasibility = int(breakdown.get("prototype_feasibility", 0))
    
    # Check novelty status from entry if it exists (from ExistingSolutionSearch)
    novelty_status = entry.get("novelty_status", "UNKNOWN")
    has_unresolved_gap = novelty_status in {"POTENTIAL_GAP", "WEAK_OVERLAP", "UNKNOWN", "NO_SEARCH"}
    
    evidence_input_count = len(entry.get("source_urls", []))
    
    status = "SIGNAL"
    rationale = "Evidence exists."
    
    # Check progression
    if specificity >= 4 and evidence_input_count >= 1:
        status = "CANDIDATE_PROBLEM"
        rationale = "Specific problem + direct evidence."
        
    if status == "CANDIDATE_PROBLEM" and independent_source_count >= 2:
        status = "CORROBORATED_PROBLEM"
        rationale = "Independent corroboration."
        
    if status == "CORROBORATED_PROBLEM" and has_unresolved_gap and feasibility >= 4:
        status = "RESEARCH_CANDIDATE"
        rationale = "Corroborated + researchable + unresolved gap."
        
    if status == "RESEARCH_CANDIDATE" and source_quality >= 8 and layer_count >= 2 and novelty_status == "POTENTIAL_GAP" and feasibility >= 8:
        status = "HIGH_PRIORITY_RESEARCH_CANDIDATE"
        rationale = "Strong evidence + cross-layer corroboration + meaningful remaining gap + feasible evaluation."
        
    # Also set legacy fields for backward compatibility in case they are used in UI
    legacy_status = "hold"
    if status == "HIGH_PRIORITY_RESEARCH_CANDIDATE": legacy_status = "high-priority_proposal_candidate"
    elif status == "RESEARCH_CANDIDATE": legacy_status = "active_research_candidate"
    elif status in {"CORROBORATED_PROBLEM", "CANDIDATE_PROBLEM"}: legacy_status = "watchlist"
    
    priority_label = "low"
    if legacy_status == "high-priority_proposal_candidate":
        priority_label = "high"
    elif legacy_status == "active_research_candidate":
        priority_label = "medium"
    elif legacy_status == "watchlist":
        priority_label = "watch"

    return {
        "status": status,
        "rationale": rationale,
        "legacy_status": legacy_status,
        "priority_label": priority_label,
        "missing_requirements": [],
        "scores": {
            "layer_count": layer_count,
            "source_quality": source_quality,
            "prototype_feasibility": feasibility,
            "evidence_input_count": evidence_input_count,
        },
    }


def build_promotion_index(entries: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    items = [entry for entry in entries if isinstance(entry, dict)]
    order = {
        "high-priority_proposal_candidate": 0,
        "active_research_candidate": 1,
        "watchlist": 2,
        "hold": 3,
    }
    items.sort(
        key=lambda item: (
            order.get(str(item.get("promotion", {}).get("status", "hold")), 9),
            -int(item.get("promotion", {}).get("scores", {}).get("ranking_score", 0) or 0),
            str(item.get("area", "")),
            str(item.get("title", "")),
        )
    )
    return items


def build_promotion_summary(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = [entry for entry in entries if isinstance(entry, dict)]
    status_counts = Counter(str(item.get("promotion", {}).get("status", "hold")) for item in items)
    promoted = [item for item in items if str(item.get("promotion", {}).get("status", "hold")) != "hold"]
    return {
        "generated_at": None,
        "counts": {
            "entries": len(items),
            "promoted": len(promoted),
            "high_priority_proposal_candidates": status_counts.get("high-priority_proposal_candidate", 0),
            "active_research_candidates": status_counts.get("active_research_candidate", 0),
            "watchlist": status_counts.get("watchlist", 0),
            "hold": status_counts.get("hold", 0),
        },
        "top_candidates": [
            {
                "title": item.get("title"),
                "area": item.get("area"),
                "synthesized_file": item.get("synthesized_file"),
                "status": item.get("promotion", {}).get("status"),
                "ranking_score": item.get("promotion", {}).get("scores", {}).get("ranking_score"),
                "rationale": item.get("promotion", {}).get("rationale"),
            }
            for item in promoted[:20]
        ],
    }
