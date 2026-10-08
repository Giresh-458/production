from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS, FUNDING_DISCOVERY_AGENT, run_registered_agent
from core.batch_runner import execute_batch_run
from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult
from core.normalization import save_normalized_collection_records
from core.schemas import ALLOWED_RESEARCH_AREAS, normalize_area
from core.mission_gate import relevance as mission_relevance



def evaluate_collection_health(collection_tasks: list[dict], min_required_agents: int) -> tuple[bool, int]:
    healthy_agents = set()
    for t in collection_tasks:
        agent_name = str(t.get("agent_name"))
        status = str(t.get("status", "")).lower()
        outputs_count = int(t.get("outputs_count", 0) or 0)
        
        # An agent is healthy if it succeeded (or partially succeeded) AND produced evidence.
        if status in ("success", "partial_success", "partial_limit") and outputs_count > 0:
            healthy_agents.add(agent_name)
    
    is_healthy = len(healthy_agents) >= min_required_agents
    return is_healthy, len(healthy_agents)

def run_stage(agent: str, mode: str, area: str | None, payload: dict[str, Any], funding_context: FundingCallContext | None = None) -> dict[str, Any]:
    return run_registered_agent(agent, mode, area=area, input_data=payload, funding_context=funding_context)


def _parse_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    date_only = bool(__import__("re").fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)
        if date_only and end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt
    except ValueError:
        return None


def get_all_open_funding_calls(funding_result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select all genuinely open funding opportunities from the live funding scan."""
    now = datetime.now(UTC)
    candidates: list[tuple[float, dict[str, Any]]] = []
    rejected: list[dict[str, Any]] = []
    
    seen_hashes: set[str] = set()
    import hashlib
    
    for record in funding_result.get("outputs", []) or []:
        status = str(record.get("status", "")).upper()
        deadline = _parse_date(record.get("deadline"), end_of_day=True)
        opening = _parse_date(record.get("opening_date"))
        reasons: list[str] = []
        if status == "CLOSED_CALL" or (deadline and deadline < now):
            reasons.append("closed or deadline has passed")
        if opening and opening > now:
            reasons.append("not open yet")
        if status != "OPEN_CALL":
            reasons.append(f"status={status or 'UNKNOWN'}")
        if reasons:
            rejected.append({"title": record.get("title"), "status": status, "reasons": reasons, "source_url": record.get("source_url")})
            continue

        missing_required = [f for f in ["title"] if not record.get(f)]
        if not (record.get("source_url") or record.get("application_url")):
            missing_required.append("source_url/application_url")
        if missing_required:
            rejected.append({"title": record.get("title"), "status": status, "reasons": [f"missing required fields: {missing_required}"], "source_url": record.get("source_url")})
            continue

        program_name = str(record.get("program_name") or record.get("title") or "").strip()
        generic_names = {"how to apply", "apply", "request for proposals", "funding", "grants", "programs"}
        if program_name.lower().rstrip(" .:-") in generic_names:
            rejected.append({"title": record.get("title"), "status": status, "reasons": ["generic landing-page title is not an identifiable funding call"], "source_url": record.get("source_url")})
            continue
        evidence_score = sum(1 for f in ["deadline", "application_url", "funding_amount", "eligibility", "evaluation_criteria"] if record.get(f))
        has_call_identity = program_name.lower() not in generic_names
        has_submission_evidence = bool(record.get("application_url"))
        has_timeline_evidence = bool(record.get("deadline") or record.get("opening_date"))
        status_evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
        has_explicit_open_evidence = bool(status_evidence.get("status")) or status == "OPEN_CALL"
        if evidence_score < 1 or not has_call_identity or (not has_timeline_evidence and not has_explicit_open_evidence) or not (has_submission_evidence or has_explicit_open_evidence):
            rejected.append({"title": record.get("title"), "status": status, "reasons": ["insufficient evidence for a verified individual call"], "source_url": record.get("source_url")})
            continue

        # Deduplication check
        call_id = record.get("call_id")
        if not call_id:
            # Fallback to hashing application URL and title to handle different discovery URLs for same call
            app_url = str(record.get("application_url") or "").strip().lower()
            title = str(record.get("title") or "").strip().lower()
            org = str(record.get("organization") or record.get("source_name") or "").strip().lower()
            base_str = f"{app_url}|{title}|{org}"
            call_id = f"CALL-{hashlib.sha256(base_str.encode('utf-8')).hexdigest()[:10].upper()}"
        
        if call_id in seen_hashes:
            rejected.append({"title": record.get("title"), "status": status, "reasons": ["duplicate of existing valid call"], "source_url": record.get("source_url")})
            continue
        seen_hashes.add(call_id)

        relevance = record.get("domain_relevance") or []
        top_score = max((float(item.get("score", 0.0) or 0.0) for item in relevance), default=0.0)
        score = 0.55 + min(top_score, 0.30)
        if record.get("deadline"):
            score += 0.06
        if record.get("application_url"):
            score += 0.06
        if record.get("funding_amount"):
            score += 0.04
        if deadline:
            days = max((deadline - now).total_seconds() / 86400.0, 0.0)
            score += max(0.0, min(0.08, 0.08 * (1.0 - min(days, 120.0) / 120.0)))
        record = dict(record)
        record["selection_score"] = round(min(score, 0.99), 4)
        record["call_id"] = call_id
        candidates.append((score, record))

    candidates.sort(key=lambda pair: (-pair[0], str(pair[1].get("title", ""))))
    selected = [pair[1] for pair in candidates]
    
    return selected, rejected


def build__selected_call_context(record: dict[str, Any], mode: str = "autonomous_live") -> FundingCallContext:
    call_id = record.get("call_id")
    if not call_id:
        app_url = str(record.get("application_url") or "").strip().lower()
        title = str(record.get("title") or "").strip().lower()
        org = str(record.get("organization") or record.get("source_name") or "").strip().lower()
        call_id = f"CALL-{__import__('hashlib').sha256(f'{app_url}|{title}|{org}'.encode('utf-8')).hexdigest()[:10].upper()}"
    rc = record.get("research_context") if isinstance(record.get("research_context"), dict) else None
    selected_domains = []
    if rc:
        selected_domains = list(rc.get("selected_domains") or [])
    if not selected_domains:
        selected_domains = [item.get("domain") for item in (record.get("domain_relevance") or []) if float(item.get("score", 0.0) or 0.0) >= 0.10][:3]
    if not selected_domains and normalize_area(record.get("focus_area")) in ALLOWED_RESEARCH_AREAS:
        selected_domains = [normalize_area(record.get("focus_area"))]
    if rc is None:
        rc = {
            "funding_call_id": call_id,
            "domain": record.get("focus_area") or "General",
            "technology_themes": tuple(record.get("research_priorities") or []),
            "funding_priorities": tuple(record.get("research_priorities") or []),
            "target_outcomes": tuple(),
            "investigation_questions": tuple(),
            "inferred_challenges": tuple(),
            "evidence_refs": tuple([record.get("source_url") or record.get("application_url") or ""]),
            "inference_provenance": ("FUNDING_CALL",),
            "confidence": "Medium",
            "domain_relevance": tuple(record.get("domain_relevance") or []),
            "selected_domains": tuple(selected_domains),
        }
    # The dataclass accepts a typed FundingResearchContext; coerce through the
    # canonical factory to keep serialization consistent.
    scored = ScoredCall(
        call_id=str(call_id),
        record=record,
        score=float(record.get("selection_score", 0.8) or 0.8),
        validation=ValidationResult(True, ["Live funding scan selected an open call."]),
    )
    context = FundingCallContext.from_scored_call(scored, mode=mode, extra_reasons=["Verified as open by current-date status/deadline checks."])
    return context


def select_collection_areas(funding_output: dict[str, Any], detected_area: str | None = None) -> list[str]:
    """Route only when canonical-domain evidence is strong; otherwise use adaptive mode."""
    ranked = funding_output.get("domain_relevance") or []
    selected: list[str] = []
    for item in ranked:
        domain = normalize_area(item.get("domain"))
        score = float(item.get("score", 0.0) or 0.0)
        raw_score = float(item.get("raw_score", 0.0) or 0.0)
        matches = list(item.get("matched_terms", []) or [])
        has_detailed_evidence = any(key in item for key in ("raw_score", "matched_terms"))
        materially_supported = (score >= 0.30 and raw_score >= 4.0 and len(matches) >= 2) if has_detailed_evidence else score >= 0.10
        if domain in ALLOWED_RESEARCH_AREAS and materially_supported:
            selected.append(domain)
        if len(selected) >= 3:
            break
    return list(dict.fromkeys(selected[:3]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live RIF funding-to-proposal pipeline.")
    parser.add_argument("--force-refresh", action="store_true", help="Force live refetch of every source (default live mode already does this).")
    parser.add_argument("--scheduled-refresh", action="store_true", help="Opt into source refresh schedules instead of live-refetching sources on this run.")
    parser.add_argument("--input-file", default=None, help="Optional manual funding-call text. If omitted, RIF discovers open calls from configured live sources.")
    parser.add_argument("--output-root", default="outputs/pipeline_run")
    parser.add_argument("--area", default=None, help="Optional canonical research-area hint for ranking; never overrides the funding evidence.")
    parser.add_argument("--funding-sources", default="sources/funding_sources.yaml")
    parser.add_argument("--max-open-calls", type=int, default=None, help="Maximum number of open calls to send downstream (default: all valid calls).")
    args = parser.parse_args()

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = Path(args.output_root) / run_id
    root.mkdir(parents=True, exist_ok=True)
    print("==============================================")
    print(f"🚀 Starting RIF Live Pipeline: {run_id}")
    print("==============================================")

    # 1. FUNDING DISCOVERY — never use test.txt unless explicitly requested.
    if args.input_file:
        input_path = Path(args.input_file)
        if not input_path.exists():
            print(f"Error: input file not found: {input_path}")
            return 1
        funding_mode = "manual_text"
        funding_payload = {
            "text": input_path.read_text(encoding="utf-8"),
            "title": f"Manual Funding Call ({input_path.name})",
            "source": f"manual://{input_path}",
            "organization": "Manual Input",
        }
        print(f"\n[1/6] Funding Agent — manual override: {input_path}")
    else:
        funding_mode = "configured_scan"
        funding_payload = {
            "sources_path": args.funding_sources,
            "posts_per_source": 6,
            "analysis_mode": "collect_only",
            "output_dir": str(root / "intermediate" / "funding"),
            "run_id": run_id,
            "force_refresh": bool(getattr(args, "force_refresh", False) or not getattr(args, "scheduled_refresh", False)),
        }
        print("\n[1/6] Funding Agent — discovering LIVE OPEN CALLS from configured sources...")

    funding_result = run_stage("funding", funding_mode, args.area if args.input_file else None, funding_payload)
    if not funding_result.get("outputs"):
        report = {"run_id": run_id, "status": "no_funding_candidates", "funding_result": funding_result}
        (root / "workflow").mkdir(parents=True, exist_ok=True)
        (root / "workflow" / "pipeline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("❌ No funding opportunities were discovered.")
        return 2

    if args.input_file:
        valid_calls = [funding_result["outputs"][0]]
        rejected = []
    else:
        valid_calls, rejected = get_all_open_funding_calls(funding_result)
        if args.max_open_calls is not None and args.max_open_calls > 0:
            valid_calls = valid_calls[:args.max_open_calls]
        
    if not valid_calls:
        report = {"run_id": run_id, "status": "no_open_call", "funding_result": funding_result, "rejected_calls": rejected}
        (root / "workflow").mkdir(parents=True, exist_ok=True)
        (root / "workflow" / "pipeline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("❌ Funding scan found no currently open call. Pipeline stopped before downstream research.")
        return 3

    print(f"   ✅ Discovered {len(valid_calls)} valid open funding calls.")
    
    # Pre-sync registry ONCE before parallel operations
    try:
        from core.source_registry import sync_source_registry, build_source_refresh_plan
        sync_source_registry()
        build_source_refresh_plan()
    except Exception as exc:
        print(f"   ⚠️ Source registry pre-sync warning: {exc}")

    downstream_agents = sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    if len(downstream_agents) != 11:
        raise AssertionError(f"Expected exactly 11 downstream funding context collectors, found {len(downstream_agents)}: {downstream_agents}")


    # ---------------------------------------------------------
    # GLOBAL SHARED COLLECTION STAGE
    # ---------------------------------------------------------
    print(f"\n==============================================")
    print(f"▶️ GLOBAL SHARED COLLECTION FOR {len(valid_calls)} CALLS")
    print(f"==============================================")
    
    all_contexts = [build__selected_call_context(c) for c in valid_calls]
    global_workflow = root / "workflow"
    global_workflow.mkdir(parents=True, exist_ok=True)
    
    print(f"\n[1.5/6] Running shared downstream collection agents ({len(downstream_agents)})...")
    downstream_areas = [None]
    
    try:
        downstream_plan = execute_batch_run(
            agent_names=downstream_agents,
            area_names=downstream_areas,
            mode="configured_scan",
            base_input_data={
                "analysis_mode": "collect_only",
                "pipeline_run_dir": str(root), # output to root/intermediate
                "force_refresh": bool(getattr(args, "force_refresh", False) or not getattr(args, "scheduled_refresh", False)),
                "allow_llm_refinement": False,
            },
            execution_mode="parallel",
            max_workers=6,
            timeout_budget_seconds=1500,
            dry_run=False,
            report_path=global_workflow / "shared_downstream_collection_plan.json",
            funding_contexts=all_contexts, # PASS ALL CONTEXTS!
        )
    except Exception as exc:
        print(f"❌ Shared collection failed with exception: {exc}")
        return 4
        
    collection_tasks = downstream_plan.get("tasks", []) if isinstance(downstream_plan, dict) else []
    min_required = max(3, len(downstream_agents) // 3)
    is_healthy, healthy_count = evaluate_collection_health(collection_tasks, min_required)
    
    collector_health = {"healthy_agents": healthy_count, "total_agents": len(downstream_agents), "required": min_required}
    if not is_healthy:
        print(f"❌ Shared collection quality gate failed: {healthy_count}/{len(downstream_agents)} unique collectors produced evidence.")
        return 4
    # ---------------------------------------------------------

    total_proposals = 0
    all_final_outputs = []
    global_exit_code = 0
    all_reports = []

    for call_idx, selected in enumerate(valid_calls, 1):
        context = build__selected_call_context(selected)
        canonical_areas = select_collection_areas(selected, args.area)
        unknown_area = not canonical_areas or max((float(x.get("score", 0.0) or 0.0) for x in selected.get("domain_relevance", []) or []), default=0.0) < 0.10
        
        call_root = root / "calls" / context.funding_call_id
        call_root.mkdir(parents=True, exist_ok=True)
        
        selected_path = call_root / "workflow" / "selected_funding_call.json"
        selected_path.parent.mkdir(parents=True, exist_ok=True)
        selected_path.write_text(json.dumps({"selected": selected, "rejected_alternatives": rejected if call_idx == 1 else []}, indent=2, ensure_ascii=False), encoding="utf-8")
        
        print(f"\n==============================================")
        print(f"▶️ Processing Call {call_idx}/{len(valid_calls)}: {selected.get('program_name') or selected.get('title')}")
        print(f"==============================================")
        print(f"   ✅ Selected: {selected.get('program_name') or selected.get('title')} | {selected.get('status')} | deadline={selected.get('deadline') or 'not specified'}")
        print(f"   🎯 Canonical research areas: {', '.join(canonical_areas) if canonical_areas else 'none — adaptive research mode'}")
        
        research_ctx = context.research_context
        research_intent = getattr(research_ctx, "research_intent", "unknown") if research_ctx else "unknown"
        if research_intent != "research":
            print(f"   ℹ️ Selected funding call is classified as {research_intent}; no research mission was established. No proposal will be generated.")
            workflow = call_root / "workflow"; workflow.mkdir(parents=True, exist_ok=True)
            report = {
                "run_id": run_id, "status": "no_research_opportunity", "output_root": str(call_root.resolve()),
                "funding": {"selected": selected, "selected_funding_call_id": context.funding_call_id},
                "research_intent": research_intent,
                "research_intent_evidence": list(getattr(research_ctx, "research_intent_evidence", ()) if research_ctx else ()),
                "final_output": None, "proposal_count": 0,
            }
            (workflow / "pipeline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            all_reports.append(report)
            continue



        adaptive_result = None
        if unknown_area or len(canonical_areas) < 2:
            print("   -> Running adaptive research discovery for potentially new/underrepresented research space...")
            try:
                adaptive_result = run_stage(
                    "adaptive_research", "configured_scan", "Unscoped",
                    {"output_dir": str(call_root / "intermediate" / "adaptive_research"), "run_id": run_id},
                    funding_context=context,
                )
            except Exception as exc:
                print(f"   ⚠️ Adaptive research failed: {exc}")

        print("\n[3/6] Normalizing evidence...")
        try:
            normalized = save_normalized_collection_records(root / "intermediate", call_root, context.funding_call_id)
        except Exception as exc:
            print(f"❌ Normalization failed with exception: {exc}")
            global_exit_code = max(global_exit_code, 4)
            continue
            
        records_path = call_root / "normalized" / "records.json"
        records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else []
        if not records:
            print("❌ No normalized evidence. Stopping rather than inventing a research gap.")
            global_exit_code = max(global_exit_code, 4)
            continue

        gated_records = []
        gate_details = []
        for record in records:
            gate = mission_relevance(record, context)
            record["mission_relevance"] = gate
            if gate["passed"]:
                gated_records.append(record)
            else:
                gate_details.append({"record_id": record.get("record_id"), "title": record.get("title"), "source_url": record.get("source_url"), **gate})
        rejected_count = len(records) - len(gated_records)
        records = gated_records
        records_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (call_root / "workflow" / "mission_relevance_gate.json").write_text(json.dumps({"input_records": rejected_count + len(records), "accepted_records": len(records), "rejected_records": rejected_count, "details": gate_details[:500]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"   🔎 Mission relevance gate: {len(records)} accepted / {rejected_count} rejected")
        if not records:
            report = {"run_id": run_id, "status": "no_relevant_evidence", "output_root": str(call_root.resolve()), "funding": {"selected": selected, "selected_funding_call_id": context.funding_call_id}, "mission_relevance_gate": {"accepted": 0, "rejected": rejected_count}, "final_output": None, "proposal_count": 0}
            (call_root / "workflow" / "pipeline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print("⚠️ Funding call found, but no collected evidence passed the mission relevance gate. No proposal will be generated.")
            all_reports.append(report)
            continue

        print("\n[4/6] Processing evidence: tagging → clustering → trends...")
        stage_results: dict[str, Any] = {}
        process_area = canonical_areas[0] if canonical_areas else "Unscoped"
        processing_failed = False
        for agent, payload in [
            ("tagging", {"outputs_root": str(call_root), "output_dir": str(call_root / "processed" / "tags")}),
            ("clustering", {"outputs_root": str(call_root), "output_dir": str(call_root / "processed" / "clusters"), "threshold": 0.20, "llm_ambiguity_resolution": False}),
            ("trend", {"outputs_root": str(call_root), "output_dir": str(call_root / "processed" / "trends")}),
        ]:
            try:
                res = run_stage(agent, "configured_scan", process_area if process_area != "Unscoped" else None, payload, context)
                stage_results[agent] = res
                if str(res.get("status", "")).lower() not in ("success", "partial_success"):
                    print(f"❌ Processing stage '{agent}' failed with status: {res.get('status')}")
                    processing_failed = True
                    break
            except Exception as exc:
                print(f"❌ Processing stage '{agent}' failed with exception: {exc}")
                processing_failed = True
                break
                
        if processing_failed:
            global_exit_code = max(global_exit_code, 4)
            continue

        print("\n[5/6] Intelligence: synthesis → ideas → proposals...")
        try:
            stage_results["synthesis"] = run_stage("synthesis", "configured_scan", process_area if process_area != "Unscoped" else None, {
                "outputs_root": str(call_root), "output_dir": str(call_root / "synthesis" / "generated"), "manifest_dir": str(call_root / "synthesis" / "manifest")
            }, context)
            synthesis_ok = str(stage_results["synthesis"].get("status", "")).lower() in ("success", "partial_success") and int(stage_results["synthesis"].get("outputs_count", len(stage_results["synthesis"].get("outputs", []) or [])) or 0) > 0
        except Exception as exc:
            print(f"❌ Synthesis failed with exception: {exc}")
            synthesis_ok = False
            
        if not synthesis_ok:
            print("❌ Synthesis failed or produced no output; refusing to generate a proposal from degraded evidence.")
            report = {
                "run_id": run_id, "status": "failed_quality_gate", "output_root": str(call_root.resolve()),
                "funding": {"selected": selected, "rejected_alternatives": rejected if call_idx == 1 else [], "selected_funding_call_id": context.funding_call_id},
                "collection": downstream_plan, "adaptive_research": adaptive_result, "stage_results": stage_results,
                "failure_reason": "synthesis_failed_or_empty", "final_output": None, "proposal_count": 0,
            }
            workflow = call_root / "workflow"; workflow.mkdir(parents=True, exist_ok=True)
            (workflow / "pipeline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            global_exit_code = max(global_exit_code, 5)
            all_reports.append(report)
            continue
            
        try:
            stage_results["idea"] = run_stage("idea", "configured_scan", process_area if process_area != "Unscoped" else None, {
                "output_dir": str(call_root / "ideas"), "manifest_dir": str(call_root / "synthesis" / "manifest"),
                "external_novelty_validation": True
            }, context)
            idea_ok = str(stage_results["idea"].get("status", "")).lower() in ("success", "partial_success")
        except Exception as exc:
            print(f"❌ Idea generation failed with exception: {exc}")
            idea_ok = False
            
        if not idea_ok:
            print("❌ Idea generation failed; refusing to generate a proposal from incomplete intelligence.")
            global_exit_code = max(global_exit_code, 5)
            continue
            
        try:
            stage_results["proposal"] = run_stage("proposal", "configured_scan", process_area if process_area != "Unscoped" else None, {
                "output_dir": str(call_root / "proposals"), "idea_dir": str(call_root / "ideas"), "__selected_call_context": context
            }, context)
            proposal_ok = str(stage_results["proposal"].get("status", "")).lower() in ("success", "partial_success")
        except Exception as exc:
            print(f"❌ Proposal generation failed with exception: {exc}")
            proposal_ok = False

        proposal_dir = call_root / "proposals"
        proposal_files = sorted(proposal_dir.rglob("*.md")) if proposal_dir.exists() else []
        final_output = proposal_files[0] if proposal_files else None
        
        if not proposal_ok or not final_output:
            print("❌ Proposal generation failed or produced no output.")
            global_exit_code = max(global_exit_code, 5)
            call_run_status = "failed_proposal_generation"
        else:
            call_run_status = "completed"
            total_proposals += len(proposal_files)
            if final_output:
                all_final_outputs.append(str(final_output.resolve()))

        report = {
            "run_id": run_id,
            "status": call_run_status,
            "output_root": str(call_root.resolve()),
            "funding": {
                "selected": selected,
                "rejected_alternatives": rejected if call_idx == 1 else [],
                "selected_funding_call_id": context.funding_call_id,
            },
            "research_routing": {
                "canonical_areas": canonical_areas,
                "adaptive_mode": bool(adaptive_result),
            },
            "collection": downstream_plan,
            "adaptive_research": adaptive_result,
            "stage_results": stage_results,
            "final_output": str(final_output.resolve()) if final_output else None,
            "proposal_count": len(proposal_files),
        }
        workflow = call_root / "workflow"
        workflow.mkdir(parents=True, exist_ok=True)
        (workflow / "pipeline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        (workflow / "pipeline_manifest.json").write_text(json.dumps({
            "run_id": run_id,
            "selected_funding_call_id": context.funding_call_id,
            "selected_program": selected.get("program_name") or selected.get("title"),
            "selected_status": selected.get("status"),
            "canonical_areas": canonical_areas,
            "adaptive_mode": bool(adaptive_result),
            "normalized_records": len(records),
            "proposals": len(proposal_files),
            "final_output": str(final_output.resolve()) if final_output else None,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        all_reports.append(report)

    # Write global summary report
    global_workflow = root / "workflow"
    global_workflow.mkdir(parents=True, exist_ok=True)
    try:
        from core.shared_crawl_manager import SharedCrawlManager
        stats = SharedCrawlManager.get().get_stats()
    except Exception:
        stats = {}
    global_report = {
        "run_id": run_id,
        "shared_crawl_stats": stats,
        "status": "completed_with_errors" if global_exit_code > 0 else "completed",
        "valid_calls_found": len(valid_calls),
        "total_proposals": total_proposals,
        "all_final_outputs": all_final_outputs,
        "reports": all_reports,
    }
    (global_workflow / "pipeline_summary.json").write_text(json.dumps(global_report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n==============================================")
    if global_exit_code == 0:
        print("✅ RIF Pipeline Completed Successfully for all valid calls")
    else:
        print(f"⚠️ RIF Pipeline Completed with some errors (Exit code {global_exit_code})")
    print(f"Valid open calls processed: {len(valid_calls)}")
    print(f"Total proposals generated: {total_proposals}")
    print(f"Summary report: {global_workflow / 'pipeline_summary.json'}")
    print("==============================================")
    return global_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
