"""Durable resume-state serialization helpers.

Schema retention policy
-----------------------
Every saved payload carries a ``schema`` string. The current writer always
emits :data:`_RESUME_STATE_CURRENT_SCHEMA`; the reader rejects anything older
than :data:`_RESUME_STATE_MINIMUM_SCHEMA`. The minimum is intentionally lagged
behind the current value so an in-flight batch from the previous release can
still resume after an upgrade.

The retirement workflow is:

1. When a field becomes mandatory (no more sensible default), bump the current
   schema to a new value and start writing it.
2. Leave the minimum schema at the previous value for one minor release so
   in-flight batches still load.
3. In the next release, raise the minimum to the new value and delete the
   optional-field fallbacks below.

The on-disk age cutoff in ``app_paths.BATCH_STATE_MAX_AGE_HOURS`` (currently
28 days) provides a hard upper bound — any saved state older than that is
discarded regardless of schema.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..batch.batch import BatchJob
from ..core.code_cycles import DEFAULT_CYCLE
from ..input.extractor import ExtractedSpec
from .pipeline import BatchSubmission, CollectedBatchState
from ..review.reviewer import EDIT_ACTION_TYPES, EditProposal, Finding, MODEL_OPUS_47, ReviewResult
from ..verification.verifier import VerificationResult

_log = logging.getLogger(__name__)


def _content_digest(content: str) -> str:
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

_RESUME_STATE_CURRENT_SCHEMA = "v3"
_RESUME_STATE_MINIMUM_SCHEMA = "v2"

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
        "content_sha256": _content_digest(spec.content),
        "source_sha256": _source_file_digest(spec.source_path),
        # Chunk 10 / Trust Upgrade: the extraction warning list survives
        # resume so a resumed run keeps the banner accurate without
        # re-reading the source DOCX (the source file may have changed
        # on disk; the saved warning is the one the original extraction
        # produced).
        "extraction_warnings": list(spec.extraction_warnings),
    }


def deserialize_extracted_spec(payload: dict[str, Any]) -> ExtractedSpec:
    raw_warnings = payload.get("extraction_warnings", []) or []
    spec = ExtractedSpec(
        filename=str(payload["filename"]),
        content=str(payload.get("content", "")),
        word_count=int(payload.get("word_count", 0)),
        source_path=str(payload.get("source_path", "")),
        source_format=str(payload.get("source_format", "unknown")),
        # Chunk 10 / Trust Upgrade: defaults to an empty list for legacy
        # state files written before the field existed (those payloads
        # predate the content-loss warning — leaving the list empty is
        # the safe fallback that preserves the original banner shape).
        extraction_warnings=[str(w) for w in raw_warnings if w],
    )
    # Warn when the on-disk file no longer matches the saved digest. Resume
    # continues — the saved content is still authoritative for the in-flight
    # batch — but the user is told the file drifted.
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
        "grounded": result.grounded,
        "model_used": result.model_used,
        "escalated": result.escalated,
        "cache_status": result.cache_status,
        "web_search_requests": result.web_search_requests,
        "successful_source_count": result.successful_source_count,
        "search_error_count": result.search_error_count,
        "searched_sources": list(result.searched_sources),
        "cited_sources": list(result.cited_sources),
        "accepted_sources": list(result.accepted_sources),
        "rejected_sources": [dict(r) for r in result.rejected_sources],
        "verification_profile": result.verification_profile,
        "verification_mode": result.verification_mode,
        "source_quote": result.source_quote,
        # Chunk 3 / Trust Upgrade: the operational-failure sentinel must
        # survive resume so a resumed report renders VERIFICATION_FAILED
        # for the same findings that originally hit a transient error.
        "verification_failed": bool(result.verification_failed),
        # Chunk 5 / Trust Upgrade: the cache-entry creation timestamp
        # must survive resume so a resumed report renders the same
        # "Cache replay — Nd old" badge the original run would have
        # shown. Stored as epoch seconds.
        "cache_entry_created_ts": float(result.cache_entry_created_ts),
        # Chunk 10 / Trust Upgrade: the elevated-confidence flag is
        # router-derived runtime telemetry, but it must survive resume
        # so a resumed report applies the same composite-confidence
        # multiplier the original run would have used. Local-skip
        # results never reach the verification cache (they aren't
        # grounded), so no cache schema bump is needed — just resume.
        "requires_elevated_confidence": bool(result.requires_elevated_confidence),
        # Chunk 11 / Trust Upgrade: fetch telemetry must round-trip
        # through resume so a resumed report renders the same
        # "Searches: N, Full-page fetches: M" line and the same
        # "Full-text sources consulted" sub-section the original run
        # would have shown.
        "web_fetch_requests": int(result.web_fetch_requests),
        "fetched_sources": list(result.fetched_sources),
        # Chunk 12 / Trust Upgrade: the models-disagreed sentinel and
        # the initial verifier's citations must survive resume so a
        # resumed report renders VERIFIED_CONTESTED for the same
        # findings the original run flagged, with the same
        # initial-verdict citations displayed alongside the final
        # verdict's sources in the evidence panel.
        "models_disagreed": bool(result.models_disagreed),
        "initial_sources": list(result.initial_sources),
        # Chunk 13 / Trust Upgrade: the budget-exhausted sentinel must
        # survive resume so a resumed report renders the same
        # "Insufficient evidence (search budget exhausted)" sub-label
        # the original run would have produced. Runtime telemetry —
        # the verification cache refuses to persist these results so
        # a cache replay never carries the flag, but resume state
        # carries the full in-memory pipeline state and must keep it.
        "budget_exhausted": bool(result.budget_exhausted),
        # Token usage telemetry — carried through resume so a resumed run's
        # diagnostics keep the spend already attributed to verified findings.
        "input_tokens": int(result.input_tokens),
        "output_tokens": int(result.output_tokens),
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
        # Chunk 2 / Trust Upgrade: resume-state schema is unversioned so
        # missing values default to empty string for backward compatibility
        # with state files written before the field was added.
        source_quote=str(payload.get("source_quote", "") or ""),
        # Chunk 3 / Trust Upgrade: defaults to False for legacy state
        # files written before the sentinel existed (those findings
        # render as INSUFFICIENT_EVIDENCE / their verdict — the safe
        # fallback, since we cannot retroactively know whether the
        # verifier crashed at the time the state was saved).
        verification_failed=bool(payload.get("verification_failed", False)),
        # Chunk 5 / Trust Upgrade: defaults to 0.0 for legacy state files
        # written before the field existed (those resume payloads predate
        # the cache-age badge — rendering "Cache replay (age unknown)" is
        # the safe fallback when the original timestamp was never stored).
        cache_entry_created_ts=float(payload.get("cache_entry_created_ts", 0.0) or 0.0),
        # Chunk 10 / Trust Upgrade: defaults to False for legacy state
        # files written before the field existed (those payloads predate
        # the elevated-confidence multiplier — leaving the multiplier
        # neutral at 1.0 is the safe fallback that preserves the
        # original auto-edit gating decision).
        requires_elevated_confidence=bool(payload.get("requires_elevated_confidence", False)),
        # Chunk 11 / Trust Upgrade: legacy state files predating the
        # web_fetch capability default to 0 / [] so the evidence panel
        # simply omits the fetch count for those resumed findings rather
        # than crashing on a missing key.
        web_fetch_requests=int(payload.get("web_fetch_requests", 0) or 0),
        fetched_sources=[str(s) for s in payload.get("fetched_sources", []) if s],
        # Chunk 12 / Trust Upgrade: legacy state files predating the
        # contested-status field default to False / [] so resumed
        # findings classify through the regular verdict-based branches
        # rather than retroactively claiming the verifiers disagreed.
        models_disagreed=bool(payload.get("models_disagreed", False)),
        initial_sources=[str(s) for s in payload.get("initial_sources", []) if s],
        # Chunk 13 / Trust Upgrade: defaults to False for legacy state
        # files written before the sentinel existed (those resume
        # payloads predate the budget-exhausted sub-label — leaving the
        # flag False keeps the INSUFFICIENT_EVIDENCE rendering exactly
        # as it was on the original run).
        budget_exhausted=bool(payload.get("budget_exhausted", False)),
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
    )


def serialize_edit_proposal(proposal: EditProposal | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    return {
        "action_type": proposal.action_type,
        "existing_text": proposal.existing_text,
        "replacement_text": proposal.replacement_text,
        "anchor_text": proposal.anchor_text,
        "insert_position": proposal.insert_position,
        "target_element_id": proposal.target_element_id,
        "edit_confidence": proposal.edit_confidence,
    }


def deserialize_edit_proposal(payload: dict[str, Any] | None) -> EditProposal | None:
    if not payload:
        return None
    insert_pos_raw = payload.get("insert_position")
    if insert_pos_raw is not None:
        normalized = str(insert_pos_raw).strip().lower()
        insert_pos: str | None = normalized if normalized in {"before", "after"} else None
    else:
        insert_pos = None
    return EditProposal(
        action_type=str(payload.get("action_type", "EDIT")).strip().upper(),
        existing_text=(
            str(payload["existing_text"]) if payload.get("existing_text") is not None else None
        ),
        replacement_text=(
            str(payload["replacement_text"]) if payload.get("replacement_text") is not None else None
        ),
        anchor_text=(
            str(payload["anchor_text"]) if payload.get("anchor_text") is not None else None
        ),
        insert_position=insert_pos,
        target_element_id=(
            str(payload["target_element_id"])
            if payload.get("target_element_id") is not None
            else None
        ),
        edit_confidence=float(payload.get("edit_confidence", 0.5)),
    )


def serialize_finding(finding: Finding, *, _include_originals: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
        "evidenceElementId": finding.evidenceElementId,
        "edit_proposal": serialize_edit_proposal(finding.edit_proposal),
        "finding_id": finding.finding_id,
        "upstream_finding_ids": list(finding.upstream_finding_ids),
        "independent_evidence_ids": list(finding.independent_evidence_ids),
        "suppression_reason": finding.suppression_reason,
        "demotion_reason": finding.demotion_reason,
        # Chunk 4 / Trust Upgrade: locator evidence (match method,
        # confidence, safety category, element id) so a resumed run
        # exporting the report after edits were applied can still
        # render the "Edit Target Evidence" panel.
        "locator_evidence": (
            dict(finding.locator_evidence)
            if finding.locator_evidence is not None
            else None
        ),
    }
    # Per-file pre-merge originals. The originals are themselves singleton
    # findings (their own ``occurrence_originals`` is empty), so the recursion
    # is bounded at one level — ``_include_originals=False`` makes that
    # explicit and prevents infinite nesting if a future code path nests
    # merged findings.
    if _include_originals:
        payload["occurrence_originals"] = [
            serialize_finding(orig, _include_originals=False)
            for orig in finding.occurrence_originals
        ]
    else:
        payload["occurrence_originals"] = []
    return payload


def deserialize_finding(payload: dict[str, Any], *, _include_originals: bool = True) -> Finding:
    insert_pos = payload.get("insertPosition")
    if insert_pos is not None:
        normalized_pos = str(insert_pos).strip().lower()
        insert_pos = normalized_pos if normalized_pos in {"before", "after"} else None
    evidence_id_raw = payload.get("evidenceElementId")
    if evidence_id_raw is None:
        evidence_id: str | None = None
    else:
        evidence_id = str(evidence_id_raw).strip() or None
    proposal = deserialize_edit_proposal(payload.get("edit_proposal"))
    action_type = str(payload.get("actionType", "EDIT"))
    # Schema-v2 fallback: payloads written before ``edit_proposal`` became a
    # first-class field synthesize one from the legacy top-level edit fields so
    # the loaded Finding behaves like a freshly-parsed one. Retire with v2.
    if proposal is None and action_type.strip().upper() in EDIT_ACTION_TYPES:
        proposal = EditProposal(
            action_type=action_type.strip().upper(),
            existing_text=(
                str(payload["existingText"]) if payload.get("existingText") is not None else None
            ),
            replacement_text=(
                str(payload["replacementText"])
                if payload.get("replacementText") is not None
                else None
            ),
            anchor_text=(
                str(payload["anchorText"]) if payload.get("anchorText") is not None else None
            ),
            insert_position=insert_pos,
            target_element_id=evidence_id,
            edit_confidence=float(payload.get("confidence", 0.5)),
        )
    upstream_ids_raw = payload.get("upstream_finding_ids", []) or []
    upstream_ids = [str(uid).strip() for uid in upstream_ids_raw if str(uid).strip()]
    independent_ids_raw = payload.get("independent_evidence_ids", []) or []
    independent_ids = [str(eid).strip() for eid in independent_ids_raw if str(eid).strip()]
    suppression_raw = payload.get("suppression_reason")
    suppression_reason = str(suppression_raw) if suppression_raw is not None else None
    demotion_raw = payload.get("demotion_reason")
    demotion_reason = str(demotion_raw) if demotion_raw is not None else None
    if _include_originals:
        originals_raw = payload.get("occurrence_originals", []) or []
        occurrence_originals = [
            deserialize_finding(o, _include_originals=False)
            for o in originals_raw
            if isinstance(o, dict)
        ]
    else:
        occurrence_originals = []
    # Chunk 4 / Trust Upgrade: locator evidence is an opaque dict with
    # known string/float keys. Coerce values defensively so a malformed
    # resume payload cannot blow up the deserializer; missing/invalid
    # values fall back to ``None`` (the report will simply skip the
    # locator panel for that finding).
    locator_evidence_raw = payload.get("locator_evidence")
    if isinstance(locator_evidence_raw, dict):
        locator_evidence: dict | None = {
            "status": str(locator_evidence_raw.get("status", "") or ""),
            "match_method": str(locator_evidence_raw.get("match_method", "") or ""),
            "match_confidence": float(
                locator_evidence_raw.get("match_confidence", 0.0) or 0.0
            ),
            "safety_category": str(
                locator_evidence_raw.get("safety_category", "") or ""
            ),
            "element_id": str(locator_evidence_raw.get("element_id", "") or ""),
        }
    else:
        locator_evidence = None
    return Finding(
        severity=str(payload.get("severity", "MEDIUM")),
        fileName=str(payload.get("fileName", "")),
        section=str(payload.get("section", "")),
        issue=str(payload.get("issue", "")),
        actionType=action_type,
        existingText=(str(payload["existingText"]) if payload.get("existingText") is not None else None),
        replacementText=(str(payload["replacementText"]) if payload.get("replacementText") is not None else None),
        codeReference=(str(payload["codeReference"]) if payload.get("codeReference") is not None else None),
        confidence=float(payload.get("confidence", 0.5)),
        affected_files=[str(v) for v in payload.get("affected_files", [])],
        verification=deserialize_verification_result(payload.get("verification")),
        anchorText=(str(payload["anchorText"]) if payload.get("anchorText") is not None else None),
        insertPosition=insert_pos,
        evidenceElementId=evidence_id,
        edit_proposal=proposal,
        finding_id=str(payload.get("finding_id", "")),
        upstream_finding_ids=upstream_ids,
        independent_evidence_ids=independent_ids,
        suppression_reason=suppression_reason,
        demotion_reason=demotion_reason,
        occurrence_originals=occurrence_originals,
        locator_evidence=locator_evidence,
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
        "suppressed_findings": [serialize_finding(f) for f in result.suppressed_findings],
    }


def deserialize_review_result(payload: dict[str, Any] | None) -> ReviewResult | None:
    if not payload:
        return None
    return ReviewResult(
        findings=[deserialize_finding(f) for f in payload.get("findings", [])],
        raw_response=str(payload.get("raw_response", "")),
        thinking=str(payload.get("thinking", "")),
        model=str(payload.get("model", MODEL_OPUS_47)),
        input_tokens=int(payload.get("input_tokens", 0)),
        output_tokens=int(payload.get("output_tokens", 0)),
        elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
        error=(str(payload["error"]) if payload.get("error") is not None else None),
        stop_reason=(str(payload["stop_reason"]) if payload.get("stop_reason") is not None else None),
        parse_status=(str(payload["parse_status"]) if payload.get("parse_status") is not None else None),
        cross_check_status=(str(payload["cross_check_status"]) if payload.get("cross_check_status") is not None else None),
        suppressed_findings=[
            deserialize_finding(f) for f in payload.get("suppressed_findings", []) or []
        ],
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
        "prepared_specs": [serialize_extracted_spec(s) for s in (submission.prepared_specs or [])],
        "code_cycle_alerts": list(submission.code_cycle_alerts),
        "structural_alerts": list(submission.structural_alerts),
        "naming_alerts": list(submission.naming_alerts),
        "template_marker_alerts": list(submission.template_marker_alerts),
        "invalid_code_cycle_alerts": list(submission.invalid_code_cycle_alerts),
        "duplicate_paragraph_alerts": list(submission.duplicate_paragraph_alerts),
        # Tracing: carry the batch-mode pipeline span_id so a resumed run
        # can close the same root span. Empty string when tracing was off
        # at submit time; legacy payloads (no key) deserialize to "".
        "trace_span_id": getattr(submission, "trace_span_id", "") or "",
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
        model=str(payload.get("model", MODEL_OPUS_47)),
        project_context=str(payload.get("project_context", "")),
        cycle_label=str(payload.get("code_cycle", DEFAULT_CYCLE.label)),
        cross_check_enabled=bool(payload.get("cross_check_enabled", False)),
        prepared_specs=prepared_specs,
        code_cycle_alerts=list(payload.get("code_cycle_alerts", [])),
        structural_alerts=list(payload.get("structural_alerts", [])),
        naming_alerts=list(payload.get("naming_alerts", [])),
        template_marker_alerts=list(payload.get("template_marker_alerts", [])),
        invalid_code_cycle_alerts=list(payload.get("invalid_code_cycle_alerts", [])),
        duplicate_paragraph_alerts=list(payload.get("duplicate_paragraph_alerts", [])),
        trace_span_id=str(payload.get("trace_span_id", "") or ""),
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
        "code_cycle_alerts": list(state.code_cycle_alerts),
        "structural_alerts": list(state.structural_alerts),
        "naming_alerts": list(state.naming_alerts),
        "template_marker_alerts": list(state.template_marker_alerts),
        "invalid_code_cycle_alerts": list(state.invalid_code_cycle_alerts),
        "duplicate_paragraph_alerts": list(state.duplicate_paragraph_alerts),
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
        code_cycle_alerts=list(payload.get("code_cycle_alerts", submission.code_cycle_alerts)),
        structural_alerts=list(payload.get("structural_alerts", submission.structural_alerts)),
        naming_alerts=list(payload.get("naming_alerts", submission.naming_alerts)),
        template_marker_alerts=list(payload.get("template_marker_alerts", submission.template_marker_alerts)),
        invalid_code_cycle_alerts=list(payload.get("invalid_code_cycle_alerts", submission.invalid_code_cycle_alerts)),
        duplicate_paragraph_alerts=list(payload.get("duplicate_paragraph_alerts", submission.duplicate_paragraph_alerts)),
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
    # Tracing: persist enough to reopen the SAME trace directory on an
    # app-restart resume so the whole batch run lands in one trace. Read
    # from the active recorder (global singleton); absent when tracing is
    # off. Purely additive — legacy loaders ignore unknown keys.
    try:
        from ..tracing import get_recorder as _get_recorder
        _rec = _get_recorder()
        if _rec is not None:
            state["trace"] = {
                "run_id": _rec.run_id,
                "trace_dir": str(_rec.trace_dir),
                "capture_level": _rec.capture_level,
            }
    except Exception:
        pass
    return state


def _validate_schema(schema_value: Any) -> str:
    """Reject payloads whose schema is below :data:`_RESUME_STATE_MINIMUM_SCHEMA`.

    Schemas are simple ``vN`` strings with monotonically increasing N. A
    missing schema field means the payload predates schema tagging entirely
    and is unconditionally rejected — the loader will discard the file.
    """
    if not isinstance(schema_value, str) or not schema_value:
        raise ValueError("Resume payload missing schema field — too old to load")
    try:
        current = int(schema_value.lstrip("v"))
        minimum = int(_RESUME_STATE_MINIMUM_SCHEMA.lstrip("v"))
    except ValueError as exc:
        raise ValueError(f"Resume payload has malformed schema: {schema_value!r}") from exc
    if current < minimum:
        raise ValueError(
            f"Resume schema {schema_value!r} is no longer supported; "
            f"minimum is {_RESUME_STATE_MINIMUM_SCHEMA!r}"
        )
    return schema_value


def _validate_batch_id(batch_id: Any) -> str:
    """Hard-fail malformed batch IDs.

    Anthropic batch IDs are non-empty strings prefixed with ``msgbatch_``.
    Anything else means the resume payload was hand-edited or corrupted —
    accepting it would let polling spin forever against a non-existent
    batch. The GUI loader catches the ValueError and discards the file.
    """
    if not isinstance(batch_id, str) or not batch_id.startswith("msgbatch_"):
        raise ValueError(f"Invalid batch_id in resume payload: {batch_id!r}")
    return batch_id


def _validate_request_map(request_map: Any) -> dict:
    """Reject malformed request_map shapes.

    Polling against a broken map would associate results with the wrong
    findings, so non-dict / empty / non-string-keyed maps fail outright.
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
    _validate_schema(payload.get("schema"))
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
        "schema": payload.get("schema"),
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
    # Tracing reattach info (optional — absent on legacy payloads / runs
    # where tracing was disabled). The GUI resume path uses this to
    # reopen the same trace directory so an app-restart resume appends to
    # the original run rather than starting a fresh trace.
    trace_meta = payload.get("trace")
    if isinstance(trace_meta, dict) and trace_meta.get("run_id"):
        out["trace"] = {
            "run_id": str(trace_meta.get("run_id")),
            "trace_dir": str(trace_meta.get("trace_dir", "")),
            "capture_level": str(trace_meta.get("capture_level", "default")),
        }
    return out
