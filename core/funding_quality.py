"""Batch-level acceptance assessment for Funding Agent audit artifacts.

This is deliberately separate from process/test success: a scan can run
successfully while its data is not complete enough to accept.
"""
from __future__ import annotations

from typing import Any, Iterable


def assess_batch_acceptance(
    *,
    run_status: str | None,
    raw_records_count: int,
    selected_actionable_count: int,
    selected_quality_flags: Iterable[dict[str, Any]] = (),
    selected_manual_review_flags: Iterable[dict[str, Any]] = (),
    source_summary: Iterable[dict[str, Any]] = (),
    expected_source_ids: Iterable[str] | None = None,
    record_accounting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ACCEPTED/BLOCKED plus explicit, deterministic blockers.

    A batch is blocked when the scan failed, no records/actionable calls were
    emitted, selected calls have quality/manual-review flags, or a source has
    unresolved evidence of incomplete acquisition. This function does not
    infer that a source is complete from a successful HTTP response.
    """
    blockers: list[dict[str, str]] = []

    def add(code: str, message: str, source_id: str | None = None) -> None:
        item = {"code": code, "message": message}
        if source_id:
            item["source_id"] = source_id
        if item not in blockers:
            blockers.append(item)

    if run_status != "success":
        add("funding_scan_not_successful", f"Funding scan status is {run_status!r}, not 'success'.")
    if raw_records_count <= 0:
        add("no_records_discovered", "The scan emitted no raw funding records.")
    if selected_actionable_count <= 0:
        add("no_actionable_calls", "The strict selector emitted no actionable funding calls.")

    if not isinstance(record_accounting, dict):
        add(
            "record_accounting_missing",
            "The audit did not provide source-to-output record accounting.",
        )
    elif not record_accounting.get("reconciled"):
        add(
            "record_accounting_unreconciled",
            "Source-stage records do not reconcile with processed/emitted records: "
            f"source-to-documents delta={record_accounting.get('unaccounted_source_to_documents_delta')!r}, "
            f"documents-to-output delta={record_accounting.get('unaccounted_documents_to_outputs_delta')!r}.",
        )

    for entry in selected_quality_flags or ():
        title = str(entry.get("title") or "(untitled selected record)")
        issues = entry.get("issues") or []
        add(
            "selected_record_quality_failure",
            f"{title}: " + ", ".join(str(issue) for issue in issues or ["unspecified quality failure"]),
        )

    for entry in selected_manual_review_flags or ():
        title = str(entry.get("title") or "(untitled selected record)")
        flags = entry.get("flags") or []
        add(
            "selected_record_requires_manual_review",
            f"{title}: " + ", ".join(str(flag) for flag in flags or ["manual review required"]),
        )

    sources = list(source_summary or ())
    if not sources:
        add(
            "source_coverage_telemetry_missing",
            "No source-level coverage/verification summary was provided.",
        )

    provided_ids = [str(source.get("source_id") or "").strip() for source in sources]
    provided_id_set = {source_id for source_id in provided_ids if source_id}
    expected_ids = {str(source_id).strip() for source_id in (expected_source_ids or ()) if str(source_id).strip()}
    if expected_ids:
        for missing_id in sorted(expected_ids - provided_id_set):
            add(
                "source_coverage_telemetry_missing",
                "Configured source is missing from the source-level audit summary.",
                missing_id,
            )
        duplicate_ids = sorted({source_id for source_id in provided_ids if source_id and provided_ids.count(source_id) > 1})
        for duplicate_id in duplicate_ids:
            add(
                "source_coverage_telemetry_duplicate",
                "Configured source appears more than once in the source-level audit summary.",
                duplicate_id,
            )

    for source in sources:
        source_id = str(source.get("source_id") or "(unknown source)")
        status = str(source.get("final_status") or "UNKNOWN").upper()
        gaps = int(source.get("coverage_gaps_count") or len(source.get("coverage_gaps") or []))
        skipped = int(source.get("relevant_urls_skipped") or 0)
        failed_pages = int(source.get("pages_failed") or 0)
        failed_details = int(source.get("detail_pages_failed") or 0)
        failed_documents = int(source.get("documents_failed") or 0)
        truncated = bool(source.get("truncated"))
        pagination_detected = bool(source.get("pagination_detected"))
        pagination_complete = bool(source.get("pagination_complete"))
        exhausted = bool(source.get("discovery_exhausted"))
        errors = source.get("errors") or []
        evidence_verified = source.get("evidence_verified") is True
        currentness_verified = source.get("currentness_verified") is True

        incomplete_reasons: list[str] = []
        if status in {"", "UNKNOWN"}:
            incomplete_reasons.append("source status unknown")
        if not evidence_verified:
            incomplete_reasons.append("evidence not verified")
        if not currentness_verified:
            incomplete_reasons.append("currentness not verified")
        if status in {"PARTIAL", "FAIL", "ERROR", "TEST_TRUNCATED"}:
            incomplete_reasons.append(f"source status={status}")
        if gaps:
            incomplete_reasons.append(f"{gaps} coverage gap(s)")
        if skipped:
            incomplete_reasons.append(f"{skipped} relevant URL(s) skipped")
        if failed_pages or failed_details or failed_documents:
            incomplete_reasons.append(
                f"fetch failures: pages={failed_pages}, details={failed_details}, documents={failed_documents}"
            )
        if truncated:
            incomplete_reasons.append("truncated")
        if pagination_detected and not pagination_complete:
            incomplete_reasons.append("pagination incomplete")
        if not exhausted and status not in {"NOT_APPLICABLE"}:
            incomplete_reasons.append("discovery not exhausted")
        if errors:
            incomplete_reasons.append(f"{len(errors)} reported error(s)")

        # Even statuses such as VERIFIED/NO_ACTIVE_OPPORTUNITIES must not
        # conceal evidence of incomplete acquisition.
        if incomplete_reasons:
            add(
                "source_coverage_incomplete",
                "; ".join(incomplete_reasons),
                source_id,
            )

    return {
        "status": "ACCEPTED" if not blockers else "BLOCKED",
        "accepted": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
    }
