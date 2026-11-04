from __future__ import annotations

import re
from typing import Any

from core.schemas import AREA_KEYWORDS

PROBLEM_SIGNAL_TERMS = (
    "problem",
    "challenge",
    "constraint",
    "requirement",
    "gap",
    "prototype",
    "deployment",
    "integration",
    "privacy",
    "security",
    "compliance",
    "scalability",
    "interoperability",
    "reliability",
    "settlement",
    "identity",
    "oracle",
    "governance",
    "verification",
)


UNSCOPED_EVIDENCE_TERMS = (
    "dataset", "data set", "api", "endpoint", "json-stat", "csv", "json",
    "parquet", "download", "time series", "data portal", "data catalog",
    "data catalogue", "electricity supply", "electricity generation",
    "electricity consumption", "electricity system", "energy system",
    "power system", "electricity market", "transmission system",
    "cross-border electricity", "grid operation", "grid flexibility", "smart grid",
    "renewable energy", "energy storage", "generation", "consumption",
    "load", "balancing", "market prices", "measurements", "telemetry", "benchmark",
    "registry", "dashboard", "explorer", "metadata", "data schema",
)


def _clean_line(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _paragraphs(raw_content: str) -> list[str]:
    if not raw_content.strip():
        return []
    blocks = re.split(r"\n\s*\n", raw_content)
    if len(blocks) == 1:
        blocks = re.split(r"(?<=[.!?])\s+", raw_content)
    paragraphs = [_clean_line(block) for block in blocks]
    return [paragraph for paragraph in paragraphs if paragraph]


def _section_headings(raw_content: str) -> list[str]:
    headings: list[str] = []
    for line in raw_content.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 80:
            continue
        if stripped.startswith(("-", "*", "•")):
            continue
        if stripped.endswith(":"):
            headings.append(stripped.rstrip(":"))
            continue
        if 1 < len(stripped.split()) <= 8 and stripped == stripped.title():
            headings.append(stripped)
    return headings[:8]


def _bullet_lists(raw_content: str) -> list[str]:
    bullets: list[str] = []
    for line in raw_content.splitlines():
        stripped = line.strip()
        if re.match(r"^([-*•]|\d+\.)\s+", stripped):
            bullets.append(re.sub(r"^([-*•]|\d+\.)\s+", "", stripped))
    return bullets[:12]


def _quoted_passages(raw_content: str) -> list[str]:
    passages: list[str] = []
    for match in re.finditer(r"\"([^\"]{20,280})\"", raw_content):
        passages.append(_clean_line(match.group(1)))
    for match in re.finditer(r"'([^']{20,280})'", raw_content):
        passages.append(_clean_line(match.group(1)))
    unique: list[str] = []
    seen: set[str] = set()
    for item in passages:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:8]


def _links_found(raw_content: str, metadata: dict[str, Any]) -> list[str]:
    urls = re.findall(r"https?://[^\s)\]>\"']+", raw_content)
    for value in metadata.values():
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    urls.append(item)
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique[:20]


def _named_entities_or_programs(raw_content: str, metadata: dict[str, Any]) -> list[str]:
    candidates = re.findall(r"\b(?:[A-Z][a-zA-Z0-9&/-]+(?:\s+[A-Z][a-zA-Z0-9&/-]+){0,3}|[A-Z]{2,}(?:-[A-Z]{2,})?)\b", raw_content)
    for key in ("title", "name", "organization", "company_name", "investor_or_organization", "issuing_body", "sponsor_or_organizer"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    unique: list[str] = []
    seen: set[str] = set()
    generic_sentence_starters = {
        "such", "while", "meanwhile", "however", "therefore", "moreover",
        "although", "already", "these", "those", "this", "that",
        "deliberate", "deep", "regulating", "employing",
    }
    source_title_values = set()
    if isinstance(metadata, dict):
        for value in (metadata.get("title"),):
            if isinstance(value, str) and value.strip():
                source_title_values.add(value.strip().casefold())
        hints = metadata.get("source_hints")
        if isinstance(hints, dict):
            value = hints.get("title")
            if isinstance(value, str) and value.strip():
                source_title_values.add(value.strip().casefold())
    for candidate in candidates:
        cleaned = _clean_line(candidate)
        first_word = cleaned.split(" ", 1)[0].casefold() if cleaned else ""
        if (
            len(cleaned) < 3
            or cleaned.lower() in {"manual", "source", "title"}
            or first_word in generic_sentence_starters
            or cleaned.casefold() in source_title_values
        ):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique[:15]


def _keyword_score(text: str, research_area: str | None) -> int:
    lowered = text.lower()
    score = 0
    for term in PROBLEM_SIGNAL_TERMS:
        score += lowered.count(term)
    if research_area and research_area in AREA_KEYWORDS:
        for keyword in AREA_KEYWORDS[research_area]:
            score += lowered.count(keyword.lower()) * 2
    elif not research_area or str(research_area).casefold() in {"general", "unscoped"}:
        # A mission-specific General/Unscoped collector still needs useful
        # source passages. Prefer concrete data/API/energy-system evidence
        # instead of returning arbitrary first paragraphs.
        for term in UNSCOPED_EVIDENCE_TERMS:
            score += lowered.count(term)
    return score


def _top_scored(paragraphs: list[str], research_area: str | None, limit: int) -> list[str]:
    scored = [(paragraph, _keyword_score(paragraph, research_area), len(paragraph)) for paragraph in paragraphs if paragraph]
    scored.sort(key=lambda item: (item[1], item[2]), reverse=True)
    results: list[str] = []
    seen: set[str] = set()
    for paragraph, score, _length in scored:
        if paragraph in seen:
            continue
        if score <= 0 and results:
            continue
        seen.add(paragraph)
        results.append(paragraph)
        if len(results) >= limit:
            break
    return results


def _excerpt_windows(paragraphs: list[str], research_area: str | None) -> list[str]:
    windows: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if _keyword_score(paragraph, research_area) <= 0:
            continue
        window = [paragraph]
        if index > 0:
            window.insert(0, paragraphs[index - 1])
        if index + 1 < len(paragraphs):
            window.append(paragraphs[index + 1])
        snippet = " ".join(window)
        snippet = snippet[:600].strip()
        if snippet and snippet not in windows:
            windows.append(snippet)
        if len(windows) >= 4:
            break
    return windows


def build_evidence_bundle(
    *,
    raw_content: str,
    title: str,
    research_area: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    paragraphs = _paragraphs(raw_content)
    key_paragraphs = _top_scored(paragraphs, research_area, 5)
    evidence_snippets = _top_scored(paragraphs, research_area, 6)
    headings = _section_headings(raw_content)
    bullets = _bullet_lists(raw_content)
    quoted = _quoted_passages(raw_content)
    links = _links_found(raw_content, metadata)
    entities = _named_entities_or_programs(raw_content, metadata)

    return {
        "page_title": title,
        "section_headings": headings,
        "key_paragraphs": key_paragraphs,
        "bullet_lists": bullets,
        "quoted_passages": quoted,
        "links_found": links,
        "named_entities_or_programs": entities,
        "evidence_snippets": evidence_snippets,
        "excerpt_windows": _excerpt_windows(paragraphs, research_area),
    }
