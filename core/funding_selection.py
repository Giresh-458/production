from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.research_schemas import FundingResearchContext

# Match the DB path used by funding_agent
DEFAULT_DB_PATH = Path("outputs/funding/funding_memory.db")

@dataclass
class ApplicantProfile:
    """Constraints for the intended applicant."""
    allowed_geographies: list[str] = field(default_factory=lambda: ["global", "remote", "anywhere"])
    min_trl: int = 1
    max_trl: int = 9
    target_research_areas: list[str] = field(default_factory=list)
    applicant_type: str = "academic" # e.g. academic, startup, non-profit


@dataclass
class ValidationResult:
    is_valid: bool
    reasons: list[str]


@dataclass
class ScoredCall:
    call_id: str
    record: dict[str, Any]
    score: float
    validation: ValidationResult


def _coerce_research_context(value: Any) -> FundingResearchContext | None:
    if isinstance(value, FundingResearchContext):
        return value
    if not isinstance(value, dict):
        return None
    data = dict(value)
    for field_name in (
        "technology_themes", "funding_priorities", "target_outcomes",
        "investigation_questions", "inferred_challenges", "evidence_refs",
        "inference_provenance", "selected_domains",
    ):
        if field_name in data and data[field_name] is not None and not isinstance(data[field_name], tuple):
            data[field_name] = tuple(data[field_name] if isinstance(data[field_name], (list, tuple)) else [str(data[field_name])])
    if "domain_relevance" in data and data["domain_relevance"] is not None and not isinstance(data["domain_relevance"], tuple):
        data["domain_relevance"] = tuple(data["domain_relevance"] if isinstance(data["domain_relevance"], (list, tuple)) else [])
    return FundingResearchContext(**data)


@dataclass
class FundingCallContext:
    """The canonical, structured downstream context for a selected funding call."""
    # Identification
    funding_call_id: str
    funding_body: str | None
    program_name: str | None
    call_id: str | None
    call_title: str | None
    
    # Status & Timeline
    status: str | None
    opening_date: str | None
    deadline: str | None
    
    # Financials & Duration
    funding_amount: str | None
    currency: str | None
    duration: str | None
    eligible_costs: str | None
    
    # Requirements
    eligibility: str | None
    geography: str | None
    trl: str | None
    research_priorities: list[str]
    required_partners: str | None
    deliverables: str | None
    evaluation_criteria: str | None
    
    # Provenance & Metadata
    application_url: str | None
    source_url: str | None
    source_name: str | None
    publication_date: str | None
    last_updated: str | None
    research_area: str | None
    
    # Selection Context
    selection_mode: str
    selection_score: float
    selection_reasons: list[str]

    # Optional for backward-compatible construction of in-memory contexts.
    opportunity_type: str | None = None
    research_context: FundingResearchContext | None = None
    
    @classmethod
    def from_scored_call(cls, scored: ScoredCall, mode: str, extra_reasons: list[str] = None) -> "FundingCallContext":
        rec = scored.record
        
        def _clean_str(val: Any) -> str | None:
            if not val:
                return None
            s = str(val).strip()
            if s.lower() in ["null", "none", "unknown", ""]:
                return None
            return s
            
        def _clean_list(val: Any) -> list[str]:
            if not val:
                return []
            if isinstance(val, list):
                return val
            s = str(val).strip()
            if s.lower() in ["null", "none", "unknown", "[]", ""]:
                return []
            if s.startswith("[") and s.endswith("]"):
                try:
                    import ast
                    parsed = ast.literal_eval(s)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except Exception:
                    pass
            return [s]

        return cls(
            funding_call_id=scored.call_id,
            funding_body=_clean_str(rec.get("funding_body")),
            program_name=_clean_str(rec.get("program_name")),
            call_id=_clean_str(rec.get("call_id")),
            call_title=_clean_str(rec.get("title")),
            status=_clean_str(rec.get("status")),
            opening_date=_clean_str(rec.get("opening_date")),
            deadline=_clean_str(rec.get("deadline")),
            funding_amount=_clean_str(rec.get("funding_amount")),
            currency=_clean_str(rec.get("currency")),
            duration=_clean_str(rec.get("duration")),
            eligible_costs=_clean_str(rec.get("eligible_costs")),
            eligibility=_clean_str(rec.get("eligibility")),
            geography=_clean_str(rec.get("geography")),
            trl=_clean_str(rec.get("trl")),
            research_priorities=_clean_list(rec.get("research_priorities") or rec.get("keywords")),
            required_partners=_clean_str(rec.get("required_partners")),
            deliverables=_clean_str(rec.get("deliverables")),
            evaluation_criteria=_clean_str(rec.get("evaluation_criteria")),
            application_url=_clean_str(rec.get("application_url")),
            source_url=_clean_str(rec.get("source_url") or rec.get("url")),
            source_name=_clean_str(rec.get("source_title") or rec.get("organization")),
            publication_date=_clean_str(rec.get("published_at")),
            last_updated=_clean_str(rec.get("last_verified_at") or rec.get("created_at")),
            research_area=_clean_str(rec.get("focus_area")),
            opportunity_type=_clean_str(rec.get("opportunity_type")),
            selection_mode=mode,
            selection_score=scored.score,
            selection_reasons=scored.validation.reasons + (extra_reasons or []),
            research_context=FundingResearchContext(**rec.get("research_context")) if rec.get("research_context") and isinstance(rec.get("research_context"), dict) else rec.get("research_context")
        )
        
    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary with clean missing values."""
        import dataclasses
        d = {}
        for k, v in self.__dict__.items():
            if v is None:
                d[k] = None
            elif isinstance(v, list) and not v:
                d[k] = []
            elif dataclasses.is_dataclass(v):
                d[k] = dataclasses.asdict(v)
            else:
                d[k] = v
        return d
        
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FundingCallContext":
        """Deserialize from dictionary."""
        kwargs = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        rc = kwargs.get("research_context")
        if rc:
            kwargs["research_context"] = _coerce_research_context(rc)
        return cls(**kwargs)


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            from datetime import UTC
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _extract_trl(trl_str: str | None) -> tuple[int, int] | None:
    """Heuristic extraction of TRL range from a string."""
    if not trl_str:
        return None
    import re
    matches = re.findall(r'\d+', trl_str)
    if not matches:
        return None
    ints = [int(m) for m in matches if 1 <= int(m) <= 9]
    if not ints:
        return None
    return min(ints), max(ints)


def validate_call(record: dict[str, Any], profile: ApplicantProfile) -> ValidationResult:
    """Check hard constraints for a funding call."""
    reasons: list[str] = []
    is_valid = True

    # 1. Closed or expired
    status = str(record.get("status", "")).upper()
    if status == "CLOSED_CALL":
        is_valid = False
        reasons.append("Call is explicitly closed.")
    
    deadline_str = record.get("deadline")
    deadline = _parse_date(deadline_str)
    now = datetime.now(UTC)
    if deadline and deadline < now:
        is_valid = False
        reasons.append(f"Deadline ({deadline_str}) is in the past.")

    # 2. Required info missing
    required_fields = ["program_name", "funding_body", "status"]
    for req in required_fields:
        val = record.get(req)
        if not val or str(val).strip().lower() in ["null", "unknown", "none", ""]:
            is_valid = False
            reasons.append(f"Critically missing required field: {req}")

    # A URL is strongly preferred, but intermediate/in-memory candidates may
    # legitimately omit it (e.g. during ranking tests or pre-publication review).
    # Final selected calls should still be checked for a verifiable source by the
    # caller before submission/crawling.
    has_url = any(
        record.get(url_field)
        and str(record.get(url_field)).strip().lower() not in {"null", "unknown", "none", ""}
        for url_field in ("source_url", "application_url")
    )
    if not has_url:
        reasons.append("Source/application URL not available; candidate is lower-confidence until verified.")

    # 3. Geography incompatible (heuristic)
    geo = str(record.get("geography", "")).lower()
    if geo and geo not in ["null", "unknown", "none"]:
        # If any allowed geography is in the string, or it says global
        match_geo = False
        if "global" in geo or "anywhere" in geo or "remote" in geo:
            match_geo = True
        else:
            for allowed in profile.allowed_geographies:
                if allowed.lower() in geo:
                    match_geo = True
                    break
        if not match_geo:
            is_valid = False
            reasons.append(f"Incompatible geography: {geo}")

    # 4. TRL incompatible
    trl_val = record.get("trl")
    call_trls = _extract_trl(trl_val)
    if call_trls:
        c_min, c_max = call_trls
        if c_max < profile.min_trl or c_min > profile.max_trl:
            is_valid = False
            reasons.append(f"TRL requirement ({trl_val}) incompatible with profile [{profile.min_trl}-{profile.max_trl}]")

    # 5. Eligibility (basic keyword check)
    elig = str(record.get("eligibility", "")).lower()
    if elig and elig not in ["null", "unknown", "none"]:
        if profile.applicant_type == "academic":
            if "startup only" in elig or "commercial only" in elig or "for-profit only" in elig:
                is_valid = False
                reasons.append("Eligibility excludes academic applicants.")
        elif profile.applicant_type == "startup":
            if "academic only" in elig or "university only" in elig or "non-profit only" in elig:
                is_valid = False
                reasons.append("Eligibility excludes startup applicants.")

    # 6. Research domain (area)
    area = str(record.get("focus_area", "")).lower()
    if area and area != "general" and profile.target_research_areas:
        target_lower = [t.lower() for t in profile.target_research_areas]
        if area not in target_lower:
            # We don't strictly fail on "general", but we fail on explicit mismatch
            is_valid = False
            reasons.append(f"Incompatible research domain: {area} (target: {', '.join(target_lower)})")

    if is_valid:
        reasons.append("Passed all hard constraints.")

    return ValidationResult(is_valid=is_valid, reasons=reasons)


def rank_calls(records: list[dict[str, Any]], profile: ApplicantProfile) -> list[ScoredCall]:
    """Score and rank eligible funding calls."""
    scored: list[ScoredCall] = []
    for record in records:
        validation = validate_call(record, profile)
        
        # Base score
        score = 0.5
        
        # Bonus for exact area match
        area = str(record.get("focus_area", "")).lower()
        if profile.target_research_areas and area in [t.lower() for t in profile.target_research_areas]:
            score += 0.2
        elif area != "general":
            score += 0.1
            
        # Bonus for clear deadlines
        if record.get("deadline") and record.get("deadline") not in ["null", "Unknown", "None"]:
            score += 0.1
            
        # Bonus for having funding amounts specified
        if record.get("funding_amount") and record.get("funding_amount") not in ["null", "Unknown", "None"]:
            score += 0.1
            
        # Bonus for clear research priorities
        priorities = record.get("research_priorities")
        if priorities and priorities not in ["null", "[]", "Unknown", "None"]:
            score += 0.05
            
        # Cap at 0.99
        score = min(round(score, 2), 0.99)
        
        # Identify by call_id or fallback to URL hash/title
        call_id = record.get("call_id")
        if not call_id or call_id in ["null", "Unknown", "None"]:
            import hashlib
            digest = hashlib.sha256(str(record.get("title", "")).encode("utf-8")).hexdigest()[:8].upper()
            call_id = f"CALL-{digest}"
            
        scored.append(ScoredCall(call_id=str(call_id), record=record, score=score, validation=validation))
        
    # Sort by validity first (valid ones on top), then score descending
    scored.sort(key=lambda sc: (sc.validation.is_valid, sc.score), reverse=True)
    return scored


def load_candidate_calls(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Check if table exists
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='funding_data'")
        if not cur.fetchone():
            return []
            
        rows = conn.execute("SELECT * FROM funding_data").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def generate_selection_output(
    mode: str, 
    selected: ScoredCall | None, 
    candidates: list[ScoredCall], 
    output_path: Path
) -> dict[str, Any]:
    
    rejected = []
    for cand in candidates:
        if not selected or cand.call_id != selected.call_id:
            reason = "Lower ranking" if cand.validation.is_valid else cand.validation.reasons[0]
            rejected.append({
                "funding_call_id": cand.call_id,
                "reason": reason
            })
            
    payload = {
        "selection_mode": mode,
        "selected_funding_call_id": selected.call_id if selected else None,
        "selection_score": selected.score if selected else None,
        "selection_reasons": selected.validation.reasons if selected else [],
        "rejected_alternatives": rejected,
        "hard_constraints_checked": True
    }
    
    if selected and selected.validation.is_valid:
        extra_reasons = []
        rec = selected.record
        if rec.get("focus_area") != "General":
            extra_reasons.append("Strong research-priority match")
        if rec.get("deadline") not in ["null", "Unknown", "None"]:
            extra_reasons.append("Deadline viable")
            
        context = FundingCallContext.from_scored_call(selected, mode, extra_reasons)
        context_dict = context.to_dict()
        # Merge the full context dict into the payload
        for k, v in context_dict.items():
            payload[k] = v
    
    if mode == "autonomous" and not selected:
        payload["status"] = "NO_SUITABLE_CALL"
        
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def execute_selection(
    mode: str, 
    profile: ApplicantProfile, 
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = Path("outputs/workflow"),
    interactive_choice: str | None = None
) -> dict[str, Any]:
    """
    Execute funding selection.
    
    Modes:
    - "autonomous": Picks highest scoring valid call.
    - "interactive": If interactive_choice is None, writes out candidates for review. 
                     If interactive_choice is a call_id, selects that call.
    """
    records = load_candidate_calls(db_path)
    candidates = rank_calls(records, profile)
    
    output_path = output_dir / "selected_funding_call.json"
    
    if mode == "autonomous":
        valid_cands = [c for c in candidates if c.validation.is_valid]
        selected = valid_cands[0] if valid_cands else None
        return generate_selection_output(mode, selected, candidates, output_path)
        
    elif mode == "review" or mode == "interactive":
        if interactive_choice:
            # We have a choice, finalize it
            selected = next((c for c in candidates if c.call_id == interactive_choice), None)
            if not selected:
                raise ValueError(f"Call ID {interactive_choice} not found among candidates.")
            return generate_selection_output(mode, selected, candidates, output_path)
        else:
            # Present candidates for review by writing them to a review queue file
            review_queue = []
            for c in candidates:
                review_queue.append({
                    "call_id": c.call_id,
                    "program_name": c.record.get("program_name"),
                    "funding_body": c.record.get("funding_body"),
                    "score": c.score,
                    "is_valid": c.validation.is_valid,
                    "reasons": c.validation.reasons
                })
            
            queue_path = output_dir / "funding_review_queue.json"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            queue_path.write_text(json.dumps(review_queue, indent=2), encoding="utf-8")
            
            return {
                "status": "AWAITING_REVIEW",
                "candidates_presented": len(review_queue),
                "queue_path": str(queue_path)
            }
    
    raise ValueError(f"Unknown mode: {mode}")
