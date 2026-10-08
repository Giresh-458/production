"""Streamlit UI for the Research Intelligence Framework.

Run with:
    streamlit run ui/streamlit_app.py
"""

from __future__ import annotations

import json
import platform
import sys
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
        st.dataframe(outputs, use_container_width=True)
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
        st.dataframe(domain_rows, use_container_width=True)

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

    if st.button("🔎 Discover Open Call & Run Full Pipeline", type="primary", use_container_width=True):
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

    if st.button("Run Agent", use_container_width=True):
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

    if st.button("Process Manual Input", use_container_width=True):
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
                st.dataframe([{"path": item} for item in pending_synthesis_queue[:25]], use_container_width=True)

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
                st.dataframe(flatten_dict_rows(by_agent, key_name="agent"), use_container_width=True)

            if attention_queue:
                st.subheader("Attention Queue")
                st.dataframe(attention_queue[:25], use_container_width=True)

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
                st.dataframe(entries[:50], use_container_width=True)
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
                st.dataframe(entries[:50], use_container_width=True)
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
                st.dataframe(entries[:50], use_container_width=True)
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
                st.dataframe(entries[:50], use_container_width=True)
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
                st.dataframe(preview_rows, use_container_width=True)

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
                st.dataframe(cluster_rows, use_container_width=True)
                selected_cluster_index = st.selectbox(
                    "Inspect Cluster",
                    range(len(filtered_clusters)),
                    format_func=lambda idx: filtered_clusters[idx].get("cluster_id", f"Cluster {idx + 1}"),
                    key="cluster_selector",
                )
                selected_cluster = filtered_clusters[selected_cluster_index]
                if selected_cluster.get("members"):
                    st.subheader("Cluster Members")
                    st.dataframe(selected_cluster.get("members"), use_container_width=True)
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
                st.dataframe(rows, use_container_width=True)
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
                st.dataframe(rows, use_container_width=True)
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
            st.dataframe(index_rows, use_container_width=True)
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

                if st.button("Apply Source Review Action", use_container_width=True):
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
            if action_columns[0].button("Save Promotion Override", use_container_width=True):
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

            if action_columns[1].button("Clear Promotion Override", use_container_width=True):
                try:
                    clear_promotion_override(synthesized_file=synthesized_file)
                    refresh_repo_views()
                    st.success(f"Cleared promotion override for `{selected_entry.get('title', 'entry')}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to clear promotion override: {exc}")

            if override_entries:
                st.subheader("Existing Promotion Overrides")
                st.dataframe(override_entries, use_container_width=True)
                render_json_expander("Raw Promotion Overrides", promotion_overrides)


def main() -> None:
    st.set_page_config(page_title="RIF UI", layout="wide")
    st.sidebar.title("RIF Navigation")
    page = st.sidebar.radio(
        "Page",
        [
            "Dashboard",
            "Run Full Pipeline",
            "Run Agent",
            "Manual Input",
            "View Outputs",
            "Insights",
            "Operational Views",
            "Processing And Intelligence",
            "Review And Promotion",
            "Settings",
        ],
    )

    if page == "Dashboard":
        render_dashboard()
    elif page == "Run Full Pipeline":
        render_full_pipeline()
    elif page == "Run Agent":
        render_run_agent()
    elif page == "Manual Input":
        render_manual_input()
    elif page == "View Outputs":
        render_view_outputs()
    elif page == "Insights":
        render_insights()
    elif page == "Operational Views":
        render_operational_views()
    elif page == "Processing And Intelligence":
        render_processing_and_intelligence_views()
    elif page == "Review And Promotion":
        render_review_and_promotion()
    else:
        render_settings()


if __name__ == "__main__":
    main()
