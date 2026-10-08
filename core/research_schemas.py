from dataclasses import dataclass, field
from typing import List, Optional, Literal, Dict, Any

@dataclass
class FundingResearchContext:
    funding_call_id: str
    domain: str
    technology_themes: tuple[str, ...]
    funding_priorities: tuple[str, ...]
    target_outcomes: tuple[str, ...]
    investigation_questions: tuple[str, ...]
    inferred_challenges: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    inference_provenance: tuple[str, ...]
    confidence: str
    # Explicit classification of whether the selected call actually supports a research workflow.
    # This prevents generic university/event/fellowship calls from being converted into invented technical missions.
    research_intent: str = "unknown"
    research_intent_evidence: tuple[str, ...] = ()
    # Domain routing metadata. These are derived from the funding call and are
    # optional so older persisted contexts remain deserializable.
    selected_domains: tuple[str, ...] = ()
    domain_relevance: tuple[dict, ...] = ()

@dataclass
class EvidenceRef:
    text: str
    source_id: str
    content_hash: str

@dataclass
class SourceRef:
    source_id: str
    document_identity: str
    content_hash: str
    relationship: Literal["SUPPORTS", "CONTRADICTS", "QUALIFIES", "MENTIONS", "UNKNOWN"]
    evidence_spans: List[str]

@dataclass
class ExistingWork:
    title: str
    url: str
    authors: List[str]
    overlap_type: Literal["STRONG_OVERLAP", "WEAK_OVERLAP", "RELATED", "UNKNOWN"]
    limitations: List[str]

@dataclass
class ClaimValidation:
    is_supported: bool
    score: float
    reason: str

@dataclass
class ClaimProvenance:
    claim: str
    provenance: Literal["EVIDENCE", "INFERENCE", "PROPOSED", "ASSUMPTION", "UNKNOWN"]
    source_refs: List[str] = field(default_factory=list)
    derived_from: List[str] = field(default_factory=list)
    generation_method: str = "deterministic"
    supporting_evidence_spans: List[str] = field(default_factory=list)
    validation_status: str = "unsupported"
    support_score: float = 0.0

    def __post_init__(self):
        if self.provenance == "EVIDENCE" and self.validation_status == "supported":
            if not self.supporting_evidence_spans:
                raise ValueError("EVIDENCE claim marked 'supported' must have supporting_evidence_spans.")
            
        # We also enforce that if it's 'supported', it should actually correspond to the text.
        # But for the purpose of structural integrity, checking that the list is non-empty is required.

@dataclass
class ResearchCandidate:
    candidate_id: str
    problem: str
    problem_type: str
    problem_evidence: List[EvidenceRef]

    supporting_sources: List[SourceRef]
    contradicting_sources: List[SourceRef]
    qualifying_sources: List[SourceRef]

    independent_source_count: int
    independent_layers: List[str]

    existing_work: List[ExistingWork]
    remaining_gap: Optional[str]

    novelty_status: Literal["NO_SEARCH", "NO_RESULT", "WEAK_OVERLAP", "STRONG_OVERLAP", "POTENTIAL_GAP", "UNKNOWN"]
    novelty_confidence: str
    search_mode: Literal["local", "external", "hybrid", "unavailable"]

    evidence_confidence: str
    problem_confidence: str
    corroboration_confidence: str
    feasibility_confidence: str

    recommended_experiment: Optional[str]

    provenance: List[ClaimProvenance]

    @classmethod
    def from_legacy(cls, payload: Dict[str, Any]) -> "ResearchCandidate":
        # Extract fields from the legacy ad-hoc dict
        evidence_nodes = payload.get("evidence_assessment", {}).get("sources", [])
        sources = [SourceRef(
            source_id=e.get("source_id", ""),
            document_identity=e.get("document_identity", ""),
            content_hash=e.get("content_hash", ""),
            relationship="SUPPORTS",
            evidence_spans=[e.get("text", "")]
        ) for e in evidence_nodes]

        return cls(
            candidate_id=payload.get("synthesized_file", "unknown-id"),
            problem=payload.get("problem", ""),
            problem_type="identified_limitation",
            problem_evidence=[EvidenceRef(text=s.evidence_spans[0], source_id=s.source_id, content_hash=s.content_hash) for s in sources if s.evidence_spans],
            supporting_sources=sources,
            contradicting_sources=[],
            qualifying_sources=[],
            independent_source_count=payload.get("evidence_assessment", {}).get("independent_source_count", 1),
            independent_layers=payload.get("layers", []),
            existing_work=[],
            remaining_gap=payload.get("research_gap", ""),
            novelty_status=payload.get("novelty_search", {}).get("novelty_status", "UNKNOWN"),
            novelty_confidence=payload.get("novelty_search", {}).get("novelty_confidence", "Low"),
            search_mode=payload.get("novelty_search", {}).get("search_mode", "local"),
            evidence_confidence=payload.get("confidence", "Medium"),
            problem_confidence=payload.get("confidence", "Medium"),
            corroboration_confidence=payload.get("confidence", "Medium"),
            feasibility_confidence=payload.get("confidence", "Medium"),
            recommended_experiment=payload.get("idea", ""),
            provenance=[]
        )
