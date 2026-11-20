"""Single topic-keyword vocabulary for tagging, trends, synthesis and ideas.

Previously each agent kept its own copy; fixes reached some copies and not others
(observed live: trends reported "security: 2" while no record carried a security
tag). Matching is whole-word/phrase, so short keywords cannot match inside other
words ("did" in "candidate", "reserve" in "preserved").
"""
from __future__ import annotations

import re

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "oracle": ("oracle", "data feed", "proof of reserve"),
    "compliance": ("compliance", "reporting", "disclosure", "assurance"),
    # "did" alone is the English verb; only DID-specific phrases count.
    "identity": ("identity", "credential", "decentralized identifier", "decentralised identifier",
                 "did method", "did document", "authentication"),
    "privacy": ("privacy", "zero-knowledge", "zk", "verifier"),
    "settlement": ("settlement", "payment rails", "payments"),
    # Bare "reserve" also names nature reserves and appears in "preserved".
    "stablecoin": ("stablecoin", "depeg", "reserve backing", "reserve-backed", "reserve assets"),
    "liquidity": ("liquidity", "amm", "liquidity pool"),
    "carbon": ("carbon", "mrv", "emissions", "carbon registry"),
    "depin": ("depin", "wireless", "sensor", "storage"),
    "mev": ("mev", "block builder", "transaction ordering", "relay"),
    "prototype": ("prototype", "hackathon", "demo"),
    "benchmark": ("benchmark", "simulator", "dataset", "test suite"),
    # Context-specific: bare "vulnerability"/"audit" matched "wildfire vulnerability"
    # and "auditability" of sustainability reports (observed live).
    "security": ("exploit", "security vulnerability", "software vulnerability", "smart contract vulnerability",
                 "vulnerability disclosure", "security incident", "postmortem", "security audit",
                 "smart contract audit", "code audit"),
    # "proposal" alone matched "the CSRD proposal" (EU legislation; observed live).
    "governance": ("governance", "tokenomics", "incentive design", "incentive mechanism",
                   "governance proposal", "voting mechanism"),
    "interoperability": ("interoperability", "cross-chain", "integration"),
    # "bottleneck" alone matched "lack of analytics is a bottleneck" (observed live).
    "scalability": ("scalability", "throughput", "latency", "high volume", "transaction volume",
                    "throughput bottleneck", "performance bottleneck", "scaling bottleneck",
                    "verification bottleneck", "does not scale", "scales poorly"),
}

# The ESG acronym's expansion names a reporting category, not governance
# mechanisms (observed live: 4 of 8 #topic-governance tags came from it).
_ESG_EXPANSION = re.compile(
    r"\b(?:environmental|social)\s*,?\s*(?:and\s+)?(?:social|environmental)\s*,?\s*(?:and\s+)?"
    r"(?:corporate\s+)?governance\b",
    re.IGNORECASE,
)


def prepare_topic_text(text: str) -> str:
    """Lower-case, unify separators, and drop the ESG acronym expansion."""
    value = _ESG_EXPANSION.sub(" esg ", str(text or ""))
    value = re.sub(r"[-_/]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def topic_keyword_hits(prepared_text: str, keywords: tuple[str, ...] | list[str]) -> list[str]:
    hits = []
    for keyword in keywords:
        normalized = re.sub(r"[-_/]+", " ", str(keyword).lower()).strip()
        if normalized and re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", prepared_text):
            hits.append(keyword)
    return hits


def match_topics(text: str) -> list[str]:
    prepared = prepare_topic_text(text)
    return [topic for topic, keywords in TOPIC_KEYWORDS.items() if topic_keyword_hits(prepared, keywords)]
