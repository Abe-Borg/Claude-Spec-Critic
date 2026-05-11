"""Chunk E — token counting and output budget enforcement tests.

Covers the directives in section 5 of the implementation plan:

- Exact token counting uses the *selected* model and the request shape that
  matches the eventual API call (directive 2).
- The exact Anthropic count is the authoritative gate when available; an
  exact count over ``RECOMMENDED_MAX`` fails early (directive 3).
- The local cl100k_base estimate carries a model-specific safety factor on
  the fallback path so an undercount cannot mask a real overage
  (directives 4, 5).
- Per-phase output budgets live in one registry; every phase resolves
  through the same helper and the registry never grants more than the
  model's hard ceiling (directives 6, 7).
- ``thinking`` budget concerns: verification retry / continuation use the
  phase-tagged helper so a tuning pass touches one place (directives 8, 9).

These tests run hermetically against the FakeClient wired up in
``test_request_payload_shape.py`` so they cover the request shape exactly
as the production code emits it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.api_config import (
    BATCH_MAX_OUTPUT_TOKENS,
    CROSS_CHECK_OUTPUT_CAP,
    HAIKU_TRIAGE_OUTPUT_CAP,
    MAX_OUTPUT_TOKENS_HAIKU,
    MAX_OUTPUT_TOKENS_OPUS,
    MAX_OUTPUT_TOKENS_SONNET,
    MODEL_HAIKU_45,
    MODEL_OPUS_46,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    PHASE_BATCH_REVIEW,
    PHASE_CROSS_CHECK,
    PHASE_REVIEW,
    PHASE_SYNTHESIS,
    PHASE_TRIAGE,
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
    REVIEW_OUTPUT_CAP,
    SYNTHESIS_OUTPUT_CAP,
    VERIFICATION_OUTPUT_CAP,
    cross_check_max_tokens,
    output_cap_for_model,
    phase_output_cap,
    review_max_tokens,
    synthesis_max_tokens,
    triage_max_tokens,
    verification_max_tokens,
)
from src.tokenizer import (
    RECOMMENDED_MAX,
    exceeds_per_call_limit,
    exceeds_per_call_limit_for_model,
    local_estimate_safety_factor,
    safe_local_estimate,
)


pytestmark = pytest.mark.token_budget


# ---------------------------------------------------------------------------
# Phase output budget registry
# ---------------------------------------------------------------------------


class TestPhaseOutputCapRegistry:
    """Directive 6: one registry feeds every phase helper."""

    def test_review_phase_resolves_to_review_cap(self):
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47) == REVIEW_OUTPUT_CAP

    def test_batch_review_phase_matches_realtime_review_phase(self):
        # Real-time and batch share the same baseline cap — findings cannot
        # diverge between modes on normal-size specs.
        assert phase_output_cap(PHASE_BATCH_REVIEW, model=MODEL_OPUS_47) == phase_output_cap(
            PHASE_REVIEW, model=MODEL_OPUS_47
        )

    def test_cross_check_phase_resolves_below_review(self):
        # Cross-check has less freedom than per-spec review.
        assert (
            phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_OPUS_47)
            == CROSS_CHECK_OUTPUT_CAP
        )
        assert (
            phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_OPUS_47)
            < phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47)
        )

    def test_synthesis_phase_uses_synthesis_cap(self):
        assert phase_output_cap(PHASE_SYNTHESIS, model=MODEL_HAIKU_45) == SYNTHESIS_OUTPUT_CAP

    def test_verification_retry_matches_verification(self):
        # Today retry shares the verification budget. The phase-tagged helper
        # is the lever for a future tuning pass.
        assert (
            phase_output_cap(PHASE_VERIFICATION_RETRY, model=MODEL_SONNET_46)
            == phase_output_cap(PHASE_VERIFICATION, model=MODEL_SONNET_46)
        )

    def test_verification_continuation_matches_verification(self):
        assert (
            phase_output_cap(PHASE_VERIFICATION_CONTINUATION, model=MODEL_SONNET_46)
            == phase_output_cap(PHASE_VERIFICATION, model=MODEL_SONNET_46)
        )

    def test_triage_phase_uses_triage_cap(self):
        assert phase_output_cap(PHASE_TRIAGE, model=MODEL_HAIKU_45) == HAIKU_TRIAGE_OUTPUT_CAP

    def test_unknown_phase_degrades_to_verification_cap(self):
        # Conservative default: a future phase that forgets to register
        # loses headroom rather than silently inheriting the 128k review cap.
        cap = phase_output_cap("future_phase_we_havent_added", model=MODEL_OPUS_47)
        assert cap == VERIFICATION_OUTPUT_CAP


class TestPhaseCapsRespectModelCeilings:
    """Directive 7: avoid using 128k+ caps as a default. Directive 4 of the
    plan acceptance criteria: phase budgets do not exceed model limits.
    """

    def test_review_cap_clamped_to_sonnet_ceiling(self):
        # Sonnet's max output is 64k; the review phase's *requested* value
        # is 128k. The helper must clamp.
        cap = phase_output_cap(PHASE_REVIEW, model=MODEL_SONNET_46)
        assert cap == MAX_OUTPUT_TOKENS_SONNET

    def test_review_cap_clamped_to_haiku_ceiling(self):
        cap = phase_output_cap(PHASE_REVIEW, model=MODEL_HAIKU_45)
        assert cap == MAX_OUTPUT_TOKENS_HAIKU

    def test_cross_check_cap_clamped_to_sonnet_ceiling(self):
        # Cross-check's requested cap (96k) exceeds Sonnet's 64k ceiling.
        cap = phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_SONNET_46)
        assert cap == MAX_OUTPUT_TOKENS_SONNET

    def test_no_phase_exceeds_model_ceiling(self):
        # Property: for every known phase × model combination, the resolved
        # cap is ≤ the model's hard output ceiling.
        phases = [
            PHASE_REVIEW,
            PHASE_BATCH_REVIEW,
            PHASE_CROSS_CHECK,
            PHASE_SYNTHESIS,
            PHASE_VERIFICATION,
            PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION,
            PHASE_TRIAGE,
        ]
        ceilings = {
            MODEL_OPUS_46: MAX_OUTPUT_TOKENS_OPUS,
            MODEL_OPUS_47: MAX_OUTPUT_TOKENS_OPUS,
            MODEL_SONNET_46: MAX_OUTPUT_TOKENS_SONNET,
            MODEL_HAIKU_45: MAX_OUTPUT_TOKENS_HAIKU,
        }
        for phase in phases:
            for model, ceiling in ceilings.items():
                cap = phase_output_cap(phase, model=model)
                assert cap <= ceiling, (
                    f"phase={phase} model={model} cap={cap} ceiling={ceiling}"
                )

    def test_only_extended_path_returns_300k(self):
        # The 300k batch beta is the *only* place a 300k cap shows up. The
        # standard phase helpers never grant it.
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47) < BATCH_MAX_OUTPUT_TOKENS
        assert (
            phase_output_cap(PHASE_BATCH_REVIEW, model=MODEL_OPUS_47)
            < BATCH_MAX_OUTPUT_TOKENS
        )
        # Extended path requires the explicit flag, which review_max_tokens
        # already covers in test_api_config.
        assert review_max_tokens(
            batch=True, model=MODEL_OPUS_47, allow_extended_output=True
        ) == BATCH_MAX_OUTPUT_TOKENS


class TestPhaseHelpersRouteThroughRegistry:
    """The thin wrappers route through ``phase_output_cap`` — kept so
    callers can keep their existing imports."""

    def test_review_max_tokens_routes_through_registry(self):
        assert review_max_tokens(model=MODEL_OPUS_47) == phase_output_cap(
            PHASE_REVIEW, model=MODEL_OPUS_47
        )

    def test_cross_check_max_tokens_routes_through_registry(self):
        assert cross_check_max_tokens(model=MODEL_OPUS_47) == phase_output_cap(
            PHASE_CROSS_CHECK, model=MODEL_OPUS_47
        )

    def test_verification_max_tokens_routes_through_registry(self):
        assert verification_max_tokens(model=MODEL_SONNET_46) == phase_output_cap(
            PHASE_VERIFICATION, model=MODEL_SONNET_46
        )

    def test_verification_max_tokens_phase_parameter(self):
        # The phase kwarg lets the caller pick retry vs continuation budgets
        # without hard-coding the constant.
        retry = verification_max_tokens(
            model=MODEL_SONNET_46, phase=PHASE_VERIFICATION_RETRY
        )
        cont = verification_max_tokens(
            model=MODEL_SONNET_46, phase=PHASE_VERIFICATION_CONTINUATION
        )
        assert retry == phase_output_cap(PHASE_VERIFICATION_RETRY, model=MODEL_SONNET_46)
        assert cont == phase_output_cap(
            PHASE_VERIFICATION_CONTINUATION, model=MODEL_SONNET_46
        )

    def test_synthesis_max_tokens_routes_through_registry(self):
        assert synthesis_max_tokens(model=MODEL_HAIKU_45) == phase_output_cap(
            PHASE_SYNTHESIS, model=MODEL_HAIKU_45
        )

    def test_triage_max_tokens_routes_through_registry(self):
        assert triage_max_tokens(model=MODEL_HAIKU_45) == phase_output_cap(
            PHASE_TRIAGE, model=MODEL_HAIKU_45
        )


# ---------------------------------------------------------------------------
# Local cl100k_base safety factor
# ---------------------------------------------------------------------------


class TestLocalEstimateSafetyFactor:
    """Directives 4 + 5: the cl100k_base estimate must not create false
    confidence. Apply a model-specific multiplier on the fallback path."""

    def test_opus_factor_within_modest_range(self):
        factor = local_estimate_safety_factor(MODEL_OPUS_47)
        assert 1.0 < factor <= 1.2

    def test_sonnet_factor_within_modest_range(self):
        factor = local_estimate_safety_factor(MODEL_SONNET_46)
        assert 1.0 < factor <= 1.2

    def test_haiku_factor_at_least_opus(self):
        # Haiku tends to undercount cl100k a bit more on structured spec
        # text — keep its margin at least as wide as Opus/Sonnet.
        haiku = local_estimate_safety_factor(MODEL_HAIKU_45)
        opus = local_estimate_safety_factor(MODEL_OPUS_47)
        assert haiku >= opus

    def test_unknown_model_uses_widest_margin(self):
        # A future model should never silently sail through a budget check
        # that would have been blocked under a known model.
        unknown = local_estimate_safety_factor("claude-future-2030")
        for known in (MODEL_OPUS_47, MODEL_SONNET_46, MODEL_HAIKU_45):
            assert unknown >= local_estimate_safety_factor(known)

    def test_none_model_uses_default_factor(self):
        factor = local_estimate_safety_factor(None)
        # Same as unknown — None goes through the default path.
        assert factor == local_estimate_safety_factor("claude-future-2030")

    def test_safe_local_estimate_pads_upward(self):
        padded = safe_local_estimate(10_000, model=MODEL_OPUS_47)
        assert padded > 10_000
        # Sanity: Opus's modest multiplier should not balloon the number.
        assert padded < 13_000


class TestExceedsPerCallLimitForModel:
    """Directive 5: the local fallback gate uses the safety factor."""

    def test_returns_false_when_well_under_budget(self):
        # 100k tokens is well under RECOMMENDED_MAX (500k) for any safety
        # factor on the registry.
        assert (
            exceeds_per_call_limit_for_model(80_000, 20_000, model=MODEL_OPUS_47)
            is False
        )

    def test_safety_factor_pushes_borderline_over_limit(self):
        # cl100k says 450k input — under the 500k recommended max — but
        # Haiku's 1.15× margin pushes the gate. Without the safety factor
        # we would have silently submitted.
        local_total = 450_000
        spec = 430_000
        overhead = 20_000
        # Sanity: under the legacy gate this is *not* over budget.
        assert exceeds_per_call_limit(spec, overhead) is False
        # Under the model-aware gate, Haiku's wider margin breaches the
        # recommended max.
        assert (
            exceeds_per_call_limit_for_model(spec, overhead, model=MODEL_HAIKU_45)
            is True
        )

    def test_safety_factor_uses_model_specific_value(self):
        # Same input. Opus (1.10×) and Haiku (1.15×) disagree about whether
        # 450k of cl100k tokens fits the 500k budget — that disagreement
        # *is* directive 5.
        spec = 444_000
        overhead = 10_000
        opus_padded = safe_local_estimate(spec + overhead, model=MODEL_OPUS_47)
        haiku_padded = safe_local_estimate(spec + overhead, model=MODEL_HAIKU_45)
        assert opus_padded < haiku_padded


# ---------------------------------------------------------------------------
# Pipeline preflight: selected model + exact-count gate
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal Anthropic-shaped stub for the preflight tests.

    Records every ``messages.count_tokens`` call so tests can assert that
    the selected model is passed through. Returns a configurable input
    token total via ``next_input_tokens``.
    """

    def __init__(self, *, return_tokens: int = 100):
        self.calls: list[dict[str, Any]] = []
        self.return_tokens = return_tokens

        class _Result:
            def __init__(s, total):
                s.input_tokens = total

        class _Messages:
            def __init__(s, parent):
                s.parent = parent

            def count_tokens(s, **kwargs):
                s.parent.calls.append(kwargs)
                return _Result(s.parent.return_tokens)

        self.messages = _Messages(self)


def _make_specs(content: str = "Sample spec content.", count: int = 1):
    from src.extractor import ExtractedSpec

    return [
        ExtractedSpec(
            filename=f"spec_{i}.docx",
            content=content,
            word_count=len(content.split()),
            source_path="",
            source_format="docx",
            paragraph_map=None,
        )
        for i in range(count)
    ]


@pytest.fixture
def patched_extractor(monkeypatch):
    """Bypass DOCX extraction — feed _prepare_specs a stub spec list."""
    specs = _make_specs()
    monkeypatch.setattr(
        "src.pipeline.extract_multiple_specs_cached",
        lambda paths: specs,
    )
    # _prepare_specs walks Path(input_dir).iterdir() when files isn't passed.
    # Provide a non-empty files list so we never touch the filesystem.
    return [Path(f"/tmp/{s.filename}") for s in specs]


@pytest.fixture
def stub_count_tokens(monkeypatch):
    """Replace the cl100k_base counter with a deterministic word-count proxy.

    Keeps the pipeline preflight hermetic; the real tokenizer wants to
    download merge tables on first use, which fails offline.
    """
    def _fake_count(text: str | None) -> int:
        return len((text or "").split()) * 2

    monkeypatch.setattr("src.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr("src.pipeline.count_tokens", _fake_count, raising=False)
    return _fake_count


@pytest.fixture
def stub_client(monkeypatch, stub_count_tokens):
    # The pipeline preflight caches exact counts in a module-level dict.
    # Clear it so cross-test state can't make this test see a stale value.
    from src.extraction_cache import clear_token_cache

    clear_token_cache()
    client = _StubClient(return_tokens=1_000)
    monkeypatch.setattr("src.tokenizer._log", type("L", (), {"warning": lambda *a, **k: None})())
    monkeypatch.setattr("src.reviewer._get_client", lambda: client)
    return client


class TestPipelinePreflightSelectsModel:
    """Directive 2: exact token counting uses the selected model."""

    def test_preflight_passes_selected_model_to_api(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src import pipeline

        # Make sure preflight is on. Pipeline imports get/cache helpers at
        # module scope, so patch the ``src.pipeline`` references directly.
        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        monkeypatch.setattr(
            "src.pipeline.get_cached_token_count", lambda key: None
        )
        monkeypatch.setattr(
            "src.pipeline.cache_token_count", lambda key, value: None
        )

        pipeline._prepare_specs(
            input_dir=Path("/tmp"),
            files=patched_extractor,
            model=MODEL_SONNET_46,
        )

        assert stub_client.calls, "expected at least one count_tokens call"
        models_used = {call["model"] for call in stub_client.calls}
        assert MODEL_SONNET_46 in models_used
        # And nothing should have been called with Opus, since that wasn't
        # the selected model.
        assert MODEL_OPUS_47 not in models_used

    def test_preflight_uses_haiku_when_haiku_selected(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src import pipeline

        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        monkeypatch.setattr(
            "src.pipeline.get_cached_token_count", lambda key: None
        )
        monkeypatch.setattr(
            "src.pipeline.cache_token_count", lambda key, value: None
        )

        pipeline._prepare_specs(
            input_dir=Path("/tmp"),
            files=patched_extractor,
            model=MODEL_HAIKU_45,
        )

        assert stub_client.calls
        assert any(call["model"] == MODEL_HAIKU_45 for call in stub_client.calls)


class TestPipelinePreflightExactCountAuthoritative:
    """Directive 3: exact count exceeding budget fails early."""

    def test_exact_count_over_budget_raises(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src import pipeline

        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        # Sidestep the cache so the API stub is consulted.
        monkeypatch.setattr("src.pipeline.get_cached_token_count", lambda key: None)
        monkeypatch.setattr("src.pipeline.cache_token_count", lambda key, value: None)
        # API returns a huge count that breaches RECOMMENDED_MAX even
        # though the local cl100k estimate is tiny.
        stub_client.return_tokens = RECOMMENDED_MAX + 50_000

        with pytest.raises(ValueError) as excinfo:
            pipeline._prepare_specs(
                input_dir=Path("/tmp"),
                files=patched_extractor,
                model=MODEL_OPUS_47,
            )

        # Error message names the spec + cites the exact count.
        msg = str(excinfo.value)
        assert "spec_0.docx" in msg
        assert "exact" in msg.lower()
        assert "claude-opus-4-7" in msg

    def test_exact_count_under_budget_does_not_raise(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src import pipeline

        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        monkeypatch.setattr("src.pipeline.get_cached_token_count", lambda key: None)
        monkeypatch.setattr("src.pipeline.cache_token_count", lambda key, value: None)
        stub_client.return_tokens = 100  # well under the budget

        # Should not raise.
        pipeline._prepare_specs(
            input_dir=Path("/tmp"),
            files=patched_extractor,
            model=MODEL_OPUS_47,
        )

    def test_local_gate_still_applies_when_preflight_disabled(
        self, monkeypatch, patched_extractor
    ):
        """When preflight is off, the local gate (with safety factor) runs."""
        from src import pipeline

        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "0")
        # Make the local counter return a value past the recommended max.
        # cl100k value alone is over budget — no safety factor needed.
        monkeypatch.setattr(
            "src.pipeline.count_tokens", lambda text: RECOMMENDED_MAX + 1_000
        )

        with pytest.raises(ValueError) as excinfo:
            pipeline._prepare_specs(
                input_dir=Path("/tmp"),
                files=patched_extractor,
                model=MODEL_OPUS_47,
            )

        msg = str(excinfo.value)
        assert "cl100k" in msg.lower() or "safety" in msg.lower() or "recommended" in msg.lower()
        assert "claude-opus-4-7" in msg


# ---------------------------------------------------------------------------
# Request-shape regressions for the Chunk E budget routing
# ---------------------------------------------------------------------------


# Re-use the FakeClient + fake_anthropic fixture from the existing
# request-shape suite so this file stays self-contained but consistent
# with the rest of the request-shape coverage.


@pytest.fixture
def fake_client_e(monkeypatch):
    """Inline FakeClient setup so the test file does not depend on the
    fixtures defined inside ``test_request_payload_shape.py``."""
    from tests.fixtures import fake_anthropic
    from tests.test_request_payload_shape import FakeClient

    client = FakeClient(
        default_final_message=fake_anthropic.review_tool_use_response(),
    )

    def _provider():
        return client

    from src import batch as batch_mod
    from src import cross_checker as cc_mod
    from src import reviewer as reviewer_mod
    from src import verifier as verifier_mod

    monkeypatch.setattr(reviewer_mod, "_get_client", _provider)
    monkeypatch.setattr(batch_mod, "_get_client", _provider)
    monkeypatch.setattr(verifier_mod, "_get_client", _provider)
    monkeypatch.setattr(cc_mod, "_get_client", _provider)

    def _fake_count(text: str | None) -> int:
        return len((text or "").split()) * 2

    monkeypatch.setattr("src.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr("src.batch.count_tokens", _fake_count)
    monkeypatch.setattr("src.cross_checker.count_tokens", _fake_count)
    monkeypatch.setattr("src.pipeline.count_tokens", _fake_count, raising=False)
    return client


def _spec_for_request_shape(content: str = "Spec body.", filename: str = "23 21 13.docx"):
    from src.extractor import ExtractedSpec

    return ExtractedSpec(
        filename=filename,
        content=content,
        word_count=len(content.split()),
        source_path="",
        source_format="docx",
        paragraph_map=None,
    )


def _finding_for_request_shape(**overrides):
    from src.reviewer import Finding

    base = dict(
        severity="HIGH",
        fileName="23 21 13.docx",
        section="2.1",
        issue="Cited code is outdated",
        actionType="EDIT",
        existingText="CBC 2019",
        replacementText="CBC 2025",
        codeReference="CBC 2025",
        confidence=0.6,
    )
    base.update(overrides)
    return Finding(**base)


class TestRequestShapeBudgetsByModel:
    """Acceptance criterion: output caps are model-limit-aware on every path."""

    def test_batch_review_request_uses_haiku_ceiling(self, fake_client_e):
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE

        submit_review_batch(
            [_spec_for_request_shape()], model=MODEL_HAIKU_45, cycle=DEFAULT_CYCLE,
        )
        params = fake_client_e.captured[0].first_params()
        # Haiku's max output is 64k — the review phase's nominal 128k must
        # be clamped.
        assert params["max_tokens"] == MAX_OUTPUT_TOKENS_HAIKU

    def test_batch_review_request_uses_sonnet_ceiling(self, fake_client_e):
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE

        submit_review_batch(
            [_spec_for_request_shape()], model=MODEL_SONNET_46, cycle=DEFAULT_CYCLE,
        )
        params = fake_client_e.captured[0].first_params()
        assert params["max_tokens"] == MAX_OUTPUT_TOKENS_SONNET

    def test_batch_review_request_uses_review_cap_for_opus(self, fake_client_e):
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE

        submit_review_batch(
            [_spec_for_request_shape()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE,
        )
        params = fake_client_e.captured[0].first_params()
        # Opus's ceiling (128k) and the review cap (128k) coincide, so the
        # clamp is a no-op here. Pin it anyway so a future cap bump can't
        # silently grant more output without updating this assertion.
        assert params["max_tokens"] == REVIEW_OUTPUT_CAP
        assert params["max_tokens"] <= MAX_OUTPUT_TOKENS_OPUS

    def test_verification_request_uses_verification_cap(self, fake_client_e):
        from src.batch import submit_verification_batch
        from src.code_cycles import DEFAULT_CYCLE

        def _prompt(_f):
            return "verify"

        def _system(_c):
            return "system"

        submit_verification_batch(
            [_finding_for_request_shape()],
            _prompt,
            _system,
            cycle=DEFAULT_CYCLE,
            model=MODEL_SONNET_46,
        )
        params = fake_client_e.captured[0].first_params()
        assert params["max_tokens"] == VERIFICATION_OUTPUT_CAP

    def test_verification_retry_request_uses_verification_cap(self):
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt body", cycle=DEFAULT_CYCLE)
        assert req["max_tokens"] == VERIFICATION_OUTPUT_CAP

    def test_verification_continuation_request_uses_verification_cap(self):
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt body",
            [{"type": "text", "text": "partial"}],
            cycle=DEFAULT_CYCLE,
        )
        assert req["max_tokens"] == VERIFICATION_OUTPUT_CAP

    def test_retry_and_initial_verification_share_budget_on_same_model(
        self, fake_client_e
    ):
        """Today the registry sets retry == initial. If a future change
        diverges them, this test should fail loud and force the author to
        decide whether the divergence is intended."""
        from src.batch import submit_verification_batch
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_retry_request

        def _prompt(_f):
            return "verify"

        def _system(_c):
            return "system"

        submit_verification_batch(
            [_finding_for_request_shape()],
            _prompt,
            _system,
            cycle=DEFAULT_CYCLE,
            model=MODEL_SONNET_46,
        )
        initial_params = fake_client_e.captured[0].first_params()
        retry_req = _build_retry_request(
            "prompt body", cycle=DEFAULT_CYCLE, model=MODEL_SONNET_46
        )
        assert initial_params["max_tokens"] == retry_req["max_tokens"]


# ---------------------------------------------------------------------------
# Output cap defense-in-depth
# ---------------------------------------------------------------------------


class TestOutputCapsAreModelLimitAware:
    """``output_cap_for_model`` is the floor under the phase registry; pin
    it directly so future helpers added on top still inherit the clamp."""

    def test_opus_requested_under_ceiling_returned_as_is(self):
        assert output_cap_for_model(MODEL_OPUS_47, requested=50_000) == 50_000

    def test_opus_requested_over_ceiling_clamped(self):
        assert (
            output_cap_for_model(MODEL_OPUS_47, requested=999_999)
            == MAX_OUTPUT_TOKENS_OPUS
        )

    def test_sonnet_clamped_to_sonnet_ceiling(self):
        assert (
            output_cap_for_model(MODEL_SONNET_46, requested=999_999)
            == MAX_OUTPUT_TOKENS_SONNET
        )

    def test_haiku_clamped_to_haiku_ceiling(self):
        assert (
            output_cap_for_model(MODEL_HAIKU_45, requested=999_999)
            == MAX_OUTPUT_TOKENS_HAIKU
        )

    def test_unknown_model_uses_conservative_ceiling(self):
        # Unknown → safest known ceiling (Sonnet). Mirrors the Chunk B
        # capability-policy fallback choice.
        assert (
            output_cap_for_model("claude-future-2030", requested=999_999)
            == MAX_OUTPUT_TOKENS_SONNET
        )
