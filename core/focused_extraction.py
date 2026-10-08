from __future__ import annotations

from typing import Iterable

from agents.utils import cleaned_text


def extract_text_blocks(soup: object) -> list[str]:
    blocks: list[str] = []
    find_all = getattr(soup, "find_all", None)
    if not callable(find_all):
        return blocks
    for node in find_all(["h1", "h2", "h3", "p", "li"]):
        text = cleaned_text(node.get_text(" ", strip=True))
        if text:
            blocks.append(text)
    return blocks


def build_focused_content(
    *,
    title: str,
    blocks: list[str],
    focus_keywords: Iterable[str],
    area_keywords: Iterable[str] = (),
    min_score: int = 2,
    neighbor_window: int = 1,
    max_blocks: int = 18,
    max_chars: int = 7000,
) -> str:
    focus_list = [keyword.lower() for keyword in focus_keywords if keyword]
    area_list = [keyword.lower() for keyword in area_keywords if keyword]
    selected: list[str] = []
    seen: set[str] = set()

    def block_score(value: str) -> int:
        lowered = value.lower()
        focus_score = sum(lowered.count(keyword) for keyword in focus_list)
        area_score = sum(lowered.count(keyword) for keyword in area_list)
        structural_bonus = 1 if ":" in value or any(token in lowered for token in ("must", "should", "required", "requirements")) else 0
        return focus_score * 2 + area_score + structural_bonus

    for index, block in enumerate(blocks):
        if len(block) < 20:
            continue
        if block_score(block) < min_score:
            continue
        start = max(0, index - neighbor_window)
        end = min(len(blocks), index + neighbor_window + 1)
        for neighbor_index in range(start, end):
            snippet = cleaned_text(blocks[neighbor_index])
            if snippet and snippet not in seen:
                seen.add(snippet)
                selected.append(snippet)
        if len(selected) >= max_blocks:
            break

    if not selected:
        return ""
    merged = cleaned_text(f"{title} {' '.join(selected)}")
    return merged[:max_chars]
