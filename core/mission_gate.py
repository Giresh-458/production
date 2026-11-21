from __future__ import annotations

import re
from typing import Any

from core.funding_selection import FundingCallContext
from core.schemas import DOMAIN_TERM_WEIGHTS, has_required_domain_anchor_groups, normalize_area

# Batch-2 mission verification uses a deliberately narrow ESG vocabulary.
# Keep this local to the downstream gate so Batch-1 routing/schema behavior remains
# unchanged while normalized evidence can still validate source-grounded ESG calls.
BATCH2_ESG_DOMAIN_TERMS = {
    "sustainability reporting",
    "sustainability disclosure",
    "sustainability assurance",
    "greenhouse gas emissions",
    "emissions trading",
    "emissions reporting",
    "carbon markets",
    "carbon market",
    "carbon credits",
    "carbon credit",
    "carbon accounting",
    "mrv",
    "climate change adaptation",
    "climate change mitigation",
    "climate resilience",
}
BATCH2_ESG_ANCHOR_GROUPS = (
    frozenset({"sustainability reporting", "sustainability disclosure", "sustainability assurance"}),
    frozenset({
        "greenhouse gas emissions", "emissions trading", "emissions reporting",
        "carbon markets", "carbon market", "carbon credits", "carbon credit",
        "carbon accounting", "mrv",
    }),
    frozenset({"climate change adaptation", "climate change mitigation", "climate resilience"}),
)


STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "their",
    "there", "which", "using", "used", "use", "are", "was", "were", "have",
    "has", "had", "will", "can", "may", "more", "than", "also", "such",
    "these", "those", "about", "research", "funding", "opportunity",
    "program", "call", "application", "applicant", "applicants", "project",
    "projects", "barrier", "barriers", "deployment", "validation",
    "evaluation", "adoption", "existing", "solutions", "address", "problems",
    "gaps", "gap", "technical", "transition", "commercialization", "tcup", "nsf",
    "investigate", "affecting", "unresolved", "limitations", "improving",
    # Generic funding priorities must not count as substantive technical overlap.
    "market", "markets", "adoption",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
        if token not in STOP
    }


def _specific_area(value: Any) -> str:
    rendered = str(value or "").strip()
    return "" if rendered.lower() in {"", "general", "unscoped", "unknown", "none", "null"} else rendered


def _supported_domain_evidence(research_context: Any) -> dict[str, set[str]] | None:
    """Return canonical domains backed by the same strong evidence as Batch-1 routing.

    Older persisted contexts may not contain domain_relevance. For those
    contexts, return None and preserve their legacy selected-domain behavior.
    When evidence is present, weak secondary domains must not become mission
    anchors merely because they appear in selected_domains.
    """
    raw_ranking = getattr(research_context, "domain_relevance", None)
    if not isinstance(raw_ranking, (list, tuple)) or not raw_ranking:
        return None
    if not any(
        isinstance(item, dict) and ("raw_score" in item or "matched_terms" in item)
        for item in raw_ranking
    ):
        return None

    supported: dict[str, set[str]] = {}
    for item in raw_ranking:
        if not isinstance(item, dict):
            continue
        domain = normalize_area(str(item.get("domain") or item.get("area") or ""))
        weights = DOMAIN_TERM_WEIGHTS.get(str(domain or ""))
        if not weights:
            continue
        try:
            raw_score = float(item.get("raw_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if raw_score < 2.5:
            continue
        matched_terms = item.get("matched_terms") or []
        if not isinstance(matched_terms, (list, tuple, set)):
            continue
        strong_terms = {
            term.casefold()
            for term, weight in weights.items()
            if float(weight) >= 2.0
        }
        if domain == "ESG":
            strong_terms.update(BATCH2_ESG_DOMAIN_TERMS)
        supported_terms = {
            str(term).casefold().strip()
            for term in matched_terms
            if str(term).casefold().strip() in strong_terms
        }
        if domain == "ESG":
            context_domain = normalize_area(str(getattr(research_context, "domain", "") or ""))
            # A single emissions/sustainability outcome is not enough to turn
            # an otherwise energy/digital call into an ESG research mission.
            if context_domain != "ESG" and len(supported_terms) < 2:
                continue
        if domain == "ESG":
            context_domain = normalize_area(str(getattr(research_context, "domain", "") or ""))
            if context_domain == "ESG":
                # ESG is the selected call's primary domain: the normal
                # required ESG anchor group is sufficient for a focused call.
                domain_anchors_supported = has_required_domain_anchor_groups(domain, supported_terms)
            else:
                # ESG is secondary to another mission. Require independent
                # evidence from two ESG concept groups so an expected outcome
                # cannot promote the source into a second research mission.
                matched_esg_groups = sum(
                    1 for group in BATCH2_ESG_ANCHOR_GROUPS
                    if group.intersection(supported_terms)
                )
                domain_anchors_supported = matched_esg_groups >= 2
        else:
            domain_anchors_supported = has_required_domain_anchor_groups(domain, supported_terms)
        if supported_terms and domain_anchors_supported:
            supported[str(domain)] = supported_terms
    return supported


def _mission_anchor_values(ctx: FundingCallContext) -> list[str]:
    """Build relevance anchors only from comparatively specific mission fields.

    Generated investigation questions and inferred challenges are deliberately
    excluded: boilerplate such as "barriers to deployment and adoption" can
    otherwise make unrelated pages look relevant to almost any grant. Canonical
    domain labels are included only when the persisted domain-ranking evidence
    meets the same strong-anchor threshold used by the pipeline's domain router.
    """
    rc = ctx.research_context
    values: list[str] = []
    supported_domain_evidence = _supported_domain_evidence(rc) if rc else None
    supported_domains = (
        set(supported_domain_evidence)
        if supported_domain_evidence is not None
        else None
    )

    def add_values(raw_values: Any) -> None:
        if not isinstance(raw_values, (list, tuple, set)):
            return
        for raw_value in raw_values:
            value = str(raw_value or "").strip()
            if not value:
                continue
            canonical = normalize_area(value)
            if (
                supported_domains is not None
                and canonical in DOMAIN_TERM_WEIGHTS
                and canonical not in supported_domains
            ):
                continue
            values.append(value)

    def add_domain(raw_value: Any) -> None:
        value = _specific_area(raw_value)
        if not value:
            return
        canonical = normalize_area(value)
        if (
            supported_domains is not None
            and canonical in DOMAIN_TERM_WEIGHTS
            and canonical not in supported_domains
        ):
            return
        values.append(value)

    if rc:
        add_values(rc.technology_themes)
        add_values(rc.funding_priorities)
        add_values(rc.target_outcomes)
        add_values(rc.selected_domains)
        add_domain(getattr(rc, "domain", None))
        if supported_domain_evidence is not None:
            for matched_terms in supported_domain_evidence.values():
                add_values(matched_terms)

    add_domain(ctx.research_area)
    add_values(ctx.research_priorities)
    return list(dict.fromkeys(values))


def _mission_anchor_text(ctx: FundingCallContext) -> str:
    return " ".join(_mission_anchor_values(ctx))


TECHNICAL_TITLE_ANCHORS = {
    "ai", "artificial", "intelligence", "digital", "platform", "platforms",
    "energy", "grid", "power", "carbon", "emissions", "mrv", "zero",
    "knowledge", "cryptography", "stablecoin", "stablecoins", "tokenization",
    "identity", "vehicle", "vehicles", "iot", "robotics", "quantum", "privacy",
    "verification", "settlement", "storage", "compute", "wireless",
    "decentralized", "infrastructure", "payments", "distributed",
}


def _call_title_anchor_terms(ctx: FundingCallContext) -> list[str]:
    """Extract only technical terms explicitly present in the selected call title."""
    title = str(getattr(ctx, "call_title", "") or getattr(ctx, "program_name", "") or "")
    tokens = {
        part
        for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", title.casefold())
        for part in token.split("-")
    }
    return sorted(tokens & TECHNICAL_TITLE_ANCHORS)


def _normalized_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


ENERGY_SYSTEM_EVIDENCE_PATTERNS = (
    r"\benergy[\s-]+systems?\b",
    r"\belectricity[\s-]+systems?\b",
    r"\b(?:power|transmission|distribution)\s+systems?\b",
    r"\belectricity\s+markets?\b",
    r"\belectricity[\s-]+(?:supply|generation|consumption|load)\b",
    r"\bpower[\s-]+generation\b",
    r"\bgrid[\s-]+(?:operation|operations|flexibility|management)\b",
    r"\b(?:smart|distribution|power|electricity)[\s-]+grids?\b",
    r"\brenewable[\s-]+energy\s+(?:integration|generation)\b",
    r"\benergy\s+storage\b",
    r"\benergy\s+platforms?\b",
    r"\bgrid\s+integration\b",
    r"\bflexibility\s+management\b",
)


def _energy_system_evidence(record: dict[str, Any]) -> str:
    """Use quoted source passages only; LLM summaries and metadata are not proof."""
    values: list[str] = []
    for key in ("evidence_snippets", "excerpt_windows"):
        raw = record.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(str(item) for item in raw if str(item).strip())
    return "\n".join(values)


def _is_energy_system_call(ctx: FundingCallContext) -> bool:
    title = str(getattr(ctx, "call_title", "") or getattr(ctx, "program_name", "") or "").casefold()
    return "energy" in title and ("platform" in title or "digital" in title)


def _has_energy_system_evidence(record: dict[str, Any]) -> bool:
    body = _energy_system_evidence(record)
    return any(re.search(pattern, body, flags=re.IGNORECASE) for pattern in ENERGY_SYSTEM_EVIDENCE_PATTERNS)

def _is_data_availability_metadata_echo(record: dict[str, Any]) -> bool:
    """Reject dataset artifacts whose only 'evidence' is the YAML name/focus hint."""
    if str(record.get("agent_name") or "").casefold() != "data_availability":
        return False
    metadata = record.get("machine_metadata")
    if not isinstance(metadata, dict):
        return False
    hints = metadata.get("source_hints")
    if not isinstance(hints, dict):
        return False
    expected = _normalized_text(f"{hints.get('source_name', '')} {hints.get('focus', '')}")
    if not expected:
        return False
    expected_tokens = set(expected.split())
    observations: list[str] = []
    for key in ("context_summary", "problem_statement"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            observations.append(value)
    for key in ("evidence_snippets", "excerpt_windows"):
        values = record.get(key)
        if isinstance(values, (list, tuple)):
            observations.extend(str(value) for value in values if str(value).strip())
    for observation in observations:
        normalized = _normalized_text(observation)
        if normalized == expected:
            return True
        observed_tokens = set(normalized.split())
        if len(observed_tokens) >= 3 and observed_tokens.issubset(expected_tokens):
            return True
    return False

_PHRASE_STOP = STOP | {
    "need", "needs", "highlights", "communication", "integrated", "integrating",
    "addressing", "towards", "toward", "across", "between", "within", "through",
}


def _call_specific_phrases(ctx: FundingCallContext) -> set[str]:
    """Two-word phrases from the call title and its source-grounded problem text."""
    rc = ctx.research_context
    parts = [str(getattr(ctx, "call_title", "") or getattr(ctx, "program_name", "") or "")]
    if rc is not None and getattr(rc, "evidence_refs", None):
        parts.extend(str(item) for item in (getattr(rc, "inferred_challenges", None) or ()))
    words = re.findall(r"[a-z][a-z-]+", " ".join(parts).casefold())
    return {
        f"{left} {right}".replace("-", " ")
        for left, right in zip(words, words[1:])
        if len(left) > 3 and len(right) > 3 and left not in _PHRASE_STOP and right not in _PHRASE_STOP
    }


def _domain_anchor_terms(ctx: FundingCallContext) -> set[str]:
    """Domain-specific anchor terms for the call's supported domains.

    Uses a domain's required anchor group where one is defined (ESG, ZK-IoV) and
    otherwise its strong terms (weight >= 2.0). Generic words that are strong for
    ESG ("sustainability") are therefore excluded, as are brand terms that only
    count with corroboration (e.g. "new european bauhaus").
    """
    from core.schemas import DOMAIN_CORROBORATION_REQUIRED_ANCHORS, DOMAIN_REQUIRED_ANCHOR_GROUPS

    rc = ctx.research_context
    evidence = _supported_domain_evidence(rc) if rc else None
    declared = [ctx.research_area]
    if rc is not None:
        declared += [getattr(rc, "domain", None), *(getattr(rc, "selected_domains", None) or ())]
    domains = set(evidence or ()) or {
        normalize_area(value) for value in declared if normalize_area(value) in DOMAIN_TERM_WEIGHTS
    }
    terms: set[str] = set()
    for domain in domains:
        groups = DOMAIN_REQUIRED_ANCHOR_GROUPS.get(domain)
        if groups:
            domain_terms = {term for group in groups for term in group}
        else:
            domain_terms = {term for term, weight in DOMAIN_TERM_WEIGHTS.get(domain, {}).items() if weight >= 2.0}
        terms |= {term.casefold() for term in domain_terms} - set(DOMAIN_CORROBORATION_REQUIRED_ANCHORS.get(domain, ()))
    return {term.replace("-", " ") for term in terms}


# Calibrated on one live call (HORIZON-NATURE-2027-02-02, 53 candidates): on-topic
# papers scored 0.450-0.552, off-topic living-lab papers <= 0.436, crypto/finance
# pages <= 0.23 (all-MiniLM-L6-v2). Recalibrate as more calls are observed.
SEMANTIC_CALL_MATCH_THRESHOLD = 0.45
# Floor for lexical passes (domain anchor term or generic call phrase).
# Calibration (all-MiniLM-L6-v2), 4 live calls on 2026-11-21:
#   HORIZON-NATURE-2027-02-02 on-topic 0.456-0.621, off-topic ESG 0.237-0.329;
#   HORIZON-CL5-2027-01-D1-09/-10/-12 on-topic 0.451-0.780, off-topic 0.200.
# Recalibrate as more calls are observed.
DOMAIN_ANCHOR_SEMANTIC_FLOOR = 0.40
_CALL_QUERY_CACHE: dict[str, str] = {}


def _semantic_gate_enabled() -> bool:
    """On by default for every entry point; set RIF_SEMANTIC_GATE=false to disable.

    The relevance decision must not depend on which script runs the gate
    (observed live: Batch-2 accepted 3 semantic matches, then Batch-3 re-applied
    the gate without the flag, rejected them and rewrote the handoff 8 -> 5).
    Without the embedding model the semantic pass returns None everywhere, so
    the gate stays lexical-only consistently on that machine.
    """
    import os

    return os.environ.get("RIF_SEMANTIC_GATE", "true").strip().casefold() in {"1", "true", "yes", "on"}


def _call_semantic_similarity(record: dict[str, Any], ctx: FundingCallContext) -> float | None:
    from core.semantic_retrieval import transformer_similarities

    rc = ctx.research_context
    query = str(getattr(ctx, "call_title", "") or getattr(ctx, "program_name", "") or "")
    if rc is not None and getattr(rc, "evidence_refs", None):
        query += ". " + " ".join(str(item) for item in (getattr(rc, "inferred_challenges", None) or ()))
    if not query.strip():
        return None
    document = ". ".join(
        [str(record.get("title") or "")]
        + [str(snippet) for snippet in (record.get("evidence_snippets") or [])[:3]]
    )[:2000]
    if not document.strip():
        return None
    scores = transformer_similarities(query, [document])
    return scores[0] if scores else None


def relevance(record: dict[str, Any], ctx: FundingCallContext) -> dict[str, Any]:
    """Conservative post-retrieval relevance gate.

    Only retrieved document content is considered evidence. Search queries,
    keywords, source/evidence types, actor IDs, and other metadata cannot make a
    document relevant because those fields may simply repeat the retrieval
    instructions rather than describe what the source actually says.
    """
    metadata = record.get("machine_metadata")
    quality_gate = metadata.get("quality_gate") if isinstance(metadata, dict) else None
    if isinstance(quality_gate, dict) and quality_gate.get("passed") is False:
        return {
            "passed": False, "score": 0.0, "matched_terms": [],
            "matched_phrases": [], "matched_title_anchors": [],
            "reason": "collector quality gate failed; artifact retained for review only",
        }
    if _is_data_availability_metadata_echo(record):
        return {
            "passed": False, "score": 0.0, "matched_terms": [],
            "matched_phrases": [], "matched_title_anchors": [],
            "reason": "data-availability evidence contains only configured source metadata, not extracted page content",
        }

    anchor_values = _mission_anchor_values(ctx)
    anchor_text = " ".join(anchor_values)
    document_text = " ".join(
        [
            str(record.get(key, ""))
            for key in ("title", "problem_statement", "context_summary")
            if record.get(key)
        ]
        # Retrieved evidence snippets are source text too. Normalization now keeps
        # only a genuine problem sentence (or a placeholder) in problem_statement,
        # so the gate must still see the snippets it previously saw via that field.
        + [str(snippet) for snippet in (record.get("evidence_snippets") or []) if str(snippet).strip()]
    )

    mission_tokens = _tokens(anchor_text)
    document_tokens = _tokens(document_text)
    title_anchors = _call_title_anchor_terms(ctx)
    energy_call = _is_energy_system_call(ctx)
    energy_evidence = _has_energy_system_evidence(record) if energy_call else False
    agent_name = str(record.get("agent_name") or "").casefold()
    # Every layer must connect to the selected energy-system mission. A
    # collection page that merely contains scattered "AI", "digital" and
    # "energy" navigation labels is not evidence of that connection.
    if energy_call and not energy_evidence:
        return {
            "passed": False, "score": 0.0, "matched_terms": [],
            "matched_phrases": [], "matched_title_anchors": [],
            "reason": "source lacks source-derived energy-system/grid evidence for the selected AI energy-platform call",
        }
    matched_title_set = set(title_anchors) & document_tokens
    document_phrases = _normalized_text(document_text)
    # Normalize simple plurals for title anchors without fuzzy keyword matching.
    for anchor in title_anchors:
        if anchor.endswith("s") and anchor[:-1] in document_tokens:
            matched_title_set.add(anchor)
    # AI may be tokenized as a two-letter acronym; retain it only as an exact
    # standalone token or the explicit phrase "artificial intelligence".
    if "ai" in title_anchors and re.search(r"\bai\b|\bartificial intelligence\b", document_phrases):
        matched_title_set.add("ai")
    # Zero-knowledge proofs are a specific privacy-preserving technique, not
    # a generic keyword coincidence. Treat the phrase as a controlled synonym.
    if "privacy" in title_anchors and re.search(r"\bzero knowledge\b|\bzkp\b", document_phrases):
        matched_title_set.add("privacy")
    matched_title_anchors = sorted(matched_title_set)
    required_title_matches = min(3, len(title_anchors))
    title_anchor_match = bool(required_title_matches) and len(matched_title_anchors) >= required_title_matches
    if energy_call and energy_evidence:
        # A concrete grid/electricity-system phrase is a stronger anchor than
        # requiring every source layer to repeat the entire call title.
        title_anchor_match = True
    if required_title_matches and not title_anchor_match:
        return {
            "passed": False, "score": 0.0,
            "matched_terms": sorted(mission_tokens & document_tokens)[:20],
            "matched_phrases": [], "matched_title_anchors": matched_title_anchors,
            "reason": (
                "source does not match enough technical anchors from the selected funding-call title "
                f"({len(matched_title_anchors)}/{required_title_matches})"
            ),
        }
    if not document_tokens or (not mission_tokens and not title_anchor_match):
        return {
            "passed": False, "score": 0.0, "matched_terms": [],
            "matched_phrases": [], "matched_title_anchors": matched_title_anchors,
            "reason": "empty substantive mission/document vocabulary",
        }

    matches = sorted(mission_tokens & document_tokens)
    raw_document = document_text.lower()
    phrases: list[str] = []
    for anchor_value in anchor_values:
        for phrase in re.findall(r"[a-z0-9][a-z0-9 -]{5,}", anchor_value.lower()):
            phrase = " ".join(phrase.split())
            phrase_tokens = _tokens(phrase)
            if (
                len(phrase.split()) >= 2
                and phrase_tokens
                and phrase in raw_document
                and phrase not in STOP
            ):
                phrases.append(phrase)

    # One exact technical phrase or at least two non-boilerplate terms is
    # required. Funding-call identity and generated retrieval queries alone do
    # not satisfy this check.
    passed = bool(phrases) or len(matches) >= 2 or title_anchor_match
    overlap_score = min(1.0, len(matches) / max(3, min(len(mission_tokens), 12))) if mission_tokens else 0.0
    title_score = len(matched_title_anchors) / len(title_anchors) if title_anchors else 0.0
    score = max(overlap_score, title_score)

    # Generic vocabulary alone ("climate", "sustainability") is not evidence of
    # relevance (observed live: a lab's blog listing passed a living-labs call on
    # those two words). Require a call-specific phrase or a domain anchor term,
    # unless a technical title anchor or energy-system evidence already applies.
    normalized_document = " ".join(re.findall(r"[a-z0-9]+", raw_document.replace("-", " ")))
    call_phrase_hits = sorted(
        phrase for phrase in _call_specific_phrases(ctx)
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalized_document)
    )
    domain_anchor_hits = sorted(
        term for term in _domain_anchor_terms(ctx)
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized_document)
    )
    if call_phrase_hits or phrases:
        # Call title/problem phrases, or exact multi-word mission phrases from
        # the call's technology themes and priorities ("compute network").
        tier = "call_specific"
    elif domain_anchor_hits:
        tier = "domain_anchor"
    elif title_anchor_match or (energy_call and energy_evidence):
        tier = "title_anchor"
    else:
        tier = None
    reason = "substantive mission overlap" if passed else "insufficient substantive mission overlap"
    semantic_similarity = None
    if not passed and _semantic_gate_enabled():
        # Lexical anchors are domain-level and miss on-topic evidence that uses
        # different wording (observed live: "Citarum Living Lab ...", "Ecosystem
        # Restoration ..." rejected for a living-labs restoration call). Rescue it
        # only with real sentence embeddings, never with TF-IDF/hash proxies.
        semantic_similarity = _call_semantic_similarity(record, ctx)
        if semantic_similarity is not None and semantic_similarity >= SEMANTIC_CALL_MATCH_THRESHOLD:
            passed = True
            tier = "semantic_call_match"
            reason = f"semantic match to the call title/problem (cosine {semantic_similarity:.3f})"
    if passed and tier is None:
        passed = False
        reason = "only generic vocabulary overlap; no call-specific phrase or domain anchor term"
    if passed and tier in {"domain_anchor", "call_specific"} and _semantic_gate_enabled():
        # A domain word ("carbon", "esg") alone admits evidence about a different
        # topic in the same domain (observed live for a restoration living-labs
        # call: ESG-rating, greenwashing and carbon-pricing papers, cosine
        # 0.237-0.329, while every on-topic record scored >= 0.456). Require the
        # evidence to be semantically related to the call as well.
        semantic_similarity = _call_semantic_similarity(record, ctx)
        if semantic_similarity is not None and semantic_similarity < DOMAIN_ANCHOR_SEMANTIC_FLOOR:
            passed = False
            # Generic call phrases ("climate change") also admit off-topic evidence
            # (observed live on HORIZON-CL5-2027-01-D1-09: a living-labs paper at
            # cosine 0.200 passed as call_specific; on-topic records were >= 0.451).
            reason = (
                f"{'call phrase' if tier == 'call_specific' else 'domain term'} only; semantically unrelated "
                f"to the call (cosine {semantic_similarity:.3f} < {DOMAIN_ANCHOR_SEMANTIC_FLOOR})"
            )
    return {
        "passed": passed,
        "score": round(score, 3),
        "matched_terms": matches[:20],
        "matched_phrases": phrases[:5],
        "matched_title_anchors": matched_title_anchors,
        "relevance_tier": tier if passed else None,
        "semantic_similarity": None if semantic_similarity is None else round(semantic_similarity, 3),
        "call_specific_phrases": call_phrase_hits[:5],
        "domain_anchor_terms": domain_anchor_hits[:5],
        "reason": reason,
    }
