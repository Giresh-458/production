from __future__ import annotations

import json
import logging
import re
from typing import Callable, Iterable

LOGGER = logging.getLogger("rif.agents.utils")


class AgentUtilsError(Exception):
    """Raised when a shared agent utility cannot complete its task."""


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


