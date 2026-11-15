from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit
from typing import Callable, Iterable

LOGGER = logging.getLogger("rif.agents.utils")


class AgentUtilsError(Exception):
    """Raised when a shared agent utility cannot complete its task."""


def is_valid_http_url(value: str | None) -> bool:
    """Return True only for absolute HTTP(S) URLs without credentials or whitespace."""
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urlsplit(value.strip())
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.netloc)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def append_collection_diagnostics(
    warnings: list[str] | None,
    source_name: str,
    *,
    pages: list | None = None,
    result: object | None = None,
    review: dict | None = None,
) -> None:
    """Expose per-source crawl failures and coverage gaps to the agent response."""
    if warnings is None:
        return

    failed_urls = getattr(result, "failed_urls", []) if result is not None else []
    if isinstance(failed_urls, list) and failed_urls:
        examples = []
        for item in failed_urls[:3]:
            if isinstance(item, dict):
                examples.append(
                    f"{item.get('url', 'unknown URL')}: {item.get('reason', 'fetch failed')}"
                )
            else:
                examples.append(str(item))
        warnings.append(
            f"Source '{source_name}' had {len(failed_urls)} failed URL(s): "
            + "; ".join(examples)
        )

    skipped_links = review.get("skipped_links", []) if isinstance(review, dict) else []
    if isinstance(skipped_links, list) and skipped_links:
        reasons = []
        for item in skipped_links[:3]:
            if isinstance(item, dict):
                reasons.append(
                    f"{item.get('url', 'unknown URL')}: {item.get('reason', 'skipped')}"
                )
            else:
                reasons.append(str(item))
        warnings.append(
            f"Source '{source_name}' had {len(skipped_links)} skipped link(s): "
            + "; ".join(reasons)
        )

    if pages is not None and not pages and not failed_urls:
        warnings.append(f"Source '{source_name}' produced no pages.")
    
    if result is not None and not getattr(result, "pages", []) and not failed_urls:
        warnings.append(f"Source '{source_name}' produced no pages.")


def normalize_confidence(value: object, default: str = "Medium") -> str:
    """Normalize model confidence to the supported High/Medium/Low vocabulary."""
    text = cleaned_text(str(value or "")).title()
    return text if text in {"High", "Medium", "Low"} else default


def coalesce_text(*values: object) -> str:
    """Return the first non-empty scalar value after whitespace normalization."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        text = cleaned_text(str(value))
        if text and text.lower() not in {"none", "null"}:
            return text
    return ""


def cleaned_text(value: str | None) -> str:
    return " ".join((value or "").split())


def first_matching_sentence(text: str, keywords: Iterable[str]) -> str | None:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    lowered_keywords = [keyword.lower() for keyword in keywords]
    for sentence in sentences:
        lowered = sentence.lower()
        if any(keyword in lowered for keyword in lowered_keywords):
            return cleaned_text(sentence)
    return None


def extract_json_fields(text: str, required_keys: Iterable[str]) -> dict[str, str]:
    keys = tuple(required_keys)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise AgentUtilsError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AgentUtilsError(f"Local LLM returned invalid JSON: {exc}") from exc

    missing = [key for key in keys if key not in parsed]
    if missing:
        raise AgentUtilsError(f"Local LLM response missing keys: {', '.join(missing)}")

    return {key: cleaned_text(str(parsed[key])) for key in keys}


def run_llm_json_prompt(
    prompt: str,
    required_keys: Iterable[str],
    generator: Callable[[str], str],
) -> dict[str, str]:
    try:
        response_text = generator(prompt)
    except Exception as exc:  # pragma: no cover - depends on local LLM runtime
        raise AgentUtilsError(f"Local LLM analysis failed: {exc}") from exc

    return extract_json_fields(response_text, required_keys)


