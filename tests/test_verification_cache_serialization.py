"""Verification-cache serialization policy (plan module M7).

The cache persists a deliberate *subset* of ``VerificationResult``'s fields.
``verification_cache`` drives every projection (to-dict, from-dict on load,
clone-for-store, clone-for-hit) off one allow-list (``_PERSISTED_FIELDS``)
plus an explicit skip-list (``_SKIPPED_FIELDS``), replacing the four
hand-maintained field-by-field copies that used to drift apart.

These tests lock that policy in:

* ``test_field_policy_is_exhaustive`` fails the moment a field is added to
  ``VerificationResult`` without classifying it as persisted or skipped —
  the drift this unification exists to prevent.
* ``test_every_persisted_field_round_trips`` sets a distinctive non-default
  value in every persisted field, saves + reloads through disk, and asserts
  each one survived.
* ``test_skipped_fields_are_not_persisted`` proves skip-list fields are
  dropped on a round-trip (replayed at their dataclass defaults).
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_cache import (
    VerificationCache,
    _PERSISTED_FIELDS,
    _SKIPPED_FIELDS,
    _result_from_dict,
)
from src.verification.verifier import VerificationResult


def _finding() -> Finding:
    return Finding(
        severity="HIGH",
        fileName="Section_22_1000.docx",
        section="2.1",
        issue="Stale code reference",
        actionType="EDIT",
        existingText="2019 edition",
        replacementText="2025 edition",
        codeReference="NFPA 13 §10.2.5",
    )


def _fully_populated_grounded_result() -> VerificationResult:
    """A grounded result with a distinctive non-default in every persisted field."""
    return VerificationResult(
        verdict="CORRECTED",
        explanation="explanation text",
        sources=["https://a.example/1"],
        correction="use the 2025 edition",
        grounded=True,
        model_used="claude-sonnet-4-6",
        escalated=True,
        web_search_requests=3,
        successful_source_count=2,
        search_error_count=1,
        searched_sources=["https://a.example/1", "https://a.example/2"],
        cited_sources=["https://a.example/1"],
        accepted_sources=["https://a.example/1"],
        rejected_sources=[{"url": "https://b.example/x", "reason": "ungrounded"}],
        verification_profile="code_standard",
        verification_mode="deep_reasoning",
        source_quote="The maximum spacing shall not exceed 15 ft.",
        web_fetch_requests=1,
        fetched_sources=["https://a.example/1"],
        models_disagreed=True,
        initial_sources=["https://a.example/initial"],
    )


def test_field_policy_is_exhaustive():
    all_fields = {f.name for f in fields(VerificationResult)}
    classified = _PERSISTED_FIELDS | _SKIPPED_FIELDS
    assert classified == all_fields, (
        "VerificationResult fields missing from the cache serialization "
        f"policy: {all_fields - classified}; classified but no longer on the "
        f"dataclass: {classified - all_fields}"
    )
    # A field must be either persisted or skipped, never both.
    assert not (_PERSISTED_FIELDS & _SKIPPED_FIELDS)


def test_every_persisted_field_round_trips(tmp_path: Path):
    result = _fully_populated_grounded_result()
    finding = _finding()

    cache = VerificationCache()
    cache.put(finding, cycle=DEFAULT_CYCLE, result=result)
    cache_path = tmp_path / "cache.json"
    cache.save_to_disk(cache_path)

    reloaded = VerificationCache()
    reloaded.load_from_disk(cache_path)
    hit = reloaded.get(finding, cycle=DEFAULT_CYCLE)
    assert hit is not None

    for name in _PERSISTED_FIELDS:
        assert getattr(hit, name) == getattr(result, name), name


def test_skipped_fields_are_not_persisted(tmp_path: Path):
    result = _fully_populated_grounded_result()
    # Set skip-list fields to non-defaults. ``verification_failed`` and
    # ``budget_exhausted`` are omitted because ``put`` refuses to cache the
    # results that carry them — their non-persistence is covered by put()'s
    # own guards and tests, not by a round-trip.
    result.escalation_attempted = True
    result.initial_model = "claude-sonnet-4-6"
    result.initial_verdict = "DISPUTED"
    result.escalation_changed_verdict = True
    result.escalation_reason = "ungrounded_critical_high"
    result.requires_elevated_confidence = True
    result.input_tokens = 1234
    result.output_tokens = 567
    result.structured_payload = {"foo": "bar"}
    result.retry_telemetry = {"attempts": 2}

    finding = _finding()
    cache = VerificationCache()
    cache.put(finding, cycle=DEFAULT_CYCLE, result=result)
    cache_path = tmp_path / "cache.json"
    cache.save_to_disk(cache_path)

    reloaded = VerificationCache()
    reloaded.load_from_disk(cache_path)
    hit = reloaded.get(finding, cycle=DEFAULT_CYCLE)
    assert hit is not None

    # Every skipped field (apart from the replay-state pair the cache stamps
    # itself) comes back at its dataclass default.
    assert hit.escalation_attempted is False
    assert hit.initial_model == ""
    assert hit.initial_verdict == ""
    assert hit.escalation_changed_verdict is False
    assert hit.escalation_reason == ""
    assert hit.requires_elevated_confidence is False
    assert hit.input_tokens == 0
    assert hit.output_tokens == 0
    assert hit.structured_payload is None
    assert hit.retry_telemetry is None
    # Replay state the cache stamps on a hit.
    assert hit.cache_status == "hit"


def test_legacy_entry_missing_keys_loads_at_defaults(tmp_path: Path):
    """A hand-written cache file missing newer keys still loads cleanly."""
    minimal = {
        "verdict": "CONFIRMED",
        "grounded": True,
        "sources": ["https://a.example/1"],
        "accepted_sources": ["https://a.example/1"],
    }
    restored = _result_from_dict(minimal, cache_status="miss")
    assert restored.verdict == "CONFIRMED"
    assert restored.grounded is True
    # Missing telemetry keys default rather than crash.
    assert restored.web_fetch_requests == 0
    assert restored.fetched_sources == []
    assert restored.models_disagreed is False
    assert restored.correction is None
    assert restored.rejected_sources == []
