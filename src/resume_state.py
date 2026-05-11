"""Durable resume-state serialization helpers."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .batch import BatchJob
from .code_cycles import DEFAULT_CYCLE
from .extractor import ExtractedSpec
from .pipeline import BatchSubmission, CollectedBatchState
from .reviewer import Finding, ReviewResult, MODEL_OPUS_46
from .verifier import VerificationResult

_log = logging.getLogger(__name__)


def _content_digest(content: str) -> str:
    """SHA-256 of the extracted spec content (Phase 5.5)."""
    return hashlib.sha256((content or "").encode("utf-8", errors="replace")).hexdigest()


def _source_file_digest(source_path: str) -> str | None:
    """SHA-256 of the underlying file at ``source_path``, or None on error."""
    if not source_path:
        return None
    try:
        path = Path(source_path)
        if not path.is_file():
            return None
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None

_RESUME_STATE_CURRENT_SCHEMA = "v2"

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
    # Phase 5.5 (audit Section 9.5): record both the extracted-content digest
    # and the source-file digest so deserialize can detect that the spec was
    # changed on disk between save and resume.
    return {
        "filename": spec.filename,
        "content": spec.content,
        "word_count": spec.word_count,
        "source_path": spec.source_path,
        "source_format": spec.source_format,
        "content_sha256": _content_digest(spec.content),
        "source_sha256": _source_file_digest(spec.source_path),
    }


def deserialize_extracted_spec(payload: dict[str, Any]) -> ExtractedSpec:
    spec = ExtractedSpec(
        filename=str(payload["filename"]),
        content=str(payload.get("content", "")),
        word_count=int(payload.get("word_count", 0)),
        source_path=str(payload.get("source_path", "")),
        source_format=str(payload.get("source_format", "unknown")),
    )
    # Phase 5.5: warn when the on-disk file no longer matches the saved
    # digest. Resume continues — the saved content is still authoritative
    # for the in-flight batch — but the user is told the file changed.
    expected_content = payload.get("content_sha256")
    if expected_content:
        actual_content = _content_digest(spec.content)
        if actual_content != expected_content:
            _log.warning(
                "Resume: stored content digest mismatch for %s (saved=%s actual=%s)",
                spec.filename, expected_content[:12], actual_content[:12],
            )
    expected_source = payload.get("source_sha256")
    if expected_source and spec.source_path:
        actual_source = _source_file_digest(spec.source_path)
        if actual_source is None:
            _log.warning(
                "Resume: source file missing for %s at %s — using saved content",
                spec.filename, spec.source_path,
            )
        elif actual_source != expected_source:
            _log.warning(
                "Resume: source file %s changed on disk since save (saved=%s actual=%s)",
                spec.source_path, expected_source[:12], actual_source[:12],
            )
    return spec


def serialize_verification_result(result: VerificationResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "verdict": result.verdict,
        "explanation": result.explanation,
        "sources": list(result.sources),
        "correction": result.correction,
        # Phase 3 evidence model fields. Missing keys deserialize to safe
        # defaults so legacy resume payloads still load.
        "grounded": result.grounded,
        "model_used": result.model_used,
        "escalated": result.escalated,
        "cache_status": result.cache_status,
        "web_search_requests": result.web_search_requests,
        "successful_source_count": result.successful_source_count,
        "search_error_count": result.search_error_count,
        # Chunk H source-grounding evidence + verification profile. All
        # are optional on the way in so legacy payloads (pre-Chunk H)
        # still deserialize with safe defaults.
        "searched_sources": list(result.searched_sources),
        "cited_sources": list(result.cited_sources),
        "accepted_sources": list(result.accepted_sources),
        "rejected_sources": [dict(r) for r in result.rejected_sources],
        "verification_profile": result.verification_profile,
        # Chunk I: verification mode round-trips through resume state so
        # a session resumed after a crash still reports each finding's
        # original routing decision. Pre-Chunk-I payloads deserialize
        # with the empty-string default below.
        "verification_mode": result.verification_mode,
    }


def deserialize_verification_result(payload: dict[str, Any] | None) -> VerificationResult | None:
    if not payload:
        return None
    raw_rejected = payload.get("rejected_sources", []) or []
    rejected: list[dict] = []
    for entry in raw_rejected:
        if isinstance(entry, dict):
            url = str(entry.get("url") or "")
            reason = str(entry.get("reason") or "")
            rejected.append({"url": url, "reason": reason})
    return VerificationResult(
        verdict=str(payload.get("verdict", "UNVERIFIED")),
        explanation=str(payload.get("explanation", "")),
        sources=[str(s) for s in payload.get("sources", [])],
        correction=(str(payload["correction"]) if payload.get("correction") is not None else None),
        grounded=bool(payload.get("grounded", False)),
        model_used=str(payload.get("model_used", "")),
        escalated=bool(payload.get("escalated", False)),
        cache_status=str(payload.get("cache_status", "n/a")),
        web_search_requests=int(payload.get("web_search_requests", 0) or 0),
        successful_source_count=int(payload.get("successful_source_count", 0) or 0),
        search_error_count=int(payload.get("search_error_count", 0) or 0),
        searched_sources=[str(s) for s in payload.get("searched_sources", []) if s],
        cited_sources=[str(s) for s in payload.get("cited_sources", []) if s],
        accepted_sources=[str(s) for s in payload.get("accepted_sources", []) if s],
        rejected_sources=rejected,
        verification_profile=str(payload.get("verification_profile", "")),
        verification_mode=str(payload.get("verification_mode", "")),
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
    insert_pos = payload.get("insertPosition")
    if insert_pos is not None:
        normalized_pos = str(insert_pos).strip().lower()
        insert_pos = normalized_pos if normalized_pos in {"before", "after"} else None
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
        insertPosition=insert_pos,
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
        # Phase 8 / plan section 12.1: persist the review mode so resume
        # restores the exact prompt path. Older payloads without this key
        # fall back to the default.
        "review_mode": submission.review_mode,
        "prepared_specs": [serialize_extracted_spec(s) for s in (submission.prepared_specs or [])],
    }


def deserialize_submission(payload: dict[str, Any]) -> BatchSubmission:
    from .review_modes import DEFAULT_REVIEW_MODE, coerce_review_mode

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
        review_mode=coerce_review_mode(payload.get("review_mode", DEFAULT_REVIEW_MODE.value)).value,
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
        "schema": _RESUME_STATE_CURRENT_SCHEMA,
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


def _validate_batch_id(batch_id: Any) -> str:
    """Phase 5.5 (audit Section 9.5): hard-fail malformed batch IDs.

    Anthropic batch IDs are non-empty strings prefixed with ``msgbatch_``.
    Anything else means the resume payload was hand-edited or corrupted —
    accepting it would let polling spin forever against a non-existent
    batch. The GUI loader catches the ValueError and discards the file.
    """
    if not isinstance(batch_id, str) or not batch_id.startswith("msgbatch_"):
        raise ValueError(f"Invalid batch_id in resume payload: {batch_id!r}")
    return batch_id


def _validate_request_map(request_map: Any) -> dict:
    """Resume payload's request_map must round-trip cleanly.

    Phase 5.5: reject malformed shapes (non-dict, non-string keys, missing
    indices) instead of silently accepting them. Polling against a broken
    map would associate results with the wrong findings.
    """
    if not isinstance(request_map, dict) or not request_map:
        raise ValueError("request_map must be a non-empty dict")
    for key, meta in request_map.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"request_map key must be a non-empty string: {key!r}")
        if not isinstance(meta, dict):
            raise ValueError(f"request_map[{key!r}] must be a dict, got {type(meta).__name__}")
    return request_map


def deserialize_resume_state(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Resume payload must be a dict")
    phase = str(payload.get("phase", ""))
    if phase and phase not in SUPPORTED_PHASES:
        raise ValueError(f"Unsupported resume phase: {phase!r}")
    submission_payload = payload.get("submission")
    if not isinstance(submission_payload, dict):
        raise ValueError("Missing submission payload")
    job_payload = submission_payload.get("job")
    if not isinstance(job_payload, dict):
        raise ValueError("Missing submission.job payload")
    _validate_batch_id(job_payload.get("batch_id"))
    _validate_request_map(job_payload.get("request_map"))
    submission = deserialize_submission(submission_payload)
    out: dict[str, Any] = {
        "version": payload.get("version"),
        "schema": payload.get("schema") or _RESUME_STATE_CURRENT_SCHEMA,
        "saved_at": payload.get("saved_at"),
        "phase": phase,
        "submission": submission,
        "resume_flags": payload.get("resume_flags", {}),
    }
    if payload.get("review_findings_payload"):
        out["review_state"] = deserialize_collected_batch_state(payload["review_findings_payload"], submission)
    if payload.get("verification_batch"):
        verification_payload = payload["verification_batch"]
        if not isinstance(verification_payload, dict):
            raise ValueError("verification_batch must be a dict")
        _validate_batch_id(verification_payload.get("batch_id"))
        out["verification_batch"] = deserialize_batch_job(verification_payload)
    return out
