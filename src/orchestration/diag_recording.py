"""Tk-free diagnostics recording for the collect-stage results.

Verbatim extractions of the post-hoc recording the GUI's single-module
collect branch performed inline (``gui.batch_controller``), so the routed
program path can record the SAME telemetry rows over each child
``PipelineResult`` — previously a program run recorded zero
verification/cross-check/compliance telemetry and its summary token totals
equaled research + review exactly.

Every function is defensive: ``diag`` is a duck-typed ``DiagnosticsReport``
(``log`` / ``record_api_call``), and a ``None`` diag or ``None`` result is a
no-op. Key-for-key identical event rows to the GUI's originals — the
diagnostics summary's per-phase rollups, verdict tallies, and token totals
key off these exact shapes.
"""
from __future__ import annotations

from .diagnostics import bound_structured_payload


def record_review_collect(diag, review_result, *, transport: str) -> None:
    """Record the review-collection outcome (one aggregate row).

    Batch transport records a ``record_api_call`` row carrying the combined
    usage; the real-time transport already recorded one row PER SPEC as the
    streams completed, so it records only a success line (double-count
    guard).
    """
    if diag is None or review_result is None:
        return
    rv = review_result
    if transport == "realtime":
        diag.log("batch_collect", "success", "Review results collected (real-time)", {
            "total_findings": rv.total_count,
        })
    else:
        diag.record_api_call(
            phase="batch_collect",
            model=rv.model,
            level="success",
            message="Review results collected",
            input_tokens=rv.input_tokens,
            output_tokens=rv.output_tokens,
            cache_creation_input_tokens=rv.cache_creation_input_tokens,
            cache_read_input_tokens=rv.cache_read_input_tokens,
            stop_reason=rv.stop_reason,
            mode="batch",
            retry_status="initial",
            structured_payload=rv.structured_payload,
            extra={
                "elapsed_seconds": round(rv.elapsed_seconds, 2),
                "parse_status": rv.parse_status,
                "severity_counts": {
                    "CRITICAL": rv.critical_count, "HIGH": rv.high_count,
                    "MEDIUM": rv.medium_count, "GRIPES": rv.gripe_count,
                },
                "total_findings": rv.total_count,
            },
        )
    if rv.error:
        diag.log("batch_collect", "error", f"Review errors: {rv.error}")


def record_verification_findings(
    diag, findings, *, transport: str, phase: str = "verification"
) -> None:
    """Record one row per verified finding plus the verdict-tally summary."""
    if diag is None or not findings:
        return
    verdicts: dict[str, int] = {}
    for f in findings:
        if f.verification:
            v = f.verification.verdict
            verdicts[v] = verdicts.get(v, 0) + 1
            event_data = {
                "verdict": f.verification.verdict,
                "finding_severity": f.severity,
                "confidence": f.confidence,
                "explanation": f.verification.explanation or "",
                # Surface the routing decision
                # so the diagnostics summary can report
                # how many findings each mode handled.
                "verification_mode": f.verification.verification_mode,
                "verification_profile": f.verification.verification_profile,
                "grounded": f.verification.grounded,
                "cache_status": f.verification.cache_status,
                "escalated": f.verification.escalated,
                # Escalation telemetry —
                # whether a second pass ran and whether
                # it changed the verdict, so the summary
                # can report "did escalation pay off?".
                "escalation_attempted": f.verification.escalation_attempted,
                "initial_model": f.verification.initial_model,
                "initial_verdict": f.verification.initial_verdict,
                "escalation_changed_verdict": f.verification.escalation_changed_verdict,
                "escalation_reason": f.verification.escalation_reason,
                # Tag remote verifications with the
                # transport that actually ran so the
                # per-phase rollup's call_mode counters
                # reflect the real path.
                "api_call": f.verification.cache_status not in ("hit", "local_skip"),
                "call_mode": transport,
                "model": f.verification.model_used,
                "web_search_requests": f.verification.web_search_requests,
                # Token usage so the per-phase diagnostics
                # rollup reports real verification spend
                # (previously absent, so verification showed
                # in=0/out=0). Cache-hit / local-skip results
                # carry 0 here (no API call ran), which is the
                # correct contribution to this-run spend.
                "input_tokens": f.verification.input_tokens,
                "output_tokens": f.verification.output_tokens,
                # Surface retry telemetry so the
                # per-phase diagnostics rollup can answer
                # "which findings burned retries / hit
                # the continuation cap?".
                "retry_telemetry": f.verification.retry_telemetry,
            }
            bounded_payload = bound_structured_payload(f.verification.structured_payload)
            if bounded_payload is not None:
                event_data["structured_payload"] = bounded_payload
            diag.log(phase, "info",
                f"Verified: {f.fileName} — {f.verification.verdict}", event_data)
    if phase == "verification":
        diag.log(phase, "success", "Verification complete", {"verdicts": verdicts})
    else:
        diag.log(phase, "success", "Cross-check verification complete", {"verdicts": verdicts})


def record_cross_check(diag, cross_check_result) -> None:
    """Record the cross-check pass outcome (one aggregate row)."""
    if diag is None or cross_check_result is None:
        return
    cc = cross_check_result
    # The cross-check pass always runs as a live
    # (synchronous) call, so the call_mode reflects that
    # rather than the batch review phase.
    diag.record_api_call(
        phase="cross_check",
        model=cc.model,
        message=f"Cross-check: {cc.cross_check_status}",
        input_tokens=cc.input_tokens,
        output_tokens=cc.output_tokens,
        cache_creation_input_tokens=cc.cache_creation_input_tokens,
        cache_read_input_tokens=cc.cache_read_input_tokens,
        stop_reason=cc.stop_reason,
        mode="realtime",
        retry_status="initial",
        structured_payload=cc.structured_payload,
        extra={"finding_count": len(cc.findings)},
    )


def record_compliance(diag, compliance_result) -> None:
    """Record the compliance pass outcome (one aggregate row)."""
    if diag is None or compliance_result is None:
        return
    comp = compliance_result
    diag.record_api_call(
        phase="compliance",
        model=comp.model,
        message=f"Compliance: {comp.cross_check_status}",
        input_tokens=comp.input_tokens,
        output_tokens=comp.output_tokens,
        cache_creation_input_tokens=comp.cache_creation_input_tokens,
        cache_read_input_tokens=comp.cache_read_input_tokens,
        stop_reason=comp.stop_reason,
        mode="realtime",
        retry_status="initial",
        structured_payload=comp.structured_payload,
        extra={
            "finding_count": len(comp.findings),
            "coverage_count": len(getattr(comp, "coverage", []) or []),
        },
    )
