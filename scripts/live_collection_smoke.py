#!/usr/bin/env python3
"""Bounded live-source smoke audit for RIF's 11 downstream collection agents.

This is an operational smoke test, not a relevance/accuracy benchmark. It uses
one configured source per collector, keeps network requests bounded, writes all
outputs beneath outputs/ci_live_collection_smoke/, and never depends on secrets.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS, run_registered_agent
from core.crawl_context import CrawlContext, current_crawl_context
from core.funding_context import (
    build_agent_specific_funding_context,
    current_funding_prompt_context,
    current_funding_queries,
    generate_funding_queries,
)
from core.funding_selection import FundingCallContext
from core.source_registry import current_force_source_refresh, current_source_area

OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "ci_live_collection_smoke"
SOURCES_ROOT = PROJECT_ROOT / "sources"
AGENT_CONFIGS = {
    "lab": "labs_sources.yaml",
    "company": "company_sources.yaml",
    "regulation": "regulation_sources.yaml",
    "opensource": "opensource_sources.yaml",
    "practitioner": "practitioner_sources.yaml",
    "investment": "investment_sources.yaml",
    "failure": "failure_sources.yaml",
    "data_availability": "data_sources.yaml",
    "hackathon": "hackathon_sources.yaml",
    "expert": "expert_sources.yaml",
}
PER_AGENT_DEADLINE_SECONDS = 35
PREFERRED_SOURCES = {
    "company": ("Securitize", "Ondo Finance"),
    "expert": ("IISc CSA Faculty", "IISc CrIS Lab"),
    "lab": ("Chainlink Labs", "Protocol Labs"),
    "practitioner": ("Ethereum Research", "Chainlink Community"),
}


def _bounded_limits() -> dict[str, int]:
    return {
        "max_pages": 1,
        "max_documents": 1,
        "max_depth": 1,
        "max_seconds": PER_AGENT_DEADLINE_SECONDS,
    }


def _prune_config(
    node: Any,
    state: dict[str, Any],
    *,
    inside_source: bool = False,
) -> Any:
    """Keep one representative configured source while preserving YAML shape.

    Prefer source names known to expose the relevant content, where a configured
    source exists, instead of always selecting the first entry in a large file.
    """
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key == "limits" and inside_source:
                result[key] = _bounded_limits()
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                if not state["selected"]:
                    preferred = {
                        name.casefold()
                        for name in state.get("preferred_names", ())
                    }
                    first = next(
                        (
                            item for item in value
                            if str(item.get("name") or "").strip().casefold() in preferred
                        ),
                        value[0],
                    )
                    state["selected"] = True
                    state["source_name"] = str(first.get("name") or "unnamed source")
                    state["source_url"] = str(first.get("url") or "")
                    result[key] = [_prune_config(first, state, inside_source=True)]
                else:
                    result[key] = []
            elif isinstance(value, list):
                # Lab configurations carry focus pages as strings. One page is
                # sufficient to prove the configured collector can fetch/extract.
                result[key] = value[:1] if inside_source and key == "focus_pages" else value
            else:
                result[key] = _prune_config(value, state, inside_source=inside_source)
        if inside_source and "limits" not in result:
            result["limits"] = _bounded_limits()
        return result
    if isinstance(node, list):
        return [_prune_config(item, state, inside_source=inside_source) for item in node]
    return node


def _make_context() -> FundingCallContext:
    # The mission is intentionally broad enough to be present in several
    # configured blockchain/security sources, while remaining a real call-like
    # research context rather than a fabricated accepted funding result.
    return FundingCallContext(
        funding_call_id="CALL-LIVE-SMOKE-20261016",
        funding_body="RIF live source smoke test",
        program_name="Privacy-preserving blockchain infrastructure",
        call_id="LIVE-SMOKE-20261016",
        call_title="Research on secure blockchain infrastructure, privacy, scalability, and tokenized assets",
        status="OPEN",
        opening_date="2026-01-01",
        deadline="2027-12-31",
        funding_amount="1000000",
        currency="EUR",
        duration="24 months",
        eligibility="Universities and research organizations with relevant technical research capability.",
        geography="EU",
        trl="TRL3-TRL6",
        research_priorities=[
            "blockchain infrastructure",
            "privacy-preserving verification",
            "distributed systems security",
            "scalability and secure settlement",
            "real-world asset tokenization",
        ],
        required_partners="Research and industry partners",
        deliverables="Prototype and evidence-backed evaluation report",
        evaluation_criteria="Technical merit, impact, and feasibility",
        eligible_costs="Personnel, equipment, and travel",
        application_url="https://example.org/live-smoke/apply",
        source_url="https://example.org/live-smoke/call",
        source_name="RIF smoke-test fixture",
        publication_date="2026-01-01",
        last_updated="2026-10-16",
        research_area="RWA",
        selection_mode="smoke_test",
        selection_score=1.0,
        selection_reasons=["Bounded live-source operational smoke test"],
    )


def _prepare_one_source_config(agent: str, run_root: Path, report: dict[str, Any]) -> Path:
    source_file = SOURCES_ROOT / AGENT_CONFIGS[agent]
    raw = yaml.safe_load(source_file.read_text(encoding="utf-8")) or {}
    state: dict[str, Any] = {
        "selected": False,
        "source_name": "",
        "source_url": "",
        "preferred_names": PREFERRED_SOURCES.get(agent, ()),
    }
    pruned = _prune_config(raw, state)
    if not state["selected"] or not state["source_url"].startswith(("https://", "http://")):
        raise RuntimeError(f"{agent}: no valid configured source could be selected from {source_file}")
    config_path = run_root / "configs" / f"{agent}_one_source.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(pruned, sort_keys=False, allow_unicode=True), encoding="utf-8")
    report["configured_source"] = {
        "source_file": str(source_file.relative_to(PROJECT_ROOT)),
        "name": state["source_name"],
        "url": state["source_url"],
        "config_path": str(config_path),
        "limits": _bounded_limits(),
    }
    return config_path


def _run_agent(agent: str, context: FundingCallContext, run_root: Path, run_id: str) -> dict[str, Any]:
    started = time.monotonic()
    output_dir = run_root / "intermediate" / agent
    db_path = run_root / "db" / f"{agent}.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "agent": agent,
        "started_at": datetime.now(UTC).isoformat(),
        "configured_scan": True,
        "status": "not_started",
        "items_processed": 0,
        "items_saved": 0,
        "outputs_count": 0,
        "errors": [],
        "warnings": [],
    }

    payload: dict[str, Any] = {
        "analysis_mode": "collect_only",
        "output_dir": str(output_dir),
        "db_path": str(db_path),
        "run_id": run_id,
        "force_refresh": True,
        "defer_registry_sync": True,
    }
    if agent in AGENT_CONFIGS:
        config_path = _prepare_one_source_config(agent, run_root, report)
        payload["sources_path"] = str(config_path)
    else:
        report["configured_source"] = {
            "source_file": "provider APIs configured in agents/literature_agent.py",
            "name": "arXiv, OpenAlex, Semantic Scholar, Crossref, DBLP",
            "limits": "provider-side result caps; no synthetic/mock provider responses",
        }

    crawl = CrawlContext(deadline=time.time() + PER_AGENT_DEADLINE_SECONDS)
    prompt_context = build_agent_specific_funding_context(agent, context)
    query_context = list(dict.fromkeys(generate_funding_queries(context)))
    force_token = current_force_source_refresh.set(True)
    area_token = current_source_area.set(None)
    crawl_token = current_crawl_context.set(crawl)
    prompt_token = current_funding_prompt_context.set(prompt_context)
    query_token = current_funding_queries.set(query_context)
    try:
        response = run_registered_agent(
            agent_name=agent,
            mode="configured_scan",
            area=None,
            input_data=payload,
            funding_context=context,
        )
        report["status"] = str(response.get("status", "unknown"))
        report["items_processed"] = int(response.get("items_processed", 0) or 0)
        report["items_saved"] = int(response.get("items_saved", 0) or 0)
        report["outputs_count"] = len(response.get("outputs", []) or [])
        report["errors"] = [str(item) for item in (response.get("errors") or [])]
        report["warnings"] = [str(item) for item in (response.get("warnings") or [])]
        report["outputs"] = [
            {
                "title": str(item.get("title") or item.get("program_name") or ""),
                "source": str(item.get("source") or item.get("source_url") or ""),
                "markdown_path": str(item.get("markdown_path") or ""),
            }
            for item in (response.get("outputs") or [])[:5]
        ]
        report["crawl_telemetry"] = {
            "http_requests": crawl.http_requests,
            "pages_fetched": crawl.pages_fetched,
            "documents_collected": crawl.documents_collected,
            "browser_attempts": crawl.browser_attempts,
            "browser_successes": crawl.browser_successes,
            "browser_failures": crawl.browser_failures,
            "stop_reasons": sorted(crawl.stop_reasons),
            "exceptions": crawl.exceptions[:10],
            "source_limits_telemetry": crawl.source_limits_telemetry[:5],
        }
    except Exception as exc:
        report["status"] = "exception"
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
        report["crawl_telemetry"] = {
            "http_requests": crawl.http_requests,
            "pages_fetched": crawl.pages_fetched,
            "documents_collected": crawl.documents_collected,
            "stop_reasons": sorted(crawl.stop_reasons),
            "exceptions": crawl.exceptions[:10],
        }
    finally:
        current_funding_queries.reset(query_token)
        current_funding_prompt_context.reset(prompt_token)
        current_crawl_context.reset(crawl_token)
        current_source_area.reset(area_token)
        current_force_source_refresh.reset(force_token)

    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["finished_at"] = datetime.now(UTC).isoformat()
    # "No evidence saved" is distinguished from a fatal collector failure:
    # irrelevant live results are informative, but must be inspected explicitly.
    report["usable_output"] = report["items_saved"] > 0 and report["outputs_count"] > 0
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_root = OUTPUT_ROOT / ("run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    run_root.mkdir(parents=True, exist_ok=True)
    context = _make_context()
    run_id = "live-smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    agents = sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    results = []
    for agent in agents:
        print(f"\n=== Live source smoke: {agent} ===", flush=True)
        result = _run_agent(agent, context, run_root, run_id)
        results.append(result)
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)

    fatal = [
        {"agent": item["agent"], "status": item["status"], "errors": item["errors"]}
        for item in results
        if item["status"] in {"error", "exception"}
    ]
    no_evidence = [item["agent"] for item in results if not item["usable_output"]]
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "run_root": str(run_root),
        "agent_count": len(results),
        "fatal_count": len(fatal),
        "no_evidence_count": len(no_evidence),
        "no_evidence_agents": no_evidence,
        "fatal_agents": fatal,
        "agents": results,
        "interpretation": (
            "Live-source smoke only: each configured collector was given at most one source, "
            "with bounded fetch limits. An agent with no saved evidence is not certified and "
            "must be reviewed; live website behavior is not a relevance-accuracy benchmark."
        ),
    }
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUTPUT_ROOT / "latest_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\n=== Live source smoke summary ===", flush=True)
    print(json.dumps({
        "summary_path": str(summary_path),
        "agent_count": len(results),
        "fatal_count": len(fatal),
        "no_evidence_count": len(no_evidence),
        "no_evidence_agents": no_evidence,
    }, indent=2), flush=True)
    # Keep no-evidence outcomes visible in the uploaded report without masking
    # them as code failures; fatal errors fail the operational smoke workflow.
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
