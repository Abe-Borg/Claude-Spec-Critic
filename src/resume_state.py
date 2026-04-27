"""Durable resume-state serialization helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import __version__
from .batch import BatchJob
from .code_cycles import DEFAULT_CYCLE
from .extractor import ExtractedSpec
from .pipeline import BatchSubmission, CollectedBatchState
from .reviewer import Finding, ReviewResult, MODEL_OPUS_46
from .verifier import VerificationResult

PHASE_REVIEW_POLL = "review_poll"
PHASE_REVIEW_COLLECT = "review_collect"
PHASE_VERIFICATION_WAVE_POLL = "verification_wave_poll"
PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL = "cross_check_verification_wave_poll"
PHASE_VERIFICATION_POLL = "verification_poll"
PHASE_CROSS_CHECK = "cross_check"
PHASE_CROSS_CHECK_VERIFICATION_POLL = "cross_check_verification_poll"
PHASE_FINALIZE = "finalize"
SUPPORTED_PHASES = {
    PHASE_REVIEW_POLL,
    PHASE_REVIEW_COLLECT,
    PHASE_VERIFICATION_POLL,
    PHASE_VERIFICATION_WAVE_POLL,
    PHASE_CROSS_CHECK,
    PHASE_CROSS_CHECK_VERIFICATION_POLL,
    PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
    PHASE_FINALIZE,
}


def serialize_batch_job(job: BatchJob) -> dict[str, Any]:
    return {
        "batch_id": job.batch_id,
        "job_type": job.job_type,
        "request_map": job.request_map,
        "created_at": job.created_at,
        "status": job.status,
    }


def deserialize_batch_job(payload: dict[str, Any]) -> BatchJob:
    return BatchJob(
        batch_id=str(payload["batch_id"]),
        job_type=str(payload.get("job_type", "review")),
        request_map=dict(payload["request_map"]),
        created_at=float(payload["created_at"]),
        status=str(payload.get("status", "submitted")),
    )


def serialize_extracted_spec(spec: ExtractedSpec) -> dict[str, Any]:
    return {
        "filename": spec.filename,
        "content": spec.content,
        "word_count": spec.word_count,
        "source_path": spec.source_path,
        "source_format": spec.source_format,
    }


def deserialize_extracted_spec(payload: dict[str, Any]) -> ExtractedSpec:
    return ExtractedSpec(
        filename=str(payload["filename"]),
        content=str(payload.get("content", "")),
        word_count=int(payload.get("word_count", 0)),
        source_path=str(payload.get("source_path", "")),
        source_format=str(payload.get("source_format", "unknown")),
    )


def serialize_verification_result(result: VerificationResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "verdict": result.verdict,
        "explanation": result.explanation,
        "sources": list(result.sources),
        "correction": result.correction,
    }


def deserialize_verification_result(payload: dict[str, Any] | None) -> VerificationResult | None:
    if not payload:
        return None
    return VerificationResult(
        verdict=str(payload.get("verdict", "UNVERIFIED")),
        explanation=str(payload.get("explanation", "")),
        sources=[str(s) for s in payload.get("sources", [])],
        correction=(str(payload["correction"]) if payload.get("correction") is not None else None),
    )


def serialize_finding(finding: Finding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "fileName": finding.fileName,
        "section": finding.section,
        "issue": finding.issue,
        "actionType": finding.actionType,
        "existingText": finding.existingText,
        "replacementText": finding.replacementText,
        "codeReference": finding.codeReference,
        "confidence": finding.confidence,
        "affected_files": list(finding.affected_files),
        "verification": serialize_verification_result(finding.verification),
        "anchorText": finding.anchorText,
        "insertPosition": finding.insertPosition,
    }


def deserialize_finding(payload: dict[str, Any]) -> Finding:
    insert_position_raw = payload.get("insertPosition")
    insert_position = str(insert_position_raw).strip().lower() if insert_position_raw is not None else None
    if insert_position not in {"before", "after"}:
        insert_position = None
    return Finding(
        severity=str(payload.get("severity", "MEDIUM")),
        fileName=str(payload.get("fileName", "")),
        section=str(payload.get("section", "")),
        issue=str(payload.get("issue", "")),
        actionType=str(payload.get("actionType", "EDIT")),
        existingText=(str(payload["existingText"]) if payload.get("existingText") is not None else None),
        replacementText=(str(payload["replacementText"]) if payload.get("replacementText") is not None else None),
        codeReference=(str(payload["codeReference"]) if payload.get("codeReference") is not None else None),
        confidence=float(payload.get("confidence", 0.5)),
        affected_files=[str(v) for v in payload.get("affected_files", [])],
        verification=deserialize_verification_result(payload.get("verification")),
        anchorText=(str(payload["anchorText"]) if payload.get("anchorText") is not None else None),
        insertPosition=insert_position,
    )


def serialize_review_result(result: ReviewResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "findings": [serialize_finding(f) for f in result.findings],
        "raw_response": result.raw_response,
        "thinking": result.thinking,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "elapsed_seconds": result.elapsed_seconds,
        "error": result.error,
        "stop_reason": result.stop_reason,
        "parse_status": result.parse_status,
        "cross_check_status": result.cross_check_status,
    }


def deserialize_review_result(payload: dict[str, Any] | None) -> ReviewResult | None:
    if not payload:
        return None
    return ReviewResult(
        findings=[deserialize_finding(f) for f in payload.get("findings", [])],
        raw_response=str(payload.get("raw_response", "")),
        thinking=str(payload.get("thinking", "")),
        model=str(payload.get("model", MODEL_OPUS_46)),
        input_tokens=int(payload.get("input_tokens", 0)),
        output_tokens=int(payload.get("output_tokens", 0)),
        elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
        error=(str(payload["error"]) if payload.get("error") is not None else None),
        stop_reason=(str(payload["stop_reason"]) if payload.get("stop_reason") is not None else None),
        parse_status=(str(payload["parse_status"]) if payload.get("parse_status") is not None else None),
        cross_check_status=(str(payload["cross_check_status"]) if payload.get("cross_check_status") is not None else None),
    )


def serialize_submission(submission: BatchSubmission) -> dict[str, Any]:
    return {
        "job": serialize_batch_job(submission.job),
        "files_reviewed": list(submission.files_reviewed),
        "review_request_ids": list(submission.review_request_ids),
        "leed_alerts": list(submission.leed_alerts),
        "placeholder_alerts": list(submission.placeholder_alerts),
        "model": submission.model,
        "project_context": submission.project_context,
        "code_cycle": submission.cycle_label,
        "cross_check_enabled": submission.cross_check_enabled,
        "export_mode": submission.export_mode,
        "prepared_specs": [serialize_extracted_spec(s) for s in (submission.prepared_specs or [])],
    }


def deserialize_submission(payload: dict[str, Any]) -> BatchSubmission:
    specs_payload = payload.get("prepared_specs") or []
    prepared_specs = [deserialize_extracted_spec(s) for s in specs_payload] if specs_payload else None
    return BatchSubmission(
        job=deserialize_batch_job(payload["job"]),
        files_reviewed=[str(v) for v in payload.get("files_reviewed", [])],
        review_request_ids=[str(v) for v in payload.get("review_request_ids", [])],
        leed_alerts=list(payload.get("leed_alerts", [])),
        placeholder_alerts=list(payload.get("placeholder_alerts", [])),
        model=str(payload.get("model", MODEL_OPUS_46)),
        project_context=str(payload.get("project_context", "")),
        cycle_label=str(payload.get("code_cycle", DEFAULT_CYCLE.label)),
        cross_check_enabled=bool(payload.get("cross_check_enabled", False)),
        export_mode=bool(payload.get("export_mode", False)),
        prepared_specs=prepared_specs,
    )


def serialize_collected_batch_state(state: CollectedBatchState) -> dict[str, Any]:
    return {
        "review_result": serialize_review_result(state.review_result),
        "cross_check_result": serialize_review_result(state.cross_check_result),
        "files_reviewed": list(state.files_reviewed),
        "leed_alerts": list(state.leed_alerts),
        "placeholder_alerts": list(state.placeholder_alerts),
        "cross_check_skipped_due_to_missing_specs": bool(state.cross_check_skipped_due_to_missing_specs),
        "truncated_specs": list(state.truncated_specs),
    }


def deserialize_collected_batch_state(payload: dict[str, Any], submission: BatchSubmission) -> CollectedBatchState:
    review = deserialize_review_result(payload.get("review_result"))
    if review is None:
        raise ValueError("Missing review_result payload")
    return CollectedBatchState(
        submission=submission,
        review_result=review,
        files_reviewed=[str(v) for v in payload.get("files_reviewed", submission.files_reviewed)],
        leed_alerts=list(payload.get("leed_alerts", submission.leed_alerts)),
        placeholder_alerts=list(payload.get("placeholder_alerts", submission.placeholder_alerts)),
        cross_check_result=deserialize_review_result(payload.get("cross_check_result")),
        cross_check_skipped_due_to_missing_specs=bool(payload.get("cross_check_skipped_due_to_missing_specs", False)),
        truncated_specs=[str(v) for v in payload.get("truncated_specs", [])],
    )


def build_resume_state(*, phase: str, submission: BatchSubmission, review_state: CollectedBatchState | None = None, verification_batch: BatchJob | None = None, cross_check_skipped_due_to_missing_specs: bool = False, verification_started: bool = False, verification_completed: bool = False, wave_index: int = 0, resolved_finding_indices: list[int] | None = None, pending_finding_indices: list[int] | None = None, poll_detached: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = {
        "version": __version__,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "submission": serialize_submission(submission),
        "resume_flags": {
            "cross_check_skipped_due_to_missing_specs": cross_check_skipped_due_to_missing_specs,
            "verification_started": verification_started,
            "verification_completed": verification_completed,
            "wave_index": wave_index,
            "resolved_finding_indices": list(resolved_finding_indices or []),
            "pending_finding_indices": list(pending_finding_indices or []),
            "poll_detached": poll_detached,
        },
    }
    if review_state is not None:
        state["review_findings_payload"] = serialize_collected_batch_state(review_state)
    if verification_batch is not None:
        state["verification_batch"] = serialize_batch_job(verification_batch)
    return state


def deserialize_resume_state(payload: dict[str, Any]) -> dict[str, Any]:
    phase = str(payload.get("phase", ""))
    submission_payload = payload.get("submission")
    if not isinstance(submission_payload, dict):
        raise ValueError("Missing submission payload")
    submission = deserialize_submission(submission_payload)
    out: dict[str, Any] = {
        "version": payload.get("version"),
        "saved_at": payload.get("saved_at"),
        "phase": phase,
        "submission": submission,
        "resume_flags": payload.get("resume_flags", {}),
    }
    if payload.get("review_findings_payload"):
        out["review_state"] = deserialize_collected_batch_state(payload["review_findings_payload"], submission)
    if payload.get("verification_batch"):
        out["verification_batch"] = deserialize_batch_job(payload["verification_batch"])
    return out
