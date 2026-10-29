from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, Optional

@dataclass(slots=True)
class CollectionResult:
    """Tracks the complete state of a single funding source collection run.

    ``pagination_complete`` may only be True when the collector has positive
    evidence that the source's discoverable pagination/traversal space has
    been exhausted.  ``discovery_exhausted`` indicates the configured
    discovery boundary was fully explored (distinct from a limit-based stop).
    """
    source_id: str
    method: str
    # --- page-level metrics ---
    pages_discovered: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    # --- detail-page metrics ---
    detail_pages_discovered: int = 0
    detail_pages_fetched: int = 0
    detail_pages_failed: int = 0
    # --- document (PDF etc.) metrics ---
    documents_discovered: int = 0
    documents_fetched: int = 0
    documents_failed: int = 0
    # --- API-specific metrics ---
    api_pages_fetched: int = 0
    api_records_received: int = 0
    api_expected_records: Optional[int] = None
    # --- record-level metrics ---
    records_discovered: int = 0
    records_valid: int = 0
    records_failed: int = 0
    records_filtered: int = 0
    records_filtered_by_reason: dict[str, int] = field(default_factory=dict)
    records_deduplicated: int = 0
    duplicates_removed: int = 0
    urls_skipped: int = 0
    relevant_urls_skipped: int = 0
    coverage_gaps: list[dict[str, Any]] = field(default_factory=list)
    # --- traversal state ---
    max_depth: Optional[int] = None
    max_depth_reached: int = 0
    # --- completeness flags ---
    pagination_detected: bool = False
    pagination_complete: bool = False
    discovery_exhausted: bool = False
    truncated: bool = False
    # Exact URLs/reasons explain source truncation; a boolean alone is not actionable.
    truncation_details: list[dict[str, Any]] = field(default_factory=list)
    evidence_verified: bool = False
    currentness_verified: bool = False
    # Empty status is valid only when the source explicitly confirms no active calls.
    no_active_opportunities_confirmed: bool = False
    # --- error tracking ---
    failed_urls: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    # --- final status ---
    final_status: str = "UNKNOWN"
    # --- normalized opportunities: list of (FundingDocument, FundingOpportunity) ---
    opportunities: list[Any] = field(default_factory=list)

ALLOWED_STATUSES = {"success", "partial_success", "partial_limit", "error"}
ALLOWED_MODES = {"configured_scan", "manual_url", "manual_text"}
ALLOWED_RESEARCH_AREAS = {"RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}
AREA_ALIASES = {
    "rwa": "RWA",
    "esg": "ESG",
    "zk-iov": "ZK-IoV",
    "zkiov": "ZK-IoV",
    "zk_iov": "ZK-IoV",
    "did": "DID",
    "depin": "DePIN",
    "mev": "MEV",
    "stablecoins": "Stablecoins",
    "stablecoin": "Stablecoins",
}
AREA_KEYWORDS = {
    "RWA": [
        "rwa",
        "real-world asset",
        "real world asset",
        "tokenized treasury",
        "tokenized fund",
        "tokenized security",
        "asset tokenization",
        "proof of reserve",
    ],
    "ESG": [
        "esg",
        "carbon",
        "carbon credit",
        "mrv",
        "sustainability",
        "emissions",
        "registry",
        "climate change adaptation",
        "climate change mitigation",
        "climate adaptation",
        "climate mitigation",
        "climate resilience",
        "climate finance",
        "transport decarbonization",
        "transport decarbonisation",
        "climate",
    ],
    "ZK-IoV": [
        "zero-knowledge",
        "zero knowledge",
        "zk",
        "zkvm",
        "v2x",
        "internet of vehicles",
        "iov",
        "vehicular",
        "mobility",
        "proving",
        "verifier",
    ],
    "DID": [
        "decentralized identity",
        "decentralised identity",
        "self-sovereign identity",
        "decentralized identifier",
        "decentralised identifier",
        "did method",
        "did document",
        "verifiable credential",
        "verifiable credentials",
        "credential",
        "identity wallet",
        "attestation",
    ],
    "DePIN": [
        "depin",
        "decentralized physical infrastructure",
        "filecoin network",
        "wireless",
        "storage network",
        "mapping",
        "sensor",
        "iot",
        "compute network",
    ],
    "MEV": [
        "mev",
        "block builder",
        "transaction ordering",
        "order flow",
        "sequencer",
        "relay",
        "liquidation cascade",
    ],
    "Stablecoins": [
        "stablecoin",
        "stablecoins",
        "payment rail",
        "settlement",
        "tokenized deposit",
        "usdc",
        "pyusd",
        "depeg",
        "vault",
        "reserve",
    ],
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_utc(value: Optional[datetime] = None) -> str:
    current = value or utc_now()
    return current.astimezone(UTC).isoformat()


def normalize_area(area: Optional[str]) -> Optional[str]:
    if area is None:
        return None
    cleaned = str(area).strip()
    if not cleaned:
        return None
    return AREA_ALIASES.get(cleaned.lower(), cleaned)


def _keyword_occurrences(text: str, keyword: str) -> int:
    """Count whole-word/phrase matches without substring false positives.

    Short research-area aliases such as ``did`` and ``zk`` must not match inside
    unrelated words (for example ``candidate``). Hyphens and underscores are
    treated as separators so phrases such as ``zero-knowledge`` still match.
    """
    normalized_text = re.sub(r"[-_/]+", " ", (text or "").lower())
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
    normalized_keyword = re.sub(r"[-_/]+", " ", (keyword or "").lower()).strip()
    if not normalized_keyword:
        return 0
    pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
    return len(re.findall(pattern, normalized_text))


# Required anchors prevent broad adjacent topics from masquerading as a RIF domain.
# In particular, climate science and the acronym ESGF (Earth System Grid Federation)
# are not, by themselves, evidence of ESG investing, carbon accounting, or MRV work.
DOMAIN_REQUIRED_ANCHOR_GROUPS = {
    "ESG": (
        frozenset({
            "esg", "carbon", "carbon credit", "mrv", "emissions",
            "sustainability reporting", "sustainability-linked",
            "environmental social governance", "climate finance",
            "climate change adaptation", "climate change mitigation",
            "climate adaptation", "climate mitigation", "climate resilience",
            "transport decarbonization", "transport decarbonisation",
        }),
    ),
    "ZK-IoV": (
        frozenset({
            "zero-knowledge", "zkvm", "zk proof", "zk proofs", "zkp",
            "zk-snark", "zk-stark",
        }),
        frozenset({
            "internet of vehicles", "vehicular", "v2x", "connected vehicle",
            "vehicle-to-everything", "vehicle-to-infrastructure",
        }),
    ),
}


def _normalize_research_term(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", str(value or "").casefold())).strip()


def has_required_domain_anchor_groups(domain: str, matched_terms: Iterable[str]) -> bool:
    """Return whether evidence contains every required anchor group for a domain."""
    normalized_domain = normalize_area(domain)
    groups = DOMAIN_REQUIRED_ANCHOR_GROUPS.get(str(normalized_domain or ""))
    if not groups:
        return True
    matches = {_normalize_research_term(term) for term in (matched_terms or ())}
    return all(
        any(_normalize_research_term(anchor) in matches for anchor in group)
        for group in groups
    )


def score_research_areas(text: str) -> Dict[str, int]:
    """Count area keywords while enforcing composite-domain evidence."""
    scores: Dict[str, int] = {}
    for area, keywords in AREA_KEYWORDS.items():
        matched = [keyword for keyword in keywords if _keyword_occurrences(text, keyword)]
        score = sum(_keyword_occurrences(text, keyword) for keyword in keywords)
        if not has_required_domain_anchor_groups(area, matched):
            score = 0
        scores[area] = score
    return scores


# Generic terms such as "health", "sensor", "credential", "registry", and
# "validation" occur across many research areas.  They are useful signals but
# must not dominate domain attribution on their own.  Multi-word/domain-specific
# phrases receive higher weights.
DOMAIN_TERM_WEIGHTS = {
    "RWA": {
        "real-world asset": 3.0, "real world asset": 3.0, "tokenized treasury": 3.0,
        "tokenized fund": 3.0, "tokenized security": 3.0, "asset tokenization": 3.0,
        "proof of reserve": 2.5, "rwa": 2.5,
    },
    "ESG": {
        "climate change adaptation": 3.0, "climate change mitigation": 3.0,
        "climate adaptation": 2.5, "climate mitigation": 2.5, "climate resilience": 2.5,
        "carbon credit": 3.0, "carbon": 1.5, "mrv": 2.5, "sustainability": 2.0,
        "emissions": 2.0, "climate": 1.5, "esg": 2.5,
        # Direct transport-decarbonisation terms are ESG evidence in mobility calls;
        # generic "mobility" alone remains insufficient to classify a call as ESG.
        "electrification of transport": 2.5, "alternative fuels": 2.0,
        "low-emission transport": 2.5, "zero-emission": 2.5,
        "transport decarbonization": 3.0, "transport decarbonisation": 3.0,
    },
    "ZK-IoV": {
        "zero-knowledge": 3.0, "zkvm": 3.0, "zk proof": 3.0, "zk proofs": 3.0,
        "zkp": 3.0, "zk-snark": 3.0, "zk-stark": 3.0,
        "internet of vehicles": 3.0, "vehicular": 2.0, "connected vehicle": 3.0,
        "vehicle-to-everything": 3.0, "vehicle-to-infrastructure": 3.0,
        "mobility": 1.5, "v2x": 2.5, "iov": 1.0, "zk": 1.0,
        "proving": 1.5, "verifier": 1.5,
    },
    "DID": {
        "decentralized identity": 3.0, "decentralised identity": 3.0,
        "verifiable credential": 3.0, "verifiable credentials": 3.0,
        "identity wallet": 3.0, "self-sovereign identity": 3.0,
        "decentralized identifier": 3.0, "decentralised identifier": 3.0,
        "did method": 2.5, "did document": 2.5,
        "attestation": 1.5, "credential": 1.0,
    },
    "DePIN": {
        "decentralized physical infrastructure": 3.0, "compute network": 2.5,
        "storage network": 2.5, "filecoin network": 3.0, "depin": 3.0,
        "wireless": 1.5, "mapping": 1.5,
        "sensor": 0.8, "iot": 0.8,
    },
    "MEV": {
        "transaction ordering": 3.0, "order flow": 3.0, "block builder": 2.5,
        "liquidation cascade": 2.5, "sequencer": 2.0, "relay": 1.5, "mev": 3.0,
    },
    "Stablecoins": {
        "stablecoin": 3.0, "stablecoins": 3.0, "tokenized deposit": 3.0,
        "payment rail": 2.0, "settlement": 1.5, "usdc": 3.0, "pyusd": 3.0,
        "depeg": 2.5, "reserve": 1.0, "vault": 0.8,
    },
}

def rank_research_areas(text: str) -> list[Dict[str, Any]]:
    """Rank canonical domains from observed text, enforcing compound-domain anchors."""
    lowered = text or ""
    weighted: Dict[str, float] = {}
    matches: Dict[str, list[str]] = {}
    missing_required_anchors: set[str] = set()
    for area in ALLOWED_RESEARCH_AREAS:
        area_weights = DOMAIN_TERM_WEIGHTS.get(area, {})
        score = 0.0
        area_matches: list[str] = []
        for term, weight in area_weights.items():
            hits = _keyword_occurrences(lowered, term)
            if hits:
                score += hits * weight
                area_matches.append(term)
        if not has_required_domain_anchor_groups(area, area_matches):
            # Never let cryptography-only or vehicle-only evidence masquerade as ZK-IoV.
            score = 0.0
            area_matches = []
            missing_required_anchors.add(area)
        weighted[area] = score
        matches[area] = area_matches

    total = sum(weighted.values())
    normalization_floor = max(4.0, total)
    ranked: list[Dict[str, Any]] = []
    for area in ALLOWED_RESEARCH_AREAS:
        score = (weighted[area] / normalization_floor) if normalization_floor else 0.0
        if area in missing_required_anchors:
            evidence = (
                "ZK-IoV requires direct evidence for both zero-knowledge cryptography "
                "and Internet-of-Vehicles/connected-vehicle technology"
            )
        else:
            evidence = (
                f"{len(matches[area])} domain-specific term(s) matched"
                if matches[area] else "No direct domain-specific evidence found"
            )
        ranked.append({
            "domain": area,
            "score": round(score, 4),
            "raw_score": round(weighted[area], 4),
            "matched_terms": matches[area][:10],
            "evidence": evidence,
            "scoring_method": "weighted canonical phrase/term evidence ratio",
        })
    order = {area: idx for idx, area in enumerate(sorted(ALLOWED_RESEARCH_AREAS))}
    ranked.sort(key=lambda item: (-item["score"], order[item["domain"]]))
    for idx, item in enumerate(ranked, 1):
        item["rank"] = idx
    return ranked

def resolve_research_area(
    *,
    explicit_area: Optional[str],
    title: str,
    raw_content: str,
    authoritative_explicit: bool = False,
) -> tuple[Optional[str], list[str], Dict[str, int]]:
    normalized_explicit = normalize_area(explicit_area)
    combined_text = f"{title}\n{raw_content}"
    scores = score_research_areas(combined_text)
    best_area = None
    best_score = 0
    for area, score in scores.items():
        if score > best_score:
            best_area = area
            best_score = score

    warnings: list[str] = []
    if normalized_explicit in ALLOWED_RESEARCH_AREAS:
        explicit_score = scores.get(normalized_explicit, 0)
        if authoritative_explicit:
            if best_area and best_area != normalized_explicit and best_score > explicit_score:
                warnings.append(
                    f"Content also matched '{best_area}', but retained authoritative routed research area '{normalized_explicit}'."
                )
            if explicit_score == 0:
                warnings.append(f"Research area '{normalized_explicit}' has weak textual support in this artifact.")
            return normalized_explicit, warnings, scores
        # Standalone schema callers may opt into content-based normalization
        # when no authoritative pipeline routing decision is available.
        materially_stronger = (
            best_area
            and best_area != normalized_explicit
            and best_score >= 3
            and best_score >= explicit_score + 3
        )
        if materially_stronger:
            warnings.append(
                f"Research area normalized from '{normalized_explicit}' to '{best_area}' based on materially stronger content evidence."
            )
            return best_area, warnings, scores
        if explicit_score == 0:
            warnings.append(f"Research area '{normalized_explicit}' has weak textual support in this artifact.")
        return normalized_explicit, warnings, scores

    if best_area and best_score >= 2:
        return best_area, warnings, scores

    if normalized_explicit:
        warnings.append(f"Research area '{normalized_explicit}' is not in the canonical area list; using Unscoped.")
    else:
        warnings.append("Unable to confidently normalize a research area; using Unscoped.")
    return None, warnings, scores


def build_metadata(
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    started = started_at or utc_now()
    finished = finished_at or utc_now()
    duration = max((finished - started).total_seconds(), 0.0)
    return {
        "started_at": isoformat_utc(started),
        "finished_at": isoformat_utc(finished),
        "duration_seconds": round(duration, 3),
    }


def _build_response(
    *,
    agent: str,
    layer: str,
    status: str,
    mode: str,
    area: Optional[str],
    items_processed: int,
    items_saved: int,
    outputs: Optional[Iterable[Dict[str, Any]]] = None,
    errors: Optional[Iterable[str]] = None,
    warnings: Optional[Iterable[str]] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_status = status if status in ALLOWED_STATUSES else "error"
    return {
        "agent": agent,
        "layer": layer,
        "status": normalized_status,
        "mode": mode,
        "area": normalize_area(area),
        "items_processed": max(int(items_processed), 0),
        "items_saved": max(int(items_saved), 0),
        "outputs": list(outputs or []),
        "errors": [str(error) for error in (errors or [])],
        "warnings": [str(warning) for warning in (warnings or [])],
        "metadata": metadata or build_metadata(started_at=started_at, finished_at=finished_at),
    }


def create_success_response(
    *,
    agent: str,
    layer: str,
    mode: str,
    area: Optional[str],
    items_processed: int = 0,
    items_saved: int = 0,
    outputs: Optional[Iterable[Dict[str, Any]]] = None,
    warnings: Optional[Iterable[str]] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    return _build_response(
        agent=agent,
        layer=layer,
        status="success",
        mode=mode,
        area=area,
        items_processed=items_processed,
        items_saved=items_saved,
        outputs=outputs,
        warnings=warnings,
        started_at=started_at,
        finished_at=finished_at,
    )


def create_partial_response(
    *,
    agent: str,
    layer: str,
    mode: str,
    area: Optional[str],
    items_processed: int = 0,
    items_saved: int = 0,
    outputs: Optional[Iterable[Dict[str, Any]]] = None,
    errors: Optional[Iterable[str]] = None,
    warnings: Optional[Iterable[str]] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    return _build_response(
        agent=agent,
        layer=layer,
        status="partial_success",
        mode=mode,
        area=area,
        items_processed=items_processed,
        items_saved=items_saved,
        outputs=outputs,
        errors=errors,
        warnings=warnings,
        started_at=started_at,
        finished_at=finished_at,
    )


def create_error_response(
    *,
    agent: str,
    layer: str,
    mode: str,
    area: Optional[str],
    errors: Optional[Iterable[str]] = None,
    warnings: Optional[Iterable[str]] = None,
    items_processed: int = 0,
    items_saved: int = 0,
    outputs: Optional[Iterable[Dict[str, Any]]] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    return _build_response(
        agent=agent,
        layer=layer,
        status="error",
        mode=mode,
        area=area,
        items_processed=items_processed,
        items_saved=items_saved,
        outputs=outputs,
        errors=errors,
        warnings=warnings,
        started_at=started_at,
        finished_at=finished_at,
    )


def validate_response_structure(response: Dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response must be a dictionary"]

    if response.get("status") not in ALLOWED_STATUSES:
        errors.append("status must be one of success, partial_success, error")
    if not isinstance(response.get("outputs"), list):
        errors.append("outputs must be a list")
    if not isinstance(response.get("errors"), list):
        errors.append("errors must be a list")
    if not isinstance(response.get("warnings"), list):
        errors.append("warnings must be a list")

    metadata = response.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata must be a dictionary")
    else:
        for key in ("started_at", "finished_at", "duration_seconds"):
            if key not in metadata:
                errors.append(f"metadata must include {key}")

    return errors
