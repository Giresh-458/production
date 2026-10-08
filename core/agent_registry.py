from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from core.agent_interface import validate_common_output, validate_run_input
from core.schemas import create_error_response, create_partial_response
from core.source_registry import build_source_refresh_plan, mark_agent_sources_checked, sync_source_registry

LOGGER = logging.getLogger(__name__)

AGENT_REGISTRY = {
    "literature": {
        "module": "agents.literature_agent",
        "layer": "Literature",
        "display_name": "Literature Agent",
    },
    "funding": {
        "module": "agents.funding_agent",
        "layer": "Funding",
        "display_name": "Funding Agent",
    },
    "lab": {
        "module": "agents.lab_agent",
        "layer": "Lab",
        "display_name": "Lab Agent",
    },
    "company": {
        "module": "agents.company_agent",
        "layer": "Company",
        "display_name": "Company Agent",
    },
    "regulation": {
        "module": "agents.regulation_agent",
        "layer": "Regulation",
        "display_name": "Regulation Agent",
    },
    "opensource": {
        "module": "agents.opensource_agent",
        "layer": "OpenSource",
        "display_name": "Open-Source Agent",
    },
    "practitioner": {
        "module": "agents.practitioner_agent",
        "layer": "Practitioner",
        "display_name": "Practitioner Agent",
    },
    "investment": {
        "module": "agents.investment_agent",
        "layer": "Investment",
        "display_name": "Investment Agent",
    },
    "failure": {
        "module": "agents.failure_agent",
        "layer": "Failure",
        "display_name": "Failure Agent",
    },
    "data_availability": {
        "module": "agents.data_availability_agent",
        "layer": "DataAvailability",
        "display_name": "Data Availability Agent",
    },
    "hackathon": {
        "module": "agents.hackathon_agent",
        "layer": "Hackathon",
        "display_name": "Hackathon / Challenge Agent",
    },
    "tagging": {
        "module": "agents.tagging_agent",
        "layer": "Processing",
        "display_name": "Tagging Agent",
    },
    "clustering": {
        "module": "agents.clustering_agent",
        "layer": "Processing",
        "display_name": "Clustering Agent",
    },
    "trend": {
        "module": "agents.trend_agent",
        "layer": "Processing",
        "display_name": "Trend Agent",
    },
    "synthesis": {
        "module": "agents.synthesis_agent",
        "layer": "Intelligence",
        "display_name": "Synthesis Agent",
    },
    "idea": {
        "module": "agents.idea_agent",
        "layer": "Intelligence",
        "display_name": "Idea Agent",
    },
    "proposal": {
        "module": "agents.proposal_agent",
        "layer": "Intelligence",
        "display_name": "Proposal Agent",
    },
    "expert": {
        "module": "agents.expert_agent",
        "layer": "Expert",
        "display_name": "Expert Agent",
    },
    "adaptive_research": {
        "module": "agents.adaptive_research_agent",
        "layer": "Adaptive Research",
        "display_name": "Adaptive Research Agent",
    },
}


def get_agent(agent_name: str):
    normalized = (agent_name or "").strip().lower()
    if normalized not in AGENT_REGISTRY:
        raise KeyError(f"Unknown agent: {agent_name}")
    return importlib.import_module(AGENT_REGISTRY[normalized]["module"])


from core.funding_selection import FundingCallContext

# Explicitly list the implemented collection agents so the registry can
# distinguish them from processing and intelligence agents.  Funding is the
# discovery/selection gate for funding-driven runs, so it must be allowed to
# run before a FundingCallContext exists.
COLLECTION_AGENTS = {
    "literature", "funding", "lab", "company", "regulation",
    "opensource", "practitioner", "investment", "failure",
    "data_availability", "hackathon", "expert",
}

FUNDING_DISCOVERY_AGENT = "funding"
DOWNSTREAM_FUNDING_CONTEXT_AGENTS = COLLECTION_AGENTS - {FUNDING_DISCOVERY_AGENT}

def run_registered_agent(
    agent_name: str,
    mode: str,
    area: Optional[str] = None,
    input_data: Optional[Dict[str, Any]] = None,
    funding_context: Optional[FundingCallContext] = None,
    funding_contexts: Optional[list[FundingCallContext]] = None,
) -> dict:
    started_at = datetime.now(UTC)
    normalized = (agent_name or "").strip().lower()
    payload = input_data or {}
    spec = AGENT_REGISTRY.get(normalized)

    if spec is None:
        return create_error_response(
            agent=normalized or "unknown",
            layer="Unknown",
            mode=mode,
            area=area,
            errors=[f"unknown agent name: {agent_name}"],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    validation_errors = validate_run_input(normalized, mode, payload, funding_context=funding_context, funding_contexts=funding_contexts)
    if validation_errors:
        if "MISSING_FUNDING_CONTEXT" in validation_errors:
            return create_error_response(
                agent=normalized,
                layer=spec["layer"],
                mode=mode,
                area=area,
                errors=["MISSING_FUNDING_CONTEXT: Funding-driven configured mode requires a selected funding call."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
        return create_error_response(
            agent=normalized,
            layer=spec["layer"],
            mode=mode,
            area=area,
            errors=validation_errors,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    LOGGER.info("Starting agent run: agent=%s mode=%s area=%s", normalized, mode, area)

    if mode == "configured_scan" and not bool(payload.get("defer_registry_sync", False)):
        try:
            sync_source_registry(agent_filter=normalized)
            build_source_refresh_plan(agent_filter=normalized)
        except Exception:
            LOGGER.exception("Failed to sync source registry for agent=%s", normalized)

    try:
        module = importlib.import_module(spec["module"])
    except Exception as exc:
        LOGGER.exception("Failed to import agent module: %s", spec["module"])
        return create_error_response(
            agent=normalized,
            layer=spec["layer"],
            mode=mode,
            area=area,
            errors=[f"import error for {spec['module']}: {exc}"],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    run_fn = getattr(module, "run_agent", None)
    if not callable(run_fn):
        return create_error_response(
            agent=normalized,
            layer=spec["layer"],
            mode=mode,
            area=area,
            errors=[f"{spec['module']} does not expose run_agent()"],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    try:
        import inspect
        sig = inspect.signature(run_fn)
        kwargs = {"mode": mode, "area": area, "input_data": payload, "funding_context": funding_context}
        if "funding_contexts" in sig.parameters:
            kwargs["funding_contexts"] = funding_contexts
        response = run_fn(**kwargs)
    except Exception as exc:
        LOGGER.exception("Agent run failed: %s", normalized)
        return create_error_response(
            agent=normalized,
            layer=spec["layer"],
            mode=mode,
            area=area,
            errors=[f"runtime error inside agent: {exc}"],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    structure_errors = validate_common_output(response)
    if structure_errors:
        LOGGER.warning("Agent returned invalid structure: %s", structure_errors)
        return create_partial_response(
            agent=normalized,
            layer=spec["layer"],
            mode=mode,
            area=area,
            items_processed=response.get("items_processed", 0) if isinstance(response, dict) else 0,
            items_saved=response.get("items_saved", 0) if isinstance(response, dict) else 0,
            outputs=response.get("outputs", []) if isinstance(response, dict) else [],
            errors=structure_errors,
            warnings=["agent returned a non-standard response; wrapper normalized the failure"],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    LOGGER.info(
        "Finished agent run: agent=%s status=%s outputs=%s",
        normalized,
        response.get("status"),
        len(response.get("outputs", [])),
    )
    if mode == "configured_scan" and response.get("status") in {"success", "partial_success"}:
        try:
            # Collect URLs of sources the agent actually attempted (succeeded or
            # failed) so that only those entries get their last_checked stamp.
            attempted_urls: set[str] = set()
            for output_item in response.get("outputs", []):
                src = str(output_item.get("source", "")).strip()
                if src:
                    attempted_urls.add(src)
            # Warnings about skipped/failed sources also indicate an attempt.
            import re
            for warning in response.get("warnings", []):
                warning_str = str(warning)
                # Preserve concrete URLs from failure warnings so a failed
                # fetch is still recorded as attempted, without marking every
                # configured source as checked.
                for match in re.findall(r"https?://[^\s)\]>,]+", warning_str):
                    attempted_urls.add(match.rstrip(".,;"))
            # An empty set means the agent did not successfully identify any
            # attempted URLs. Never interpret that as "all sources checked".
            # None is reserved for explicit/manual registry maintenance.
            mark_agent_sources_checked(
                normalized,
                attempted_urls=attempted_urls,
            )
            build_source_refresh_plan(agent_filter=normalized)
        except Exception:
            LOGGER.exception("Failed to update source refresh state for agent=%s", normalized)
    return response
