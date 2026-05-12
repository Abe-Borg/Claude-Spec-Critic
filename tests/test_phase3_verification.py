"""Phase 3 verification redesign tests.

Covers:
- VerificationResult evidence model (plan 7.5).
- Per-run verification cache (plan 7.2).
- Local pre-classification skip (plan 7.3).
- Sonnet → Opus escalation routing (plan 7.1).
- Real-time fallback for small retry tails (plan 7.4).
"""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

from src.code_cycles import DEFAULT_CYCLE
from src.reviewer import Finding


def _make_finding(
    *,
    severity: str = "HIGH",
    code_ref: str | None = "CBC 2025",
    issue: str = "Cited code edition is outdated",
    existing: str | None = "per CBC 2019",
    replacement: str | None = "per CBC 2025",
    action: str = "EDIT",
) -> Finding:
    return Finding(
        severity=severity,
        fileName="23 21 13 - Hydronic.docx",
        section="2.1",
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=0.6,
    )


# ---------------------------------------------------------------------------
# 7.3 Local pre-classification
# ---------------------------------------------------------------------------


def test_local_skip_enabled_by_default(monkeypatch):
    # Phase 3.3 (audit Section 7.3): default is now ON so placeholder/LEED/
    # internal-contradiction GRIPES never spend a web search.
    monkeypatch.delenv("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", raising=False)
    from src.verification_router import classify_finding_for_verification, local_skip_enabled
    assert local_skip_enabled() is True
    f = _make_finding(severity="GRIPES", code_ref=None, issue="placeholder text [SELECT]")
    assert classify_finding_for_verification(f) == "local_skip"


def test_local_skip_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", "0")
    from src.verification_router import classify_finding_for_verification, local_skip_enabled
    assert local_skip_enabled() is False
    f = _make_finding(severity="GRIPES", code_ref=None, issue="placeholder text [SELECT]")
    assert classify_finding_for_verification(f) == "web_required"


def test_local_skip_categorises_placeholder_gripes(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", "1")
    from src.verification_router import classify_finding_for_verification
    f = _make_finding(severity="GRIPES", code_ref=None, issue="placeholder text [SELECT] not filled in")
    assert classify_finding_for_verification(f) == "local_skip"


def test_local_skip_keeps_code_reference_findings(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", "1")
    from src.verification_router import classify_finding_for_verification
    # Has a code reference — must still go to web verification.
    f = _make_finding(severity="GRIPES", code_ref="CBC 2025", issue="placeholder")
    assert classify_finding_for_verification(f) == "web_required"


def test_local_skip_keeps_high_severity_even_without_code_ref(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", "1")
    from src.verification_router import classify_finding_for_verification
    f = _make_finding(severity="HIGH", code_ref=None, issue="placeholder TBD")
    assert classify_finding_for_verification(f) == "web_required"


def test_prepare_findings_for_verification_resolves_local_and_cached(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", "1")
    from src.verifier import VerificationResult, prepare_findings_for_verification
    from src.verification_cache import VerificationCache

    cache = VerificationCache()
    cached_finding = _make_finding(issue="cached claim")
    # Chunk 5: cached CONFIRMED must carry an accepted citation. The
    # cache silently refuses to store CONFIRMED/CORRECTED results
    # without one (the strengthened invariant), so the test asserts the
    # cache-hit path with a well-formed source-bearing entry.
    cache.put(
        cached_finding, cycle=DEFAULT_CYCLE,
        result=VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://dgs.ca.gov"],
        ),
    )

    skip_finding = _make_finding(severity="GRIPES", code_ref=None, issue="placeholder TBD content")
    needs_web = _make_finding(issue="needs grounding")

    findings = [skip_finding, cached_finding, needs_web]
    remaining = prepare_findings_for_verification(findings, cycle=DEFAULT_CYCLE, cache=cache)

    assert remaining == [needs_web]
    assert skip_finding.verification is not None
    assert skip_finding.verification.cache_status == "local_skip"
    assert cached_finding.verification is not None
    assert cached_finding.verification.cache_status == "hit"
    assert needs_web.verification is None


# ---------------------------------------------------------------------------
# 7.1 Sonnet default + Opus escalation
# ---------------------------------------------------------------------------


def test_sonnet_default_on_by_default(monkeypatch):
    # Phase 2.7 (audit Section 6.7): Sonnet is the default verifier so
    # cost/latency drops without losing quality on CRITICAL/HIGH findings
    # (those escalate to Opus via :func:`should_escalate_verification`).
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_MODEL", raising=False)
    import src.api_config as api_config
    importlib.reload(api_config)
    assert api_config.VERIFICATION_MODEL_DEFAULT == api_config.MODEL_SONNET_46


def test_sonnet_default_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "0")
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_MODEL", raising=False)
    import src.api_config as api_config
    importlib.reload(api_config)
    # Opus-everywhere fallback now points at Opus 4.7.
    assert api_config.VERIFICATION_MODEL_DEFAULT == api_config.MODEL_OPUS_47


def test_should_escalate_for_critical_when_sonnet_default(monkeypatch):
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_MODEL", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", raising=False)
    import src.api_config as api_config
    import src.verification_router as router
    importlib.reload(api_config)
    importlib.reload(router)
    f = _make_finding(severity="CRITICAL")
    assert router.should_escalate_verification(
        f, verdict="UNVERIFIED", grounded=False,
        successful_source_count=0, search_error_count=0,
    ) is True


def test_should_not_escalate_when_sonnet_default_disabled(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "0")
    import src.api_config as api_config
    import src.verification_router as router
    importlib.reload(api_config)
    importlib.reload(router)
    f = _make_finding(severity="CRITICAL")
    # Flag off — initial verifier is already Opus, nowhere to escalate to.
    assert router.should_escalate_verification(
        f, verdict="UNVERIFIED", grounded=False,
        successful_source_count=0, search_error_count=0,
    ) is False


def test_should_escalate_for_high_severity_unverified(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "1")
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_MODEL", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", raising=False)
    import src.api_config as api_config
    import src.verification_router as router
    importlib.reload(api_config)
    importlib.reload(router)
    f = _make_finding(severity="HIGH")
    assert router.should_escalate_verification(
        f, verdict="UNVERIFIED", grounded=False,
        successful_source_count=0, search_error_count=0,
    ) is True


def test_should_not_escalate_low_severity(monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "1")
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_MODEL", raising=False)
    import src.api_config as api_config
    import src.verification_router as router
    importlib.reload(api_config)
    importlib.reload(router)
    f = _make_finding(severity="MEDIUM")
    assert router.should_escalate_verification(
        f, verdict="UNVERIFIED", grounded=False,
        successful_source_count=0, search_error_count=0,
    ) is False


# ---------------------------------------------------------------------------
# 7.4 Real-time fallback for small retry tails
# ---------------------------------------------------------------------------


def _make_request_meta(finding_idx: int, model: str = "claude-opus-4-6") -> dict:
    return {"finding_idx": finding_idx, "model": model}


class _FakeResultObj:
    """Minimal stand-in for an Anthropic batch result."""
    def __init__(self, *, type_: str = "errored", message=None):
        self.result = SimpleNamespace(type=type_, message=message, error=None)


def test_realtime_fallback_calls_verify_finding_for_small_tail(monkeypatch):
    """When max waves end with <= threshold unresolved findings and the
    realtime-fallback threshold is set, call verify_finding instead of
    submitting another batch wave."""
    from src import verifier
    from src.verifier import VerificationResult, collect_verification_batch_results
    from src.batch import BatchJob

    findings = [_make_finding(issue=f"claim {i}") for i in range(2)]
    job = BatchJob(
        batch_id="msgbatch_test",
        job_type="verify",
        request_map={
            "verify__0": {"finding_idx": 0, "model": "claude-opus-4-6"},
            "verify__1": {"finding_idx": 1, "model": "claude-opus-4-6"},
        },
        created_at=0.0,
    )

    # Force every wave to keep both findings unresolved (errored result).
    monkeypatch.setattr(
        verifier, "retrieve_verification_results_detailed",
        lambda _job: {cid: _FakeResultObj(type_="errored") for cid in _job.request_map},
    )
    # Skip real polling.
    monkeypatch.setattr(
        verifier, "poll_batch_bounded",
        lambda batch_id, *, policy, log, progress_cb: SimpleNamespace(detached=False, poll_failed=False),
    )
    # Pretend follow-up waves return the same job structure.
    monkeypatch.setattr(
        verifier, "submit_verification_followup_wave",
        lambda reqs, request_map: BatchJob(batch_id="msgbatch_followup", job_type="verify", request_map=request_map, created_at=0.0),
    )

    # Stub verify_finding to verify it gets called for the tail.
    calls: list[Finding] = []
    def _fake_verify_finding(finding, *, cycle=DEFAULT_CYCLE, cache=None):
        calls.append(finding)
        return VerificationResult(verdict="CONFIRMED", grounded=True, sources=["https://example.com"], model_used="claude-opus-4-6")
    monkeypatch.setattr(verifier, "verify_finding", _fake_verify_finding)

    collect_verification_batch_results(
        job, findings,
        max_waves=2,
        realtime_fallback_threshold=5,
    )

    assert len(calls) == 2, "real-time fallback should run for both unresolved findings"
    for f in findings:
        assert f.verification is not None
        assert f.verification.verdict == "CONFIRMED"


def test_realtime_fallback_disabled_when_threshold_zero(monkeypatch):
    """Threshold=0 (default) preserves prior behavior: terminal UNVERIFIED."""
    from src import verifier
    from src.verifier import collect_verification_batch_results
    from src.batch import BatchJob

    findings = [_make_finding(issue="claim 0")]
    job = BatchJob(
        batch_id="msgbatch_test",
        job_type="verify",
        request_map={"verify__0": {"finding_idx": 0, "model": "claude-opus-4-6"}},
        created_at=0.0,
    )
    monkeypatch.setattr(
        verifier, "retrieve_verification_results_detailed",
        lambda _job: {cid: _FakeResultObj(type_="errored") for cid in _job.request_map},
    )
    monkeypatch.setattr(
        verifier, "poll_batch_bounded",
        lambda batch_id, *, policy, log, progress_cb: SimpleNamespace(detached=False, poll_failed=False),
    )
    monkeypatch.setattr(
        verifier, "submit_verification_followup_wave",
        lambda reqs, request_map: BatchJob(batch_id="msgbatch_followup", job_type="verify", request_map=request_map, created_at=0.0),
    )
    called = []
    monkeypatch.setattr(verifier, "verify_finding", lambda *a, **k: called.append(a) or None)

    collect_verification_batch_results(
        job, findings,
        max_waves=1,
        realtime_fallback_threshold=0,
    )
    assert called == [], "verify_finding must not be invoked when fallback threshold is 0"
    assert findings[0].verification is not None
    assert findings[0].verification.verdict == "UNVERIFIED"


# ---------------------------------------------------------------------------
# Resume-state round-trip (Phase 3 fields persist across save/load)
# ---------------------------------------------------------------------------


def test_resume_state_round_trip_preserves_phase3_fields():
    from src.resume_state import (
        deserialize_verification_result,
        serialize_verification_result,
    )
    from src.verifier import VerificationResult

    original = VerificationResult(
        verdict="CONFIRMED",
        explanation="Backed by DGS",
        sources=["https://dgs.ca.gov"],
        correction=None,
        grounded=True,
        model_used="claude-sonnet-4-6",
        escalated=False,
        cache_status="miss",
        web_search_requests=2,
        successful_source_count=2,
        search_error_count=0,
    )
    payload = serialize_verification_result(original)
    restored = deserialize_verification_result(payload)
    assert restored is not None
    assert restored.verdict == "CONFIRMED"
    assert restored.grounded is True
    assert restored.model_used == "claude-sonnet-4-6"
    assert restored.web_search_requests == 2
    assert restored.successful_source_count == 2


def test_legacy_resume_payload_deserializes_with_safe_defaults():
    from src.resume_state import deserialize_verification_result
    legacy = {"verdict": "UNVERIFIED", "explanation": "x", "sources": [], "correction": None}
    restored = deserialize_verification_result(legacy)
    assert restored is not None
    assert restored.verdict == "UNVERIFIED"
    assert restored.grounded is False  # default
    assert restored.cache_status == "n/a"
    assert restored.model_used == ""
