"""Streamlit UI for the Research Intelligence Framework.

Run with:
    streamlit run ui/streamlit_app.py
"""

from __future__ import annotations

import json
import re
import platform
import sys
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:  # API keys (e.g. OPENALEX_API_KEY) from the project's git-ignored .env
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # python-dotenv is a declared dependency; tolerate absence
    pass
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_registry import AGENT_REGISTRY, run_registered_agent
from core.schemas import ALLOWED_RESEARCH_AREAS
from core.intelligence import refresh_intelligence_views
from core.promotion import clear_promotion_override, load_promotion_overrides, set_promotion_override
from core.source_registry import review_source_candidate

RESEARCH_AREAS = sorted(ALLOWED_RESEARCH_AREAS) + ["None"]
MODES = ["configured_scan", "manual_url", "manual_text"]


def get_project_root() -> Path:
    return PROJECT_ROOT


def load_registered_agents() -> dict[str, dict[str, str]]:
    try:
        return dict(AGENT_REGISTRY)
    except Exception:
        return {}


def get_output_file_counts(outputs_dir: Path) -> list[dict[str, Any]]:
    if not outputs_dir.exists():
        return []

    counts: list[dict[str, Any]] = []
    for layer_dir in sorted(path for path in outputs_dir.iterdir() if path.is_dir()):
        markdown_files = [path for path in layer_dir.rglob("*.md") if path.name.lower() != "insights.md"]
        counts.append({"layer": layer_dir.name, "markdown_files": len(markdown_files)})
    return counts


def find_markdown_files(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(path for path in base_dir.rglob("*.md") if path.is_file())


def find_insights_files(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(path for path in base_dir.rglob("insights.md") if path.is_file())


def get_recent_markdown_files(base_dir: Path, limit: int = 10) -> list[Path]:
    files = [path for path in find_markdown_files(base_dir) if path.name.lower() != "insights.md"]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Selected file is missing."
    except OSError as exc:
        return f"Unable to read file: {exc}"


def read_json_file(path: Path) -> Any | None:
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except ValueError:
        return None


def get_workflow_file_paths() -> dict[str, Path]:
    workflow_dir = get_project_root() / "outputs" / "workflow"
    sources_dir = get_project_root() / "sources"
    return {
        "source_registry": sources_dir / "source_registry.json",
        "source_health_summary": workflow_dir / "source_health_summary.json",
        "source_refresh_plan": workflow_dir / "source_refresh_plan.json",
        "source_discovery_candidates": workflow_dir / "source_discovery_candidates.json",
        "source_review_queue": workflow_dir / "source_review_queue.json",
        "recursive_expansion_review": workflow_dir / "recursive_expansion_review.json",
        "operational_views": workflow_dir / "operational_views.json",
        "batch_run_report": workflow_dir / "batch_run_report.json",
        "batch_run_report_manual": workflow_dir / "batch_run_report_manual.json",
        "full_collection_run_report": workflow_dir / "full_collection_run_report.json",
        "status": workflow_dir / "status.json",
    }


def flatten_dict_rows(mapping: dict[str, Any], *, key_name: str = "name") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            row = {key_name: key}
            row.update(value)
            rows.append(row)
        else:
            rows.append({key_name: key, "value": value})
    return rows


def render_json_expander(label: str, payload: Any) -> None:
    with st.expander(label):
        st.json(payload)


def refresh_repo_views() -> None:
    refresh_intelligence_views(get_project_root() / "outputs")


def render_agent_response(response: dict[str, Any]) -> None:
    status = response.get("status", "error")
    message = f"{status}: {response.get('agent', 'unknown')} ({response.get('layer', 'Unknown')})"
    if status == "success":
        st.success(message)
    elif status == "partial_success":
        st.warning(message)
    else:
        st.error(message)

    metric_columns = st.columns(4)
    metric_columns[0].metric("Items Processed", response.get("items_processed", 0))
    metric_columns[1].metric("Items Saved", response.get("items_saved", 0))
    metric_columns[2].metric("Mode", response.get("mode", "unknown"))
    metric_columns[3].metric("Area", response.get("area") or "None")

    summary_rows = [
        {"field": "agent", "value": response.get("agent")},
        {"field": "layer", "value": response.get("layer")},
        {"field": "status", "value": response.get("status")},
        {"field": "mode", "value": response.get("mode")},
        {"field": "area", "value": response.get("area") or "None"},
    ]
    st.table(summary_rows)

    errors = response.get("errors", [])
    warnings = response.get("warnings", [])
    if errors:
        st.subheader("Errors")
        for item in errors:
            st.error(item)
    if warnings:
        st.subheader("Warnings")
        for item in warnings:
            st.warning(item)

    outputs = response.get("outputs", [])
    st.subheader("Outputs")
    if outputs:
        st.dataframe(outputs)
    else:
        st.info("No outputs returned.")

    with st.expander("Raw Response"):
        st.json(response)


def render_submitted_request(request_payload: dict[str, Any]) -> None:
    st.subheader("Submitted Request")
    st.json(request_payload)


def validate_ui_input(mode: str, input_data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if mode == "manual_url" and not input_data.get("url", "").strip():
        errors.append("Please enter a URL before running the agent.")
    if mode == "manual_text" and not input_data.get("text", "").strip():
        errors.append("Please enter text before running the agent.")
    return errors


def save_ui_result(
    state_prefix: str,
    *,
    agent_name: str,
    mode: str,
    area: str | None,
    input_data: dict[str, Any],
    response: dict[str, Any],
) -> None:
    st.session_state[f"{state_prefix}_request"] = {
        "agent_name": agent_name,
        "mode": mode,
        "area": area,
        "input_data": input_data,
    }
    st.session_state[f"{state_prefix}_response"] = response


def render_saved_result(state_prefix: str) -> None:
    saved_request = st.session_state.get(f"{state_prefix}_request")
    saved_response = st.session_state.get(f"{state_prefix}_response")
    if not saved_request or not saved_response:
        return
    render_submitted_request(saved_request)
    render_agent_response(saved_response)


def normalize_area_selection(selected_area: str) -> str | None:
    return None if selected_area == "None" else selected_area


def build_input_data_for_mode(mode: str, *, title: str = "", source: str = "", url: str = "", text: str = "") -> dict[str, Any]:
    if mode == "manual_url":
        return {"url": url.strip()}
    if mode == "manual_text":
        payload = {"text": text}
        if title.strip():
            payload["title"] = title.strip()
        if source.strip():
            payload["source"] = source.strip()
        return payload
    return {}


def latest_pipeline_runs(limit: int = 10) -> list[Path]:
    root = get_project_root() / "outputs" / "pipeline_run"
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[:limit]


def render_pipeline_result(run_root: Path) -> None:
    workflow = run_root / "workflow"
    manifest = read_json_file(workflow / "pipeline_manifest.json") or {}
    report = read_json_file(workflow / "pipeline_report.json") or {}

    st.subheader(f"Pipeline Run: {run_root.name}")
    if manifest:
        stages = manifest.get("stages", {}) or {}
        counts = manifest.get("counts", {}) or {}
        cols = st.columns(6)
        cols[0].metric("Collected", sum(int(v.get("items_saved", 0) or 0) for v in stages.values() if isinstance(v, dict) and "items_saved" in v))
        cols[1].metric("Normalized", counts.get("normalized_records", 0))
        cols[2].metric("Clusters", counts.get("clusters", 0))
        cols[3].metric("Trends", counts.get("trends", 0))
        cols[4].metric("Ideas", counts.get("ideas", 0))
        cols[5].metric("Proposals", counts.get("proposals", 0))
    domain_rows = []
    for path in [workflow / "pipeline_report.json"]:
        payload = read_json_file(path) or {}
        for item in payload.get("funding_domain_relevance", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict):
                domain_rows.append(item)
    if domain_rows:
        st.subheader("Funding Domain Relevance")
        st.dataframe(domain_rows)

    proposals_dir = run_root / "proposals"
    proposal_files = sorted(proposals_dir.rglob("*.md")) if proposals_dir.exists() else []
    if proposal_files:
        st.subheader(f"Generated Proposals ({len(proposal_files)})")
        selected = st.selectbox("Proposal", proposal_files, format_func=lambda p: p.name, key=f"proposal_{run_root.name}")
        st.caption(str(selected.relative_to(get_project_root())))
        st.markdown(read_text_file(selected))
    if report:
        with st.expander("Pipeline Report JSON"):
            st.json(report)


def render_full_pipeline() -> None:
    st.title("🚀 Live Funding → Research → Proposal")
    st.caption("RIF discovers a currently open funding call first, builds an evidence-backed research mission, runs the collection/processing pipeline, and finishes with proposal artifacts.")

    st.markdown("""
    <style>
    .rif-card {padding:1rem 1.1rem;border:1px solid rgba(128,128,128,.25);border-radius:14px;margin-bottom:.7rem;}
    .rif-kpi {font-size:1.7rem;font-weight:700;}
    .rif-muted {opacity:.72;font-size:.9rem;}
    </style>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.markdown('<div class="rif-card"><div class="rif-kpi">1</div><div>Find an open call</div><div class="rif-muted">Live source scan + deadline checks</div></div>', unsafe_allow_html=True)
    c2.markdown('<div class="rif-card"><div class="rif-kpi">2</div><div>Build research intelligence</div><div class="rif-muted">Evidence, gaps, trends, adaptive discovery</div></div>', unsafe_allow_html=True)
    c3.markdown('<div class="rif-card"><div class="rif-kpi">3</div><div>Generate proposals</div><div class="rif-muted">Final proposal artifacts + provenance</div></div>', unsafe_allow_html=True)

    with st.expander("Pipeline controls", expanded=True):
        area_options = ["Auto-rank from funding evidence"] + sorted(ALLOWED_RESEARCH_AREAS)
        area_choice = st.selectbox("Optional research-area hint", area_options)
        max_calls = st.number_input("Open calls to consider", min_value=1, max_value=5, value=1, step=1)
        source_path = st.text_input("Funding source registry", value="sources/funding_sources.yaml")
        st.info("Leave the research area on auto. The funding call is authoritative; the hint only influences ranking and never turns a closed call into an open one.")

    if st.button("🔎 Discover Open Call & Run Full Pipeline", type="primary", width="stretch"):
        cmd = [sys.executable, str(get_project_root() / "run_pipeline.py"), "--funding-sources", source_path, "--max-open-calls", str(int(max_calls))]
        if area_choice != "Auto-rank from funding evidence":
            cmd.extend(["--area", area_choice])
        with st.status("Running live RIF pipeline…", expanded=True) as status:
            st.write("1/6 — Scanning funding sources and rejecting closed/expired calls.")
            completed = subprocess.run(cmd, cwd=get_project_root(), capture_output=True, text=True)
            if completed.stdout:
                st.code(completed.stdout, language="text")
            if completed.stderr:
                st.text(completed.stderr)
            if completed.returncode == 0:
                status.update(label="Pipeline completed", state="complete")
            else:
                status.update(label=f"Pipeline stopped (exit {completed.returncode})", state="error")
                st.error("The pipeline did not produce a final proposal. Open the latest run below for the exact failure stage.")
        runs = latest_pipeline_runs(1)
        if runs:
            st.session_state["ui_latest_pipeline_run"] = str(runs[0])

    latest = st.session_state.get("ui_latest_pipeline_run")
    if latest and Path(latest).exists():
        render_pipeline_result(Path(latest))
    else:
        st.subheader("Latest live run")
        runs = latest_pipeline_runs(5)
        if runs:
            selected = st.selectbox("Inspect run", runs, format_func=lambda p: p.name)
            render_pipeline_result(selected)
        else:
            st.info("No live pipeline runs yet. Start the discovery pipeline above.")


def render_dashboard() -> None:
    st.title("Research Intelligence Framework")
    st.markdown("A research-intelligence system that turns funding calls into evidence-grounded research missions, gaps, ideas, and proposals.")

    agents = load_registered_agents()
    outputs_dir = get_project_root() / "outputs"

    metrics = st.columns(3)
    metrics[0].metric("Registered Agents", len(agents))
    metrics[1].metric("Research Areas", len(RESEARCH_AREAS) - 1)
    metrics[2].metric("Output Layers", len(get_output_file_counts(outputs_dir)))

    if outputs_dir.exists():
        st.success(f"Outputs folder found at `{outputs_dir}`")
    else:
        st.warning("Outputs folder does not exist yet.")

    if agents:
        st.success("Core agent registry import works.")
    else:
        st.error("Agent registry could not be loaded.")

    st.subheader("Quick Start")
    st.code("python run_pipeline.py", language="powershell")
    st.caption("The default workflow is live: discover an open funding call → collect evidence → synthesize → generate the final proposal. Manual text is an explicit override, not the normal path.")

    st.subheader("Registered Agents")
    if agents:
        agent_rows = [
            {
                "agent": name,
                "display_name": spec.get("display_name", name),
                "layer": spec.get("layer", "Unknown"),
                "module": spec.get("module", ""),
            }
            for name, spec in agents.items()
        ]
        st.table(agent_rows)
    else:
        st.info("No registered agents found.")

    st.subheader("Available Research Areas")
    st.table([{"research_area": area} for area in RESEARCH_AREAS[:-1]])

    st.subheader("Output File Counts")
    counts = get_output_file_counts(outputs_dir)
    if counts:
        st.table(counts)
    else:
        st.info("No output folders available yet.")

    st.subheader("Recent Markdown Files")
    recent_files = get_recent_markdown_files(outputs_dir)
    if recent_files:
        st.table(
            [
                {
                    "file": str(path.relative_to(get_project_root())),
                    "modified": path.stat().st_mtime,
                }
                for path in recent_files
            ]
        )
    else:
        st.info("No markdown files found yet.")


def render_run_agent() -> None:
    st.title("Run Agent")
    st.info("Agents now default to collection-only mode. They save intermediate artifacts for later synthesis instead of doing full in-agent analysis.")
    agents = load_registered_agents()
    if not agents:
        st.error("No registered agents are available.")
        return

    selected_agent = st.selectbox("Agent", list(agents.keys()))
    selected_area = st.selectbox("Research Area", RESEARCH_AREAS)
    selected_mode = st.selectbox("Mode", MODES)

    input_data: dict[str, Any] = {}
    title = ""
    source = ""
    url = ""
    text = ""

    if selected_mode == "manual_url":
        url = st.text_input("URL")
        input_data = build_input_data_for_mode(selected_mode, url=url)
    elif selected_mode == "manual_text":
        title = st.text_input("Title")
        source = st.text_input("Source")
        text = st.text_area("Text", height=240)
        input_data = build_input_data_for_mode(selected_mode, title=title, source=source, text=text)
    else:
        st.info("Configured scan mode uses the agent's configured source file.")

    if st.button("Run Agent", width="stretch"):
        validation_errors = validate_ui_input(selected_mode, input_data)
        if validation_errors:
            for item in validation_errors:
                st.warning(item)
        else:
            normalized_area = normalize_area_selection(selected_area)
            with st.spinner("Running agent..."):
                response = run_registered_agent(
                    agent_name=selected_agent,
                    mode=selected_mode,
                    area=normalized_area,
                    input_data=input_data,
                )
            save_ui_result(
                "run_agent",
                agent_name=selected_agent,
                mode=selected_mode,
                area=normalized_area,
                input_data=input_data,
                response=response,
            )

    render_saved_result("run_agent")


def render_manual_input() -> None:
    st.title("Manual Input")
    st.info("Manual runs also default to collection-only mode and save intermediate artifacts under `outputs/intermediate/`.")
    agents = load_registered_agents()
    if not agents:
        st.error("No registered agents are available.")
        return

    selected_agent = st.selectbox("Agent", list(agents.keys()), key="manual_agent")
    selected_area = st.selectbox("Research Area", RESEARCH_AREAS, key="manual_area")
    input_type = st.radio("Input Type", ["URL", "Raw Text"], horizontal=True)

    title = st.text_input("Optional Title", key="manual_title")
    source = st.text_input("Optional Source", key="manual_source")
    input_data: dict[str, Any]
    mode: str

    if input_type == "URL":
        url = st.text_input("URL", key="manual_url_field")
        mode = "manual_url"
        input_data = build_input_data_for_mode(mode, url=url)
    else:
        text = st.text_area("Raw Text", height=260, key="manual_text_field")
        mode = "manual_text"
        input_data = build_input_data_for_mode(mode, title=title, source=source, text=text)

    if st.button("Process Manual Input", width="stretch"):
        validation_errors = validate_ui_input(mode, input_data)
        if validation_errors:
            for item in validation_errors:
                st.warning(item)
        else:
            normalized_area = normalize_area_selection(selected_area)
            with st.spinner("Processing input..."):
                response = run_registered_agent(
                    agent_name=selected_agent,
                    mode=mode,
                    area=normalized_area,
                    input_data=input_data,
                )
            save_ui_result(
                "manual_input",
                agent_name=selected_agent,
                mode=mode,
                area=normalized_area,
                input_data=input_data,
                response=response,
            )

    render_saved_result("manual_input")


def render_view_outputs() -> None:
    st.title("View Outputs")
    outputs_dir = get_project_root() / "outputs"
    if not outputs_dir.exists():
        st.warning("The outputs folder does not exist yet.")
        return

    layer_dirs = sorted(path for path in outputs_dir.iterdir() if path.is_dir())
    if not layer_dirs:
        st.info("No output subfolders found.")
        return

    selected_layer = st.selectbox("Layer Folder", layer_dirs, format_func=lambda path: path.name)
    markdown_files = find_markdown_files(selected_layer)
    if not markdown_files:
        st.info("No markdown files found in this layer.")
        return

    selected_file = st.selectbox(
        "Markdown File",
        markdown_files,
        format_func=lambda path: str(path.relative_to(get_project_root())),
    )

    if not selected_file.exists():
        st.error("Selected file is missing.")
        return

    st.caption(str(selected_file.relative_to(get_project_root())))
    st.markdown(read_text_file(selected_file))


def render_insights() -> None:
    st.title("Insights")
    outputs_dir = get_project_root() / "outputs"
    if not outputs_dir.exists():
        st.warning("The outputs folder does not exist yet.")
        return

    insight_files = find_insights_files(outputs_dir)
    if not insight_files:
        st.warning("No insights.md files were found.")
        return

    selected_file = st.selectbox(
        "Insights File",
        insight_files,
        format_func=lambda path: str(path.relative_to(get_project_root())),
    )
    if not selected_file.exists():
        st.error("Selected insights file is missing.")
        return

    st.caption(str(selected_file.relative_to(get_project_root())))
    st.markdown(read_text_file(selected_file))


def render_settings() -> None:
    st.title("Settings")
    project_root = get_project_root()
    outputs_dir = project_root / "outputs"
    llm_provider_path = project_root / "core" / "llm_provider.py"
    sources_dir = project_root / "sources"

    st.info("Settings are read-only for now.")

    settings_rows = [
        {"setting": "Project Root", "value": str(project_root)},
        {"setting": "Outputs Path", "value": str(outputs_dir)},
        {"setting": "Sources Path", "value": str(sources_dir)},
        {"setting": "Python Executable", "value": sys.executable},
        {"setting": "Python Version", "value": platform.python_version()},
        {"setting": "Platform", "value": platform.platform()},
        {"setting": "llm_provider.py exists", "value": str(llm_provider_path.exists())},
        {"setting": "outputs/ exists", "value": str(outputs_dir.exists())},
        {"setting": "Registered Agents", "value": str(len(load_registered_agents()))},
    ]
    st.table(settings_rows)

    st.subheader("Available Agents")
    st.json(load_registered_agents())

    st.subheader("Available Research Areas")
    st.json(RESEARCH_AREAS[:-1])


def render_operational_views() -> None:
    st.title("Operational Views")
    st.markdown("Monitor source health, refresh queues, review queues, recursive expansion activity, and batch execution reports.")

    workflow_paths = get_workflow_file_paths()
    loaded = {name: read_json_file(path) for name, path in workflow_paths.items()}

    operational_views = loaded.get("operational_views")
    source_health_summary = loaded.get("source_health_summary")
    source_refresh_plan = loaded.get("source_refresh_plan")
    source_discovery_candidates = loaded.get("source_discovery_candidates")
    source_review_queue = loaded.get("source_review_queue")
    recursive_expansion_review = loaded.get("recursive_expansion_review")
    source_registry = loaded.get("source_registry")

    if not any(loaded.values()):
        st.warning("No workflow or operational JSON artifacts were found yet.")
        return

    top_metrics = st.columns(4)
    top_metrics[0].metric(
        "Sources",
        ((operational_views or {}).get("source_freshness") or {}).get("total_sources")
        or ((source_health_summary or {}).get("counts") or {}).get("total_entries")
        or 0,
    )
    top_metrics[1].metric(
        "Pending Synthesis",
        ((operational_views or {}).get("queues") or {}).get("pending_synthesis_count", 0),
    )
    top_metrics[2].metric(
        "Candidate Sources",
        ((operational_views or {}).get("queues") or {}).get("candidate_source_count")
        or ((source_discovery_candidates or {}).get("stats") or {}).get("total_candidates")
        or 0,
    )
    top_metrics[3].metric(
        "Pending Review",
        ((operational_views or {}).get("queues") or {}).get("pending_review_count")
        or ((source_review_queue or {}).get("stats") or {}).get("pending_entries")
        or 0,
    )

    tabs = st.tabs(
        [
            "Overview",
            "Source Health",
            "Refresh Plan",
            "Discovery",
            "Review Queue",
            "Recursive Review",
            "Batch Reports",
        ]
    )

    with tabs[0]:
        if operational_views:
            freshness = operational_views.get("source_freshness", {})
            queues = operational_views.get("queues", {})
            promotion = operational_views.get("promotion", {})
            failures = operational_views.get("failures", {})
            source_health = operational_views.get("source_health_summary", {})

            metric_row = st.columns(4)
            metric_row[0].metric("Due For Refresh", freshness.get("due_for_refresh_count", 0))
            metric_row[1].metric("Never Checked", freshness.get("never_checked_count", 0))
            metric_row[2].metric("Watchlist", promotion.get("watchlist", 0))
            metric_row[3].metric("Failed Fetches", failures.get("failed_source_fetch_count", 0))

            st.subheader("Queue Summary")
            st.table(
                [
                    {"queue": "Pending Synthesis", "count": queues.get("pending_synthesis_count", 0)},
                    {"queue": "Pending Recursive Expansions", "count": queues.get("pending_recursive_expansions", 0)},
                    {"queue": "Candidate Sources", "count": queues.get("candidate_source_count", 0)},
                    {"queue": "Pending Source Reviews", "count": queues.get("pending_review_count", 0)},
                    {"queue": "Due Source Entries", "count": source_health.get("due_entries", 0)},
                ]
            )

            pending_synthesis_queue = queues.get("pending_synthesis_queue", [])
            if pending_synthesis_queue:
                st.subheader("Pending Synthesis Queue")
                st.dataframe([{"path": item} for item in pending_synthesis_queue[:25]])

            render_json_expander("Raw Operational Views", operational_views)
        else:
            st.info("Operational views are not available yet.")

    with tabs[1]:
        if source_health_summary:
            counts = source_health_summary.get("counts", {})
            averages = source_health_summary.get("averages", {})
            by_agent = source_health_summary.get("by_agent", {})
            attention_queue = source_health_summary.get("attention_queue", [])
            status_counts = source_health_summary.get("status_counts", {})

            metric_row = st.columns(4)
            metric_row[0].metric("Total Entries", counts.get("total_entries", 0))
            metric_row[1].metric("Due Entries", counts.get("due_entries", 0))
            metric_row[2].metric("Avg Trust", averages.get("trust_score", 0))
            metric_row[3].metric("Avg Relevance", averages.get("relevance_score", 0))

            st.subheader("Status Counts")
            st.table([{"status": key, "count": value} for key, value in status_counts.items()])

            if by_agent:
                st.subheader("By Agent")
                st.dataframe(flatten_dict_rows(by_agent, key_name="agent"))

            if attention_queue:
                st.subheader("Attention Queue")
                st.dataframe(attention_queue[:25])

            if source_registry and isinstance(source_registry, dict):
                entries = source_registry.get("entries", [])
                st.subheader("Source Registry Snapshot")
                st.caption(f"{len(entries)} configured source entries currently tracked.")

            render_json_expander("Raw Source Health Summary", source_health_summary)
        else:
            st.info("Source health summary is not available yet.")

    with tabs[2]:
        if source_refresh_plan:
            stats = source_refresh_plan.get("stats", {})
            entries = source_refresh_plan.get("entries", [])

            metric_row = st.columns(3)
            metric_row[0].metric("Total Entries", stats.get("total_entries", 0))
            metric_row[1].metric("Due Entries", stats.get("due_entries", 0))
            metric_row[2].metric("Skipped Entries", stats.get("skipped_entries", 0))

            if entries:
                st.subheader("Refresh Plan Entries")
                st.dataframe(entries[:50])
            else:
                st.info("No refresh plan entries are currently queued.")

            render_json_expander("Raw Source Refresh Plan", source_refresh_plan)
        else:
            st.info("Source refresh plan is not available yet.")

    with tabs[3]:
        if source_discovery_candidates:
            stats = source_discovery_candidates.get("stats", {})
            entries = source_discovery_candidates.get("entries", [])

            metric_row = st.columns(3)
            metric_row[0].metric("Total Candidates", stats.get("total_candidates", 0))
            metric_row[1].metric("New Candidates", stats.get("new_candidates", 0))
            metric_row[2].metric("Existing Candidates", stats.get("existing_candidates", 0))

            if entries:
                st.subheader("Discovery Candidates")
                st.dataframe(entries[:50])
            else:
                st.info("No discovery candidates found.")

            render_json_expander("Raw Discovery Candidates", source_discovery_candidates)
        else:
            st.info("Source discovery candidates are not available yet.")

    with tabs[4]:
        if source_review_queue:
            stats = source_review_queue.get("stats", {})
            entries = source_review_queue.get("entries", [])

            metric_row = st.columns(3)
            metric_row[0].metric("Total Review Entries", stats.get("total_entries", 0))
            metric_row[1].metric("Pending", stats.get("pending_entries", 0))
            metric_row[2].metric("Decided", stats.get("decided_entries", 0))

            if entries:
                st.subheader("Review Queue")
                st.dataframe(entries[:50])
            else:
                st.info("No source review entries are available.")

            render_json_expander("Raw Source Review Queue", source_review_queue)
        else:
            st.info("Source review queue is not available yet.")

    with tabs[5]:
        if recursive_expansion_review:
            stats = recursive_expansion_review.get("stats", {})
            entries = recursive_expansion_review.get("entries", [])

            metric_row = st.columns(5)
            metric_row[0].metric("Total Reviews", stats.get("total_reviews", 0))
            metric_row[1].metric("Saved", stats.get("saved_artifacts", 0))
            metric_row[2].metric("Filtered Out", stats.get("filtered_out", 0))
            metric_row[3].metric("Rejected", stats.get("rejected", 0))
            metric_row[4].metric("Pending", stats.get("pending_artifacts", 0))

            if entries:
                st.subheader("Recursive Expansion Entries")
                st.dataframe(entries[:50])
            else:
                st.info("No recursive expansion review entries are available yet.")

            render_json_expander("Raw Recursive Expansion Review", recursive_expansion_review)
        else:
            st.info("Recursive expansion review is not available yet.")

    with tabs[6]:
        batch_report_names = ["batch_run_report", "batch_run_report_manual", "full_collection_run_report", "status"]
        available_reports = [(name, loaded.get(name)) for name in batch_report_names if loaded.get(name)]

        if not available_reports:
            st.info("No batch or full-run reports are available yet.")
        else:
            for name, report in available_reports:
                st.subheader(name.replace("_", " ").title())
                if isinstance(report, dict):
                    summary_rows = []
                    for key in ("generated_at", "started_at", "finished_at", "status", "planned_tasks", "completed_tasks", "successful_tasks", "failed_tasks"):
                        if key in report:
                            summary_rows.append({"field": key, "value": report.get(key)})
                    if summary_rows:
                        st.table(summary_rows)
                render_json_expander(f"Raw {name}", report)


def render_processing_and_intelligence_views() -> None:
    st.title("Processing And Intelligence")
    st.markdown("Browse normalized records, evidence clusters, duplicate relationships, ranking outputs, and synthesis artifacts.")

    project_root = get_project_root()
    normalized_records_path = project_root / "outputs" / "normalized" / "records.json"
    clusters_path = project_root / "outputs" / "synthesis" / "clusters" / "index.json"
    dedup_path = project_root / "outputs" / "synthesis" / "manifest" / "dedup_report.json"
    ranking_path = project_root / "outputs" / "synthesis" / "manifest" / "ranking.json"
    synthesis_index_path = project_root / "outputs" / "synthesis" / "manifest" / "index.json"
    synthesis_root = project_root / "outputs" / "synthesis"

    normalized_records = read_json_file(normalized_records_path) or []
    clusters = read_json_file(clusters_path) or []
    dedup_report = read_json_file(dedup_path) or {}
    ranking_entries = read_json_file(ranking_path) or []
    synthesis_index = read_json_file(synthesis_index_path) or []

    if not any([normalized_records, clusters, dedup_report, ranking_entries, synthesis_index]):
        st.warning("No processing or intelligence artifacts were found yet.")
        return

    top_metrics = st.columns(5)
    top_metrics[0].metric("Normalized Records", len(normalized_records))
    top_metrics[1].metric("Clusters", len(clusters))
    top_metrics[2].metric("Exact Duplicates", (dedup_report.get("counts") or {}).get("exact_duplicates", 0))
    top_metrics[3].metric("Soft Duplicates", (dedup_report.get("counts") or {}).get("soft_duplicates", 0))
    top_metrics[4].metric("Synthesis Artifacts", len(synthesis_index))

    tabs = st.tabs(
        [
            "Normalized Records",
            "Clusters",
            "Dedup Report",
            "Ranking",
            "Synthesis Artifacts",
        ]
    )

    with tabs[0]:
        if normalized_records:
            available_areas = ["All"] + sorted({item.get("research_area", "Unknown") for item in normalized_records})
            available_layers = ["All"] + sorted({item.get("layer", "Unknown") for item in normalized_records})
            selected_area = st.selectbox("Research Area Filter", available_areas, key="normalized_area_filter")
            selected_layer = st.selectbox("Layer Filter", available_layers, key="normalized_layer_filter")
            search_term = st.text_input("Search Records", key="normalized_search_term").strip().lower()

            filtered_records = normalized_records
            if selected_area != "All":
                filtered_records = [item for item in filtered_records if item.get("research_area") == selected_area]
            if selected_layer != "All":
                filtered_records = [item for item in filtered_records if item.get("layer") == selected_layer]
            if search_term:
                filtered_records = [
                    item
                    for item in filtered_records
                    if search_term in str(item.get("title", "")).lower()
                    or search_term in str(item.get("problem_statement", "")).lower()
                    or search_term in str(item.get("actor", "")).lower()
                ]

            st.caption(f"{len(filtered_records)} record(s) matched the current filters.")
            preview_rows = [
                {
                    "title": item.get("title"),
                    "area": item.get("research_area"),
                    "layer": item.get("layer"),
                    "actor": item.get("actor"),
                    "source_url": item.get("source_url"),
                    "file_path": item.get("file_path"),
                }
                for item in filtered_records
            ]
            if preview_rows:
                st.dataframe(preview_rows)

                selected_title = st.selectbox(
                    "Inspect Record",
                    range(len(filtered_records)),
                    format_func=lambda idx: filtered_records[idx].get("title", f"Record {idx + 1}"),
                    key="normalized_record_selector",
                )
                render_json_expander("Selected Normalized Record", filtered_records[selected_title])
            else:
                st.info("No normalized records matched the current filters.")

            render_json_expander("Raw Normalized Records", normalized_records)
        else:
            st.info("No normalized records are available.")

    with tabs[1]:
        if clusters:
            available_areas = ["All"] + sorted({item.get("area", "Unknown") for item in clusters})
            selected_area = st.selectbox("Cluster Area Filter", available_areas, key="cluster_area_filter")
            filtered_clusters = clusters if selected_area == "All" else [item for item in clusters if item.get("area") == selected_area]

            st.caption(f"{len(filtered_clusters)} cluster(s) matched the current filter.")
            cluster_rows = [
                {
                    "cluster_id": item.get("cluster_id"),
                    "area": item.get("area"),
                    "supporting_layers": ", ".join(item.get("supporting_layers", [])),
                    "member_count": len(item.get("members", [])),
                    "representative_problem": item.get("representative_problem", "")[:160],
                }
                for item in filtered_clusters
            ]
            if cluster_rows:
                st.dataframe(cluster_rows)
                selected_cluster_index = st.selectbox(
                    "Inspect Cluster",
                    range(len(filtered_clusters)),
                    format_func=lambda idx: filtered_clusters[idx].get("cluster_id", f"Cluster {idx + 1}"),
                    key="cluster_selector",
                )
                selected_cluster = filtered_clusters[selected_cluster_index]
                if selected_cluster.get("members"):
                    st.subheader("Cluster Members")
                    st.dataframe(selected_cluster.get("members"))
                render_json_expander("Selected Cluster", selected_cluster)
            else:
                st.info("No clusters matched the current filter.")

            render_json_expander("Raw Clusters", clusters)
        else:
            st.info("No cluster outputs are available.")

    with tabs[2]:
        if dedup_report:
            counts = dedup_report.get("counts", {})
            metric_row = st.columns(3)
            metric_row[0].metric("Exact Duplicates", counts.get("exact_duplicates", 0))
            metric_row[1].metric("Soft Duplicates", counts.get("soft_duplicates", 0))
            metric_row[2].metric("Corroborating Evidence", counts.get("corroborating_evidence", 0))

            relation_type = st.selectbox(
                "Relation Type",
                ["Exact Duplicates", "Soft Duplicates", "Corroborating Evidence"],
                key="dedup_relation_selector",
            )
            relation_key_map = {
                "Exact Duplicates": "exact_duplicates",
                "Soft Duplicates": "soft_duplicates",
                "Corroborating Evidence": "corroborating_evidence",
            }
            selected_relations = dedup_report.get(relation_key_map[relation_type], [])
            if selected_relations:
                rows = [
                    {
                        "reason": item.get("reason"),
                        "similarity": item.get("similarity"),
                        "left_title": (item.get("left") or {}).get("title"),
                        "right_title": (item.get("right") or {}).get("title"),
                        "area": (item.get("left") or {}).get("research_area"),
                    }
                    for item in selected_relations
                ]
                st.dataframe(rows)
                selected_relation_index = st.selectbox(
                    "Inspect Relation",
                    range(len(selected_relations)),
                    format_func=lambda idx: f"{rows[idx]['left_title']} ↔ {rows[idx]['right_title']}",
                    key="dedup_relation_detail_selector",
                )
                render_json_expander("Selected Dedup Relation", selected_relations[selected_relation_index])
            else:
                st.info("No entries for the selected relation type.")

            render_json_expander("Raw Dedup Report", dedup_report)
        else:
            st.info("No dedup report is available.")

    with tabs[3]:
        if ranking_entries:
            available_areas = ["All"] + sorted({item.get("area", "Unknown") for item in ranking_entries})
            selected_area = st.selectbox("Ranking Area Filter", available_areas, key="ranking_area_filter")
            filtered_entries = ranking_entries if selected_area == "All" else [item for item in ranking_entries if item.get("area") == selected_area]
            filtered_entries = sorted(
                filtered_entries,
                key=lambda item: ((item.get("ranking") or {}).get("score", 0)),
                reverse=True,
            )

            rows = [
                {
                    "title": item.get("title"),
                    "area": item.get("area"),
                    "score": (item.get("ranking") or {}).get("score"),
                    "label": (item.get("ranking") or {}).get("label"),
                    "priority_band": (item.get("ranking") or {}).get("priority_band"),
                    "promotion_status": (item.get("promotion") or {}).get("status"),
                }
                for item in filtered_entries
            ]
            if rows:
                st.dataframe(rows)
                selected_ranking_index = st.selectbox(
                    "Inspect Ranked Entry",
                    range(len(filtered_entries)),
                    format_func=lambda idx: filtered_entries[idx].get("title", f"Entry {idx + 1}"),
                    key="ranking_entry_selector",
                )
                render_json_expander("Selected Ranking Entry", filtered_entries[selected_ranking_index])
            else:
                st.info("No ranking entries matched the current filter.")

            render_json_expander("Raw Ranking Entries", ranking_entries)
        else:
            st.info("No ranking outputs are available.")

    with tabs[4]:
        synthesis_files = find_markdown_files(synthesis_root)
        if synthesis_index:
            st.subheader("Synthesis Index")
            index_rows = [
                {
                    "title": item.get("title"),
                    "area": item.get("area"),
                    "layers": ", ".join(item.get("layers_covered", [])),
                    "method": item.get("synthesis_method"),
                    "status": item.get("status"),
                    "promotion_status": (item.get("promotion") or {}).get("status"),
                    "synthesized_file": item.get("synthesized_file"),
                }
                for item in synthesis_index
            ]
            st.dataframe(index_rows)
            selected_synthesis_index = st.selectbox(
                "Inspect Synthesis Manifest Entry",
                range(len(synthesis_index)),
                format_func=lambda idx: synthesis_index[idx].get("title", f"Synthesis {idx + 1}"),
                key="synthesis_manifest_selector",
            )
            render_json_expander("Selected Synthesis Manifest Entry", synthesis_index[selected_synthesis_index])

        if synthesis_files:
            st.subheader("Synthesis Markdown Viewer")
            selected_file = st.selectbox(
                "Synthesis File",
                synthesis_files,
                format_func=lambda path: str(path.relative_to(project_root)),
                key="synthesis_markdown_selector",
            )
            st.caption(str(selected_file.relative_to(project_root)))
            st.markdown(read_text_file(selected_file))
        else:
            st.info("No synthesis markdown artifacts were found.")

        if synthesis_index:
            render_json_expander("Raw Synthesis Index", synthesis_index)


def render_review_and_promotion() -> None:
    st.title("Review And Promotion")
    st.markdown("Apply source-review decisions and set durable promotion overrides for synthesized problems.")

    project_root = get_project_root()
    review_queue_path = project_root / "outputs" / "workflow" / "source_review_queue.json"
    synthesis_index_path = project_root / "outputs" / "synthesis" / "manifest" / "index.json"

    review_queue = read_json_file(review_queue_path) or {}
    synthesis_index = read_json_file(synthesis_index_path) or []
    promotion_overrides = load_promotion_overrides()

    tabs = st.tabs(["Source Review", "Promotion Control"])

    with tabs[0]:
        entries = [item for item in review_queue.get("entries", []) if isinstance(item, dict)]
        pending_entries = [item for item in entries if str(item.get("review_status", "")).strip().lower() == "pending"]

        if not entries:
            st.info("No source review entries are available yet.")
        else:
            metric_row = st.columns(3)
            stats = review_queue.get("stats", {})
            metric_row[0].metric("Total Entries", stats.get("total_entries", len(entries)))
            metric_row[1].metric("Pending", stats.get("pending_entries", len(pending_entries)))
            metric_row[2].metric("Decided", stats.get("decided_entries", max(len(entries) - len(pending_entries), 0)))

            review_scope = st.radio("Review Scope", ["Pending Only", "All"], horizontal=True, key="review_scope")
            selectable_entries = pending_entries if review_scope == "Pending Only" else entries

            if selectable_entries:
                selected_index = st.selectbox(
                    "Select Review Entry",
                    range(len(selectable_entries)),
                    format_func=lambda idx: f"{selectable_entries[idx].get('agent_name', 'unknown')} | {selectable_entries[idx].get('name', 'Untitled')}",
                    key="review_entry_selector",
                )
                selected_entry = selectable_entries[selected_index]

                st.subheader("Selected Review Entry")
                st.table(
                    [
                        {"field": "review_id", "value": selected_entry.get("review_id")},
                        {"field": "agent_name", "value": selected_entry.get("agent_name")},
                        {"field": "name", "value": selected_entry.get("name")},
                        {"field": "url", "value": selected_entry.get("url")},
                        {"field": "review_status", "value": selected_entry.get("review_status")},
                        {"field": "registry_status", "value": selected_entry.get("registry_status")},
                        {"field": "area_hints", "value": ", ".join(selected_entry.get("area_hints", []))},
                    ]
                )
                render_json_expander("Selected Source Review Entry", selected_entry)

                action = st.selectbox("Review Action", ["approve", "reject", "pause", "rescore"], key="review_action")
                rationale = st.text_area("Rationale", key="review_rationale", height=120)
                activate = st.checkbox("Activate Immediately", value=True, key="review_activate") if action == "approve" else False
                trust_score = None
                relevance_score = None
                if action == "rescore":
                    trust_score = st.slider("Trust Score", min_value=0.0, max_value=1.0, value=float(selected_entry.get("trust_score", 0.7) or 0.7), step=0.05, key="review_trust")
                    relevance_score = st.slider("Relevance Score", min_value=0.0, max_value=1.0, value=float(selected_entry.get("relevance_score", 0.7) or 0.7), step=0.05, key="review_relevance")

                if st.button("Apply Source Review Action", width="stretch"):
                    if not rationale.strip():
                        st.warning("Please enter a rationale before applying a review action.")
                    else:
                        try:
                            result = review_source_candidate(
                                review_id=str(selected_entry.get("review_id", "")).strip(),
                                action=action,
                                rationale=rationale.strip(),
                                activate=bool(activate),
                                trust_score=trust_score,
                                relevance_score=relevance_score,
                            )
                            refresh_repo_views()
                            st.success(f"Applied `{action}` to review entry `{result.review_id}`.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Unable to apply review action: {exc}")
            else:
                st.info("No review entries matched the selected scope.")

    with tabs[1]:
        override_entries = [item for item in promotion_overrides.get("entries", []) if isinstance(item, dict)]
        metric_row = st.columns(2)
        metric_row[0].metric("Synthesis Entries", len(synthesis_index))
        metric_row[1].metric("Promotion Overrides", len(override_entries))

        if not synthesis_index:
            st.info("No synthesis entries are available yet.")
        else:
            area_options = ["All"] + sorted({item.get("area", "Unknown") for item in synthesis_index})
            selected_area = st.selectbox("Area Filter", area_options, key="promotion_area_filter")
            filtered_entries = synthesis_index if selected_area == "All" else [item for item in synthesis_index if item.get("area") == selected_area]
            filtered_entries = sorted(filtered_entries, key=lambda item: str(item.get("title", "")))

            selected_index = st.selectbox(
                "Select Synthesis Entry",
                range(len(filtered_entries)),
                format_func=lambda idx: filtered_entries[idx].get("title", f"Synthesis {idx + 1}"),
                key="promotion_entry_selector",
            )
            selected_entry = filtered_entries[selected_index]
            synthesized_file = str(selected_entry.get("synthesized_file", "")).strip()
            current_override = next(
                (item for item in override_entries if str(item.get("synthesized_file", "")).strip() == synthesized_file),
                None,
            )

            st.subheader("Selected Synthesis Entry")
            st.table(
                [
                    {"field": "title", "value": selected_entry.get("title")},
                    {"field": "area", "value": selected_entry.get("area")},
                    {"field": "promotion_status", "value": (selected_entry.get("promotion") or {}).get("status")},
                    {"field": "priority_label", "value": (selected_entry.get("promotion") or {}).get("priority_label")},
                    {"field": "ranking_score", "value": (selected_entry.get("ranking") or {}).get("score")},
                    {"field": "manual_override", "value": (selected_entry.get("promotion") or {}).get("manual_override", False)},
                ]
            )
            if synthesized_file:
                st.caption(synthesized_file)
            render_json_expander("Selected Synthesis Entry", selected_entry)

            promotion_status = st.selectbox(
                "Promotion Status",
                ["watchlist", "active_research_candidate", "high-priority_proposal_candidate", "hold"],
                index=["watchlist", "active_research_candidate", "high-priority_proposal_candidate", "hold"].index(
                    str((current_override or {}).get("status", (selected_entry.get("promotion") or {}).get("status", "watchlist")))
                ),
                key="promotion_status_selector",
            )
            promotion_rationale = st.text_area(
                "Promotion Rationale",
                value=str((current_override or {}).get("rationale", "")),
                key="promotion_rationale",
                height=120,
            )

            action_columns = st.columns(2)
            if action_columns[0].button("Save Promotion Override", width="stretch"):
                try:
                    set_promotion_override(
                        synthesized_file=synthesized_file,
                        status=promotion_status,
                        rationale=promotion_rationale.strip(),
                    )
                    refresh_repo_views()
                    st.success(f"Saved promotion override for `{selected_entry.get('title', 'entry')}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to save promotion override: {exc}")

            if action_columns[1].button("Clear Promotion Override", width="stretch"):
                try:
                    clear_promotion_override(synthesized_file=synthesized_file)
                    refresh_repo_views()
                    st.success(f"Cleared promotion override for `{selected_entry.get('title', 'entry')}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear promotion override: {exc}")

            if override_entries:
                st.subheader("Existing Promotion Overrides")
                st.dataframe(override_entries)
                render_json_expander("Raw Promotion Overrides", promotion_overrides)



_CARD_CSS = """
<style>
.rif-chips {display:flex; gap:.4rem; flex-wrap:wrap; margin-bottom:.35rem;}
.rif-chip {font-size:.72rem; font-weight:600; padding:.12rem .55rem; border-radius:999px;
           border:1px solid rgba(128,128,128,.35); opacity:.9;}
.rif-chip.rif-soon {border-color:#d9534f; color:#d9534f;}
.rif-chip.rif-ok {border-color:#2e9d6b; color:#2e9d6b;}
.rif-title {font-size:1.05rem; font-weight:650; line-height:1.35; margin:.1rem 0 .2rem 0;
             display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
             min-height:calc(1.05rem * 1.35 * 3);}
.rif-sub {font-size:.8rem; opacity:.65; margin-bottom:.5rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.rif-row {font-size:.86rem; margin:.15rem 0;}
.rif-problem {font-size:.84rem; line-height:1.5; opacity:.85; border-left:3px solid rgba(128,128,128,.35);
              padding-left:.6rem; margin:.55rem 0 .4rem 0;
              display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden;
              min-height:calc(.84rem * 1.5 * 4);}
.rif-stats {display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:.8rem; margin:.4rem 0 1rem 0;}
.rif-stat {border:1px solid rgba(128,128,128,.25); border-radius:.6rem; padding:.7rem .9rem;}
.rif-stat-label {font-size:.75rem; opacity:.65; text-transform:uppercase; letter-spacing:.03em;}
.rif-stat-value {font-size:1.45rem; font-weight:650; margin-top:.15rem; white-space:nowrap;}
.rif-stat-sub {font-size:.78rem; opacity:.65; min-height:1rem;}
@media (max-width: 800px) {.rif-stats {grid-template-columns:repeat(2, minmax(0, 1fr));}}
</style>
"""

_REASON_TEXT = (
    ("research intent", "Not research funding (innovation, coordination or procurement action)"),
    ("source-grounded research problem", "No research problem stated in the official text"),
    ("research-context confidence", "No research problem stated in the official text"),
    ("domain-specific evidence", "Outside the 7 research domains"),
    ("requires a direct", "Outside the 7 research domains"),
    ("eligibility", "Eligibility not stated on the source"),
    ("verified individual call", "Could not be verified as an individual open call"),
    ("currently open", "Could not be verified as open"),
)


def _plain_reason(reasons: list[str]) -> str:
    text = " ".join(str(r) for r in reasons or []).casefold()
    for needle, label in _REASON_TEXT:
        if needle in text:
            return label
    return str((reasons or ["Did not qualify"])[0])


def _days_left(deadline: str | None) -> int | None:
    from datetime import date

    try:
        return (date.fromisoformat(str(deadline)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


def collect_open_calls(run_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Qualified open calls (one per call folder) and the other open calls with reasons."""
    qualified: list[dict[str, Any]] = []
    others: dict[str, dict[str, Any]] = {}
    for call_dir in sorted((run_root / "calls").glob("*")):
        payload = read_json_file(call_dir / "workflow" / "selected_funding_call.json") or {}
        selected = payload.get("selected") if isinstance(payload, dict) else None
        if isinstance(selected, dict):
            qualified.append(selected)
        for item in (payload.get("rejected_alternatives") or []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("status") == "OPEN_CALL":
                others.setdefault(str(item.get("title")), item)
    # The run summary keeps two more groups of rejected open calls.
    summary = read_json_file(run_root / "workflow" / "pipeline_summary.json") or {}
    for key, fallback in (("rejected_non_research", ["research intent is not verified as research"]),
                          ("rejected_unverified_research_context", ["missing source-grounded research problem signal"])):
        for entry in (summary.get(key) or []) if isinstance(summary, dict) else []:
            call = entry.get("call") if isinstance(entry, dict) else None
            if isinstance(call, dict) and call.get("status") == "OPEN_CALL":
                others.setdefault(str(call.get("title")), {
                    "title": call.get("title"), "status": call.get("status"), "source_url": call.get("source_url"),
                    "reasons": entry.get("reasons") or fallback,
                })
    qualified_titles = {str(c.get("title")) for c in qualified}
    qualified.sort(key=lambda c: str(c.get("deadline") or "9999"))
    rest = [o for t, o in others.items() if t not in qualified_titles]
    rest.sort(key=lambda o: str(o.get("title") or ""))
    return qualified, rest


_ELIGIBILITY_HEADINGS = (
    (r"Topic-specific eligibility conditions:\s*", "Topic-specific conditions"),
    (r"Annex B of the Work Programme General Annexes \(cited for eligibility by the topic conditions\):\s*",
     "General eligibility (Annex B)"),
)


_ABBREVIATIONS = {"st", "no", "nr", "art", "e.g", "i.e", "etc", "vs", "mr", "dr", "co", "ltd", "inc"}


def _sentence_breaks(text: str):
    """Positions where a sentence ends (". X" / "; X"), ignoring abbreviations like "St."."""
    import re

    for match in re.finditer(r"[.;](\s+)(?=[A-Z])", text):
        word = re.search(r"([A-Za-z.]+)$", text[:match.start()])
        if match.group(0)[0] == "." and word and (word.group(1).casefold() in _ABBREVIATIONS or len(word.group(1)) == 1):
            continue
        yield match


def _split_sentences(text: str) -> list[str]:
    text = text.strip()
    parts, start = [], 0
    for match in _sentence_breaks(text):
        parts.append(text[start:match.start() + 1])
        start = match.end()
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def eligibility_bullets(text: str) -> list[tuple[str | None, list[tuple[str, list[str]]]]]:
    """Structure official eligibility text into (heading, [(bullet, [sub-bullets])]).

    Wording is kept verbatim; only layout changes: known section labels become
    headings, sentences become bullets and "–"-introduced list items sub-bullets.
    """
    import re

    pattern = "|".join(f"(?:{p})" for p, _ in _ELIGIBILITY_HEADINGS)
    sections: list[tuple[str | None, str]] = []
    position, heading = 0, None
    for match in re.finditer(pattern, text or ""):
        if text[position:match.start()].strip():
            sections.append((heading, text[position:match.start()]))
        heading = next(label for p, label in _ELIGIBILITY_HEADINGS if re.fullmatch(p, match.group(0)))
        position = match.end()
    if (text or "")[position:].strip():
        sections.append((heading, text[position:]))

    structured = []
    for heading, body in sections:
        pieces = re.split(r"\s+[–—]\s+", body.strip())
        bullets: list[tuple[str, list[str]]] = [(s, []) for s in _split_sentences(pieces[0])]
        for item in pieces[1:]:
            end = next((m for m in _sentence_breaks(item) if m.group(0)[0] == "."), None)
            sub, rest = (item[:end.start() + 1], item[end.end():]) if end else (item, "")
            if not bullets:
                bullets.append(("", []))
            bullets[-1][1].append(sub.strip())
            bullets.extend((s, []) for s in _split_sentences(rest))
        structured.append((heading, bullets))
    return structured


def eligibility_markdown(text: str) -> str:
    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")

    lines: list[str] = []
    for heading, bullets in eligibility_bullets(text):
        if heading:
            lines.append(f"**{heading}**")
        for bullet, subs in bullets:
            if bullet:
                lines.append(f"- {esc(bullet)}")
            for sub in subs:
                lines.append(f"    - {esc(sub)}")
        lines.append("")
    return "\n".join(lines).strip()


def pretty_date(value: str | None) -> str:
    from datetime import date

    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y").lstrip("0")
    except (TypeError, ValueError):
        return str(value or "not stated")


def _money(amount: str) -> str:
    number = float(amount.replace(",", ""))
    if number >= 1_000_000:
        text = f"{number / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}M"
    if number >= 1_000:
        return f"{number / 1_000:.0f}K"
    return f"{number:.0f}"


def pretty_budget(text: str | None) -> str:
    """Compact EU budget line, e.g. "€60M total · €10M per project · 6 grants"; raw text otherwise."""
    import re

    raw = str(text or "").strip()
    if not raw:
        return "not stated"
    parts = []
    symbol = "€" if "EUR" in raw else ""
    total = re.search(r"topic budget [A-Z]{3}\s*([\d,]+)", raw)
    per = re.search(r"per project [A-Z]{3}\s*([\d,]+)(?:\s*-\s*([\d,]+))?", raw)
    grants = re.search(r"expected grants\s*(\d+)", raw)
    if total:
        parts.append(f"{symbol}{_money(total.group(1))} total")
    if per:
        low = _money(per.group(1))
        parts.append(f"{symbol}{low}–{_money(per.group(2))} per project" if per.group(2) else f"{symbol}{low} per project")
    if grants:
        parts.append(f"{grants.group(1)} grants")
    return " · ".join(parts) if parts else raw


def pretty_scan_time(run_name: str) -> str:
    from datetime import datetime

    try:
        return datetime.strptime(run_name[:15], "%Y%m%d-%H%M%S").strftime("%d %b %Y, %H:%M").lstrip("0")
    except ValueError:
        return run_name


def render_call_card(call: dict[str, Any]) -> None:
    import html

    rc = call.get("research_context") if isinstance(call.get("research_context"), dict) else {}
    days = _days_left(call.get("deadline"))
    chips = [f'<span class="rif-chip">{html.escape(str(call.get("research_area") or call.get("focus_area") or "Unscoped"))}</span>']
    if days is not None:
        cls = "rif-soon" if days <= 30 else "rif-ok"
        chips.append(f'<span class="rif-chip {cls}">{days} days left</span>')
    problem = (rc.get("inferred_challenges") or [None])[0]
    body = (
        f'<div class="rif-chips">{"".join(chips)}</div>'
        f'<div class="rif-title" title="{html.escape(str(call.get("title") or ""))}">{html.escape(str(call.get("title") or "Untitled call"))}</div>'
        f'<div class="rif-sub">{html.escape(str(call.get("call_id") or ""))} · {html.escape(str(call.get("funding_body") or ""))}</div>'
        f'<div class="rif-row"><b>Deadline</b> {html.escape(pretty_date(call.get("deadline")))}</div>'
        f'<div class="rif-row" title="{html.escape(str(call.get("funding_amount") or ""))}"><b>Budget</b> '
        f'{html.escape(pretty_budget(call.get("funding_amount")))}</div>'
    )
    # Always reserve the problem block so cards in a row line up.
    body += (f'<div class="rif-problem" title="{html.escape(str(problem))}">{html.escape(str(problem))}</div>'
             if problem else '<div class="rif-problem">No problem statement extracted.</div>')
    with st.container(border=True):
        st.markdown(body, unsafe_allow_html=True)
        if call.get("eligibility"):
            with st.expander("Eligibility"):
                st.markdown(eligibility_markdown(str(call["eligibility"])))
        links = st.columns(2)
        if call.get("application_url"):
            links[0].link_button("Apply", str(call["application_url"]), width="stretch")
        if call.get("source_url"):
            links[1].link_button("Official page", str(call["source_url"]), width="stretch")


def render_open_calls() -> None:
    st.markdown(_CARD_CSS, unsafe_allow_html=True)
    st.title("Open calls")
    st.caption("Live funding calls that fit your research domains. Light / dark: ⋮ menu (top right).")
    runs = latest_pipeline_runs(1)
    if not runs:
        st.info("No scan yet. Open **Run pipeline** in the sidebar to scan live funding sources.")
        return
    run_root = runs[0]
    qualified, others = collect_open_calls(run_root)

    import html

    earliest = next((c for c in qualified if _days_left(c.get("deadline")) is not None), None)
    scan = pretty_scan_time(run_root.name)
    scan_day, _, scan_hour = scan.partition(", ")
    stats = [
        ("Qualified calls", str(len(qualified)), "open · research · in your domains"),
        ("Earliest deadline", pretty_date(earliest.get("deadline")) if earliest else "—",
         f"{_days_left(earliest.get('deadline'))} days left" if earliest else ""),
        ("Other open calls", str(len(others)), "did not qualify"),
        ("Last scan", scan_day, scan_hour),
    ]
    st.markdown(
        '<div class="rif-stats">' + "".join(
            f'<div class="rif-stat"><div class="rif-stat-label">{html.escape(label)}</div>'
            f'<div class="rif-stat-value">{html.escape(value)}</div>'
            f'<div class="rif-stat-sub">{html.escape(sub)}</div></div>'
            for label, value, sub in stats
        ) + "</div>",
        unsafe_allow_html=True,
    )

    query = st.text_input("Search", placeholder="Search by title, ID or topic", label_visibility="collapsed")
    if query.strip():
        needle = query.strip().casefold()
        qualified = [
            c for c in qualified
            if needle in " ".join(str(c.get(k) or "") for k in ("title", "call_id", "research_area", "funding_body")).casefold()
            or needle in " ".join((c.get("research_context") or {}).get("inferred_challenges") or []).casefold()
        ]

    if not qualified:
        st.info("No qualified open call matches." if query.strip() else "No open call qualified in the latest scan.")
    # Row by row, so the two cards of each row start and line up together.
    for row_start in range(0, len(qualified), 2):
        columns = st.columns(2, gap="medium")
        for column, call in zip(columns, qualified[row_start:row_start + 2]):
            with column:
                render_call_card(call)

    if others:
        st.write("")
        with st.expander(f"Other open calls ({len(others)}) — did not qualify"):
            groups: dict[str, list[dict[str, Any]]] = {}
            for item in others:
                groups.setdefault(_plain_reason(item.get("reasons") or []), []).append(item)
            for reason, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                st.markdown(f"**{reason}** · {len(items)}")
                st.markdown("\n".join(
                    f"- [{str(i.get('title') or 'Untitled')}]({i['source_url']})" if i.get("source_url")
                    else f"- {str(i.get('title') or 'Untitled')}"
                    for i in items
                ))
    st.caption("Qualified = open, research funding, in the 7 domains, with a stated problem and eligibility.")


_ADVANCED_PAGES = {
    "Dashboard": "render_dashboard",
    "Run Agent": "render_run_agent",
    "Manual Input": "render_manual_input",
    "View Outputs": "render_view_outputs",
    "Insights": "render_insights",
    "Operational Views": "render_operational_views",
    "Processing And Intelligence": "render_processing_and_intelligence_views",
    "Review And Promotion": "render_review_and_promotion",
    "Settings": "render_settings",
}


def render_advanced() -> None:
    st.title("Advanced")
    choice = st.selectbox("Tool", list(_ADVANCED_PAGES))
    globals()[_ADVANCED_PAGES[choice]]()


# Session keys saved to disk so a browser reload keeps the user's progress.
_PERSIST_KEYS = ("as_candidates", "as_query", "as_pick", "as_inspection", "as_trace",
                 "as_scan", "as_review", "as_done", "as_form_query", "as_form_agents", "as_form_domains", "as_form_focus",
                 "as_last_added", "as_test_results")
_PERSIST_PREFIXES = ("as_sel_", "as_agents_", "as_doms_", "as_one_")
_FLOW_KEYS = ("as_candidates", "as_pick", "as_inspection", "as_query", "as_done", "as_trace",
              "as_scan", "as_review", "as_last_added", "as_test_results", "as_confirm_remove")
_GITHUB_REPO = re.compile(r"^https://github\.com/[^/]+/[^/#?]+/?$")


def _url_id(url: str) -> str:
    import hashlib

    return hashlib.sha1(url.rstrip("/").lower().encode("utf-8")).hexdigest()[:12]


def _persist_add_source() -> None:
    from core import add_source_state

    state = st.session_state
    values = {k: state[k] for k in _PERSIST_KEYS if k in state}
    values.update({k: state[k] for k in list(state.keys()) if str(k).startswith(_PERSIST_PREFIXES)})
    try:
        add_source_state.save_state(values)
    except Exception as exc:  # never break the page because the state file cannot be written
        st.caption(f"Progress could not be saved ({type(exc).__name__}).")


def _restore_add_source() -> None:
    from core import add_source_state

    state = st.session_state
    if state.get("as_restored"):
        return
    saved = add_source_state.load_state()
    if isinstance(saved.get("as_pick"), dict) and "agents" not in saved["as_pick"]:
        saved = {}  # progress saved by an older version of this page
    for key, value in saved.items():
        if key in _PERSIST_KEYS or key.startswith(_PERSIST_PREFIXES):
            state[key] = value
    state["as_restored"] = True


def _rerun() -> None:
    _persist_add_source()
    st.rerun()


def _reset_add_source() -> None:
    from core import add_source_state

    for key in _FLOW_KEYS:
        st.session_state.pop(key, None)
    for key in [k for k in st.session_state if str(k).startswith(_PERSIST_PREFIXES)]:
        st.session_state.pop(key, None)
    add_source_state.clear_state()


def _evidence_text(fit: Any, limit: int = 5) -> str:
    return ", ".join(fit.evidence[:limit]) or "no evidence"


def _domains_text(detected: list[Any]) -> str:
    return "; ".join(f"{domain} ({', '.join(terms[:3])})" for domain, terms in detected) or "none found"


def _agent_issue(agent: str, url: str, configured: dict[str, list[str]]) -> str:
    from core import user_sources as us

    if any(d.startswith(us.AGENT_LABELS[agent] + ": ") for d in configured.get(us._url_key(url), [])):
        return "already configured for this agent"
    if agent == "opensource" and not _GITHUB_REPO.match(url):
        return "the Open source agent needs a GitHub repository URL"
    return ""


def _inspect_into_state(url: str, agents: list[str], domains: list[str]) -> None:
    from core import user_sources as us

    st.session_state["as_inspection"] = us.inspect_url(url, agents[0] if len(agents) == 1 else None,
                                                       domains=domains or None)
    st.session_state.pop("as_trace", None)


def _add_items(items: list[dict[str, Any]], focus: str) -> None:
    """Add approved (page, agent, domains) items, then report per item."""
    from core import user_sources as us

    state = st.session_state
    results, added = [], []
    for item in items:
        name = (item.get("name") or item.get("title") or urlsplit(item["url"]).hostname or item["url"])[:120]
        try:
            us.add_user_source(item["agent"], name, item["url"], item["domains"], focus)
            us.sync_registry_for(item["agent"], item["url"])
            results.append(f"Added \"{name}\" to the {us.AGENT_LABELS[item['agent']]} agent ({', '.join(item['domains'])})")
            added.append({"agent": item["agent"], "url": item["url"], "name": name})
        except us.UserSourceError as exc:
            results.append(f"Not added to {us.AGENT_LABELS[item['agent']]}: {exc}")
    _reset_add_source()
    state["as_done"] = ". ".join(results) + (". Added sources are collected on every run, like the built-in sources."
                                             if added else ". Nothing was added.")
    state["as_last_added"] = added
    _rerun()


def _run_site_scan(url: str) -> None:
    from core import add_source_state
    from core.agent_fit import scan_site

    discarded = add_source_state.load_discarded()
    with st.spinner("Reading this page and the pages it links to, for every agent..."):
        given, found, notes = scan_site(url, skip=lambda link: add_source_state.is_discarded(link, discarded))
    # Unnecessary pages (fit no agent) are dropped and remembered, so they are not fetched again.
    unfit = [f.url for f in found if not f.agent]
    if unfit:
        add_source_state.discard(unfit, "fits no agent")
        notes.append(f"{len(unfit)} linked page(s) fit no agent and were discarded.")
    st.session_state["as_scan"] = {"url": url, "given": given, "found": [f for f in found if f.agent], "notes": notes}


def _render_site_scan(url: str, pick: dict[str, Any], auto: bool) -> None:
    """Scan a page and its linked pages for every agent; add only what the user approves."""
    from core import add_source_state
    from core import user_sources as us

    state = st.session_state
    st.markdown("#### Pages on this site")
    scan = state.get("as_scan")
    if scan is None or scan["url"] != url:
        if auto:
            _run_site_scan(url)
            scan = state["as_scan"]
        else:
            st.caption("Check this page and the pages it links to for every agent (nothing is added without your approval).")
            if st.button("Scan this site for all agents"):
                _run_site_scan(url)
                _rerun()
            return

    given, notes = scan["given"], scan["notes"]
    if given is None:
        st.warning(" ".join(notes) or "The page could not be read.")
        return
    if given.agent:
        also = [us.AGENT_LABELS[a] for a in given.alternatives]
        st.markdown(f"**The URL you gave** fits **{us.AGENT_LABELS[given.agent]}** (evidence: {', '.join(given.evidence[:4])})"
                    + (f" and also fits: **{', '.join(also)}**." if also else "."))
    else:
        st.markdown("**The URL you gave fits no agent.**")
    discarded = add_source_state.load_discarded()
    useful = [f for f in scan["found"] if not add_source_state.is_discarded(f.url, discarded)]
    if useful:
        st.markdown(f"**Inside it, {len(useful)} page(s) fit an agent.** Tick the ones to add. The agents and research "
                    "domains are the app's suggestions from each page's text; you can change both, or discard a page.")
    else:
        st.info("No linked page fits an agent.")
    agents = list(us.AGENT_FILES)
    configured = us.configured_urls()
    for index, finding in enumerate(useful, start=1):
        uid = _url_id(finding.url)
        page_domains = [d for d, _terms in (finding.domains or [])]
        state.setdefault(f"as_sel_{uid}", False)
        state.setdefault(f"as_agents_{uid}", [finding.agent])
        state.setdefault(f"as_doms_{uid}", list(pick["domains"]) or page_domains)
        already = configured.get(us._url_key(finding.url), [])
        with st.container(border=True):
            c1, c2 = st.columns([5, 1.3])
            c1.checkbox(f"Page #{index}: {finding.title or finding.url}", key=f"as_sel_{uid}")
            c1.caption(f"{finding.url}  \nAgent evidence: {', '.join(finding.evidence[:5])}"
                       + (f"  \nAlso fits: {', '.join(us.AGENT_LABELS[a] for a in finding.alternatives)}" if finding.alternatives else "")
                       + f"  \nDomains on the page: {_domains_text(finding.domains or [])}"
                       + (f"  \nAlready configured: {'; '.join(already)}" if already else ""))
            if c2.button("Discard", key=f"as_discard_{uid}", width="stretch"):
                add_source_state.discard([finding.url], "discarded by you")
                for key in (f"as_sel_{uid}", f"as_agents_{uid}", f"as_doms_{uid}", "as_review"):
                    state.pop(key, None)
                _rerun()
            d1, d2 = st.columns(2)
            d1.multiselect("Agents", agents, key=f"as_agents_{uid}", format_func=lambda a: us.AGENT_LABELS[a])
            d2.multiselect("Research domains", list(us.DOMAINS), key=f"as_doms_{uid}",
                           placeholder="None detected — pick at least one")
    for note in notes:
        st.caption(note)
    hidden = add_source_state.discarded_for_site(url)
    if hidden:
        if st.button(f"Show {len(hidden)} discarded page(s) from this site again"):
            add_source_state.restore_site(url)
            _run_site_scan(url)
            _rerun()

    selected = []
    for finding in useful:
        uid = _url_id(finding.url)
        if not state.get(f"as_sel_{uid}"):
            continue
        for agent in state.get(f"as_agents_{uid}") or []:
            selected.append({"url": finding.url, "title": finding.title, "agent": agent,
                             "domains": list(state.get(f"as_doms_{uid}") or [])})
    if st.button(f"Review selected ({len(selected)})", disabled=not selected):
        state["as_review"] = selected
        _rerun()

    review = state.get("as_review")
    if review:
        configured = us.configured_urls()
        st.markdown("#### Please confirm")
        st.markdown("These will be added — nothing has been added yet:")
        ok_items = []
        for item in review:
            issue = _agent_issue(item["agent"], item["url"], configured)
            if not item["domains"]:
                issue = issue or "no research domain chosen"
            st.markdown(f"- **{item['title'] or item['url']}** → **{us.AGENT_LABELS[item['agent']]}** · "
                        f"domains: {', '.join(item['domains']) or '—'}  \n  {item['url']}"
                        + (f" — **will be skipped: {issue}**" if issue else ""))
            if not issue:
                ok_items.append(item)
        b1, b2 = st.columns(2)
        if b1.button(f"Confirm and add ({len(ok_items)})", type="primary", width="stretch", disabled=not ok_items):
            _add_items(ok_items, pick["focus"])
        if b2.button("Back", width="stretch"):
            state.pop("as_review", None)
            _rerun()


def render_add_source() -> None:
    from core import user_sources as us

    _restore_add_source()
    try:
        restored = us.ensure_user_sources()
        for row in restored:
            us.sync_registry_for(row["agent"], row["url"])
        if restored:
            st.info(f"Restored {len(restored)} added source(s) that were missing from the source files "
                    "(for example after a git checkout): " + "; ".join(r["url"] for r in restored))
    except Exception as exc:
        st.caption(f"Added sources could not be checked ({type(exc).__name__}).")
    _render_add_source_body()
    _persist_add_source()

    # ---- sources the user added
    rows = us.list_user_sources()
    st.subheader(f"Sources you added ({len(rows)})")
    if not rows:
        st.caption("None yet.")
    for index, row in enumerate(rows):
        c1, c2 = st.columns([6, 1])
        c1.markdown(f"**{row['name']}** · {us.AGENT_LABELS[row['agent']]} · added {row.get('added_at') or '—'}  \n{row['url']}")
        if c2.button("Remove", key=f"as_rm_{index}", width="stretch"):
            st.session_state["as_confirm_remove"] = {"agent": row["agent"], "name": row["name"], "url": row["url"]}
            _rerun()
        pending = st.session_state.get("as_confirm_remove")
        if pending and pending["url"] == row["url"] and pending["agent"] == row["agent"]:
            st.warning(f"Remove \"{row['name']}\" from the {us.AGENT_LABELS[row['agent']]} agent? "
                       "It will no longer be collected and will not be restored.")
            k1, k2 = st.columns(2)
            if k1.button("Yes, remove it", key=f"as_rm_yes_{index}", type="primary", width="stretch"):
                us.remove_user_source(row["agent"], row["name"])
                us.sync_registry_for(row["agent"], row["url"])
                st.session_state.pop("as_confirm_remove", None)
                st.session_state.pop("as_done", None)
                _rerun()
            if k2.button("Keep it", key=f"as_rm_no_{index}", width="stretch"):
                st.session_state.pop("as_confirm_remove", None)
                _rerun()
    if rows:
        st.caption("Added sources are saved in the agents' files under sources/. "
                   "Commit them to keep them in git and to use them in GitHub CI.")


def _render_add_source_body() -> None:
    from core import user_sources as us
    from core.agent_fit import best_agents, fits_agent, trace_pages

    st.title("Add a source")
    st.caption("Give a name or a link. Pick agents and research domains, or leave them empty to let the app decide "
               "from the page text. Nothing is added until you confirm. Your progress is kept if the page reloads.")
    state = st.session_state
    if state.get("as_done"):
        st.success(state["as_done"])
    last = state.get("as_last_added") or []
    if last:
        if st.button(f"Test collection now ({len(last)} source{'s' if len(last) != 1 else ''})",
                     help="Runs each agent's own collection code on the new source, exactly as a pipeline run does."):
            results = []
            for item in last:
                with st.spinner(f"Collecting {item['url']} with the {us.AGENT_LABELS[item['agent']]} agent..."):
                    results.append({**item, "result": us.test_collect(item["agent"], item["url"])})
            state["as_test_results"] = results
            _rerun()
    for item in state.get("as_test_results") or []:
        r = item["result"]
        label = f"{us.AGENT_LABELS[item['agent']]} agent · {item['url']}"
        if not r.get("ok"):
            st.error(f"{label}: collection failed — {r.get('error')}")
            continue
        summary = (f"{label}: fetched {r['pages']} page(s), produced {r['documents']} document(s)"
                   + (f", status {r['status']}" if r.get("status") else "") + f" in {r['seconds']} s.")
        (st.success if r["documents"] else st.warning)(summary + ("" if r["documents"] else
            " No document passed the agent's relevance filters for the current research context."))
        if r.get("titles"):
            st.caption("Documents: " + "; ".join(t for t in r["titles"] if t))
        for message in (r.get("warnings") or []) + (r.get("errors") or []):
            st.caption(f"Note: {message}")

    # ---- step 1: what and where
    with st.container(border=True):
        query = st.text_input("Name or URL", placeholder="e.g. Gitcoin   or   https://gitcoin.co/grants", key="as_form_query")
        c1, c2 = st.columns(2)
        agents = c1.multiselect("Add to agents", list(us.AGENT_FILES), key="as_form_agents",
                                format_func=lambda a: us.AGENT_LABELS[a], placeholder="Let the app decide")
        domains = c2.multiselect("Research domains", list(us.DOMAINS), key="as_form_domains",
                                 placeholder="Let the app decide")
        focus = st.text_input("Note (optional)", placeholder="What should the agent look for on this site?", key="as_form_focus")
        b1, b2 = st.columns([1, 4])
        if b1.button("Find website", type="primary", disabled=not query.strip()):
            _reset_add_source()
            state["as_query"] = query.strip()
            if us.looks_like_url(query):
                state["as_candidates"] = [{"label": query.strip(), "description": "the link you gave",
                                           "url": us.normalize_url(query)}]
            else:
                with st.spinner("Looking up the official website..."):
                    try:
                        state["as_candidates"] = us.find_url_candidates(query)
                    except Exception as exc:
                        state["as_candidates"] = []
                        st.error(f"Lookup failed ({type(exc).__name__}). Paste the link instead.")
        if state.get("as_candidates") is not None and b2.button("Start over"):
            _reset_add_source()
            _rerun()

    candidates = state.get("as_candidates")
    if candidates is None:
        return
    # ---- step 2: is this the right URL?
    if not candidates:
        st.warning(f"No official website found for \"{state.get('as_query')}\". Paste the link instead.")
        return
    if not state.get("as_inspection"):
        st.subheader("Is this the right website?")
        pick = st.radio("Website", list(range(len(candidates))), label_visibility="collapsed",
                        format_func=lambda i: f"{candidates[i]['label']} — {candidates[i]['url']}"
                        + (f"  ({candidates[i]['description']})" if candidates[i].get("description") else ""))
        if candidates[0].get("wikidata"):
            st.caption("Found via Wikidata's \"official website\" field. That is usually the homepage; "
                       "the next step checks the page and the pages it links to.")
        b1, b2 = st.columns(2)
        if b1.button("Yes, this one", type="primary", width="stretch"):
            state["as_pick"] = {"agents": list(agents), "domains": list(domains), "focus": focus,
                                "by_name": bool(candidates[pick].get("wikidata")),
                                "name": candidates[pick]["label"] if candidates[pick].get("wikidata") else ""}
            with st.spinner("Checking the page, which agents and research domains it suits..."):
                _inspect_into_state(candidates[pick]["url"], list(agents), list(domains))
            _rerun()
        if b2.button("No, cancel", width="stretch"):
            _reset_add_source()
            _rerun()
        return

    # ---- step 3: page check, site scan, this page
    inspection = state["as_inspection"]
    pick = state["as_pick"]
    ok_status = bool(inspection.status and inspection.status < 400)
    url = inspection.final_url if inspection.final_url and ok_status else inspection.url
    fits = inspection.agent_fits
    suggested = [f for f in best_agents(fits) if fits_agent(fits, f.agent)]
    detected = inspection.detected_domains or []
    st.subheader("Check before adding")
    with st.container(border=True):
        st.markdown(f"**Page title:** {inspection.title or '—'}")
        st.markdown(f"**URL:** {url}")
        if inspection.final_url and inspection.final_url.rstrip('/') != inspection.url.rstrip('/'):
            st.caption(f"Redirected from {inspection.url}")
        st.markdown(f"**Research domains detected on the page:** {_domains_text(detected)}")
        robots = {True: "allowed", False: "not allowed"}.get(inspection.robots_allowed, "unknown")
        st.markdown(f"**robots.txt:** {robots} · **HTTP status:** {inspection.status or '—'}")

    chosen = list(pick["agents"])
    not_fit = [a for a in chosen if not (fits and fits_agent(fits, a))]
    _render_site_scan(url, pick, auto=not chosen or bool(pick.get("by_name")) or bool(not_fit) or not fits)

    # ---- this page only
    st.markdown("#### Or add just this page")
    uid = _url_id(url)
    configured = us.configured_urls()
    fit_by_agent = {f.agent: f for f in fits}
    options: list[tuple[str, str, bool]] = []  # (agent, label, default ticked)
    for agent in chosen:
        own = fit_by_agent.get(agent)
        if fits and fits_agent(fits, agent):
            options.append((agent, f"{us.AGENT_LABELS[agent]} — fits this page (evidence: {_evidence_text(own)})", True))
        else:
            reason = f"evidence: {_evidence_text(own)}" if own and own.evidence else "no evidence on this page"
            options.append((agent, f"{us.AGENT_LABELS[agent]} — does not look like a good fit ({reason})", False))
    for fit in suggested:
        if fit.agent not in chosen:
            options.append((fit.agent, f"{us.AGENT_LABELS[fit.agent]} — suggested by the app (evidence: {_evidence_text(fit)})", False))

    if chosen and not_fit:
        others = [us.AGENT_LABELS[f.agent] for f in suggested if f.agent not in chosen]
        names = ", ".join(us.AGENT_LABELS[a] for a in not_fit)
        st.warning(f"This page does not look like a good fit for: {names}. "
                   + (f"It fits: {', '.join(others)}." if others else
                      ("No agent is a good fit for this URL itself; see the pages on this site above."
                       if not suggested else "")))
        if len(chosen) == 1 and st.button(f"Find a better page for {us.AGENT_LABELS[chosen[0]]} on this site"):
            with st.spinner("Following the site's links and checking each page..."):
                state["as_trace"] = {"agent": chosen[0], "pages": trace_pages(url, chosen[0])}
            _rerun()
    if not options:
        if fits and best_agents(fits):
            st.warning("This page itself fits no agent clearly (see the pages on this site above). "
                       "To add it anyway, pick agents below.")
        else:
            st.warning("The page has too little readable text to tell which agent it suits "
                       "(often a JavaScript-only page). Pick agents below, or paste a more specific page.")

    trace = state.get("as_trace")
    if trace and chosen and trace["agent"] == chosen[0]:
        given = next((p for p in trace["pages"] if p.note == "the page you gave"), None)
        given_score = given.score if given else 0.0
        better = [p for p in trace["pages"] if p.note != "the page you gave" and p.score >= 8 and p.score > given_score]
        checked = len(trace["pages"]) - (1 if given else 0)
        if better:
            st.markdown(f"**Pages on this site that fit the {us.AGENT_LABELS[trace['agent']]} agent better** "
                        f"(checked {checked} linked pages):")
            choice = st.radio("Better pages", list(range(len(better))), label_visibility="collapsed",
                              format_func=lambda i: f"{better[i].title or better[i].url} — {better[i].url} "
                              f"(evidence: {', '.join(better[i].evidence[:4])})")
            if st.button("Use this page", type="primary"):
                with st.spinner("Checking the page..."):
                    _inspect_into_state(better[choice].url, chosen, pick["domains"])
                _rerun()
        else:
            st.info(f"No linked page on this site fits the {us.AGENT_LABELS[trace['agent']]} agent better "
                    f"(checked {checked} linked pages). You can still add this page, or paste a specific page.")

    ticked: list[str] = []
    if options:
        st.caption("Tick the agents to add this page to:")
        for agent, label, default in options:
            key = f"as_one_{uid}_{agent}"
            issue = _agent_issue(agent, url, configured)
            state.setdefault(key, default and not issue)
            st.checkbox(label + (f" — cannot add: {issue}" if issue else ""), key=key, disabled=bool(issue))
            if state.get(key) and not issue:
                ticked.append(agent)
    else:
        manual_key = f"as_one_{uid}_manual"
        state.setdefault(manual_key, [])
        manual = st.multiselect("Agents", list(us.AGENT_FILES), key=manual_key, format_func=lambda a: us.AGENT_LABELS[a])
        for agent in manual:
            issue = _agent_issue(agent, url, configured)
            if issue:
                st.error(f"{us.AGENT_LABELS[agent]}: {issue}")
            else:
                ticked.append(agent)

    dom_key = f"as_one_{uid}_domains"
    state.setdefault(dom_key, list(pick["domains"]) or [d for d, _terms in detected])
    if not pick["domains"]:
        st.caption(f"Research domains suggested from the page text: {_domains_text(detected)}. You can change them.")
    page_domains = st.multiselect("Research domains for this page", list(us.DOMAINS), key=dom_key,
                                  placeholder="None detected — pick at least one")
    for problem in inspection.problems:
        if "Already configured" not in problem:
            st.error(problem)
    for warning in inspection.warnings:
        st.warning(warning)
    name_key = f"as_one_{uid}_name"
    state.setdefault(name_key, pick["name"] or inspection.title or "")
    name = st.text_input("Source name", key=name_key)
    blocking = [p for p in inspection.problems if "Already configured" not in p]
    if not page_domains:
        st.info("Pick at least one research domain: none was found on the page text.")
    b1, b2 = st.columns(2)
    if b1.button(f"Add this page to {len(ticked)} agent(s)", type="primary", width="stretch",
                 disabled=not ticked or bool(blocking) or not name.strip() or not page_domains):
        _add_items([{"url": url, "title": inspection.title, "name": name.strip(), "agent": agent,
                     "domains": list(page_domains)} for agent in ticked], pick["focus"])
    if b2.button("No, cancel", key="as_cancel2", width="stretch"):
        _reset_add_source()
        _rerun()


def main() -> None:
    st.set_page_config(page_title="RIF", page_icon="🔎", layout="wide")
    st.sidebar.title("RIF")
    st.sidebar.caption("Research Intelligence Framework")
    pages = ["Open calls", "Add source", "Run pipeline", "Advanced"]
    # Keep the current page in the URL (?page=...) so a browser reload returns to it.
    current = st.query_params.get("page", pages[0])
    page = st.sidebar.radio("Page", pages, index=pages.index(current) if current in pages else 0,
                            label_visibility="collapsed")
    if st.query_params.get("page") != page:
        st.query_params["page"] = page
    if page == "Open calls":
        render_open_calls()
    elif page == "Add source":
        render_add_source()
    elif page == "Run pipeline":
        render_full_pipeline()
    else:
        render_advanced()


if __name__ == "__main__":
    main()
