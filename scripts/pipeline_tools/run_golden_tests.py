from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_registry import run_registered_agent
from core.evidence import build_evidence_bundle
from core.quality import evaluate_collection_quality
from core.recursive_collection import extract_ranked_links
from core.schemas import resolve_research_area

CASES_PATH = PROJECT_ROOT / "evaluation" / "golden_cases" / "cases.json"
REPORT_PATH = PROJECT_ROOT / "outputs" / "workflow" / "golden_test_report.json"
TEMP_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "golden_tmp"


def _load_cases() -> list[dict[str, Any]]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [item for item in data if isinstance(item, dict)]


def _cleanup_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def _markdown_has_section(markdown: str, section_name: str) -> bool:
    marker = f"## {section_name}".strip()
    return marker in markdown


def _lower_contains_all(text: str, substrings: list[str]) -> bool:
    lowered = text.lower()
    return all(substring.lower() in lowered for substring in substrings)


def _run_agent_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    agent = str(case["agent"])
    mode = str(case["mode"])
    area = case.get("area")
    payload = dict(case.get("input_data", {}))
    temp_root = TEMP_OUTPUT_ROOT / case_id
    temp_output_dir = temp_root / "artifacts"
    temp_manifest_dir = temp_root / "manifest"
    payload.setdefault("output_dir", str(temp_output_dir))
    if agent == "synthesis":
        payload.setdefault("manifest_dir", str(temp_manifest_dir))
        payload.setdefault("outputs_root", str(PROJECT_ROOT / "outputs"))

    response = run_registered_agent(agent, mode, area, payload)
    failures: list[str] = []
    expected = case.get("expected", {})

    if response.get("status") != expected.get("status"):
        failures.append(f"expected status {expected.get('status')} but got {response.get('status')}")

    if int(response.get("items_saved", 0)) < int(expected.get("min_items_saved", 0)):
        failures.append(
            f"expected at least {expected.get('min_items_saved', 0)} saved item(s) but got {response.get('items_saved', 0)}"
        )

    outputs = response.get("outputs", [])
    first_output = outputs[0] if outputs else {}
    problem_text = str(first_output.get("problem", ""))
    if expected.get("problem_contains") and not _lower_contains_all(problem_text, list(expected["problem_contains"])):
        failures.append("output problem field did not contain the expected signal phrases")

    markdown_path = Path(str(first_output.get("markdown_path", ""))) if first_output.get("markdown_path") else None
    markdown_text = ""
    if markdown_path and markdown_path.exists():
        markdown_text = markdown_path.read_text(encoding="utf-8")
        for section_name in expected.get("markdown_sections", []):
            if not _markdown_has_section(markdown_text, str(section_name)):
                failures.append(f"missing markdown section: {section_name}")
        if expected.get("markdown_contains") and not _lower_contains_all(markdown_text, list(expected["markdown_contains"])):
            failures.append("markdown artifact did not contain the expected signal phrases")
    else:
        failures.append("expected markdown artifact was not created")

    _cleanup_path(temp_root)
    return {
        "id": case_id,
        "type": case["type"],
        "passed": not failures,
        "failures": failures,
        "response_summary": {
            "status": response.get("status"),
            "items_processed": response.get("items_processed"),
            "items_saved": response.get("items_saved"),
            "warnings": response.get("warnings", []),
            "errors": response.get("errors", []),
        },
    }


def _run_quality_case(case: dict[str, Any]) -> dict[str, Any]:
    title = str(case.get("title", ""))
    raw_content = str(case.get("raw_content", ""))
    explicit_area = case.get("explicit_area")
    resolved_area, _, area_scores = resolve_research_area(explicit_area=explicit_area, title=title, raw_content=raw_content)
    evidence_bundle = build_evidence_bundle(
        title=title,
        raw_content=raw_content,
        research_area=resolved_area,
        metadata={"source": str(case.get("source", ""))},
    )
    result = evaluate_collection_quality(
        agent_name=str(case.get("agent_name", "")),
        title=title,
        source=str(case.get("source", "")),
        research_area=resolved_area,
        raw_content=raw_content,
        evidence_bundle=evidence_bundle,
        area_scores=area_scores,
    )

    failures: list[str] = []
    expected = case.get("expected", {})
    if bool(result.get("passed")) != bool(expected.get("passed")):
        failures.append(f"expected passed={expected.get('passed')} but got {result.get('passed')}")
    reason = str(result.get("rejection_reason", ""))
    for snippet in expected.get("reason_contains", []):
        if str(snippet).lower() not in reason.lower():
            failures.append(f"missing rejection reason snippet: {snippet}")

    return {
        "id": str(case["id"]),
        "type": case["type"],
        "passed": not failures,
        "failures": failures,
        "quality_result": result,
    }


def _run_recursive_case(case: dict[str, Any]) -> dict[str, Any]:
    fixture_path = PROJECT_ROOT / str(case.get("fixture", ""))
    html = fixture_path.read_text(encoding="utf-8")
    ranked_links = extract_ranked_links(
        base_url=str(case.get("base_url", "")),
        html=html,
        allowed_domains=tuple(case.get("allowed_domains", [])),
        link_keywords=tuple(case.get("link_keywords", [])),
        relevance_threshold=int(case.get("relevance_threshold", 1)),
    )
    urls = [item[0] for item in ranked_links]
    failures: list[str] = []
    expected = case.get("expected", {})
    for included in expected.get("included_urls", []):
        if included not in urls:
            failures.append(f"expected ranked links to include {included}")
    for excluded in expected.get("excluded_urls", []):
        if excluded in urls:
            failures.append(f"expected ranked links to exclude {excluded}")

    return {
        "id": str(case["id"]),
        "type": case["type"],
        "passed": not failures,
        "failures": failures,
        "ranked_links": [
            {"url": url, "score": score, "anchor_text": anchor_text}
            for url, score, anchor_text in ranked_links
        ],
    }


def run_golden_tests() -> dict[str, Any]:
    cases = _load_cases()
    results: list[dict[str, Any]] = []
    TEMP_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    for case in cases:
        case_type = str(case.get("type", "")).strip()
        if case_type == "agent_run":
            result = _run_agent_case(case)
        elif case_type == "quality_gate":
            result = _run_quality_case(case)
        elif case_type == "recursive_links":
            result = _run_recursive_case(case)
        else:
            result = {
                "id": str(case.get("id", "unknown")),
                "type": case_type,
                "passed": False,
                "failures": [f"unsupported golden case type: {case_type}"],
            }
        results.append(result)

    _cleanup_path(TEMP_OUTPUT_ROOT)
    passed_count = sum(1 for item in results if item.get("passed"))
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases_path": str(CASES_PATH.resolve()),
        "summary": {
            "total_cases": len(results),
            "passed_cases": passed_count,
            "failed_cases": len(results) - passed_count,
        },
        "results": results,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    report = run_golden_tests()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if int(report["summary"]["failed_cases"]) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
