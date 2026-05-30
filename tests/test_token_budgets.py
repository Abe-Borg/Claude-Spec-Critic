"""Token counting and output budget enforcement tests.

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

These tests run hermetically against a local stub client so they cover
the preflight path without making network calls.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.core.api_config import (
    BATCH_MAX_OUTPUT_TOKENS,
    CROSS_CHECK_OUTPUT_CAP,
    HAIKU_TRIAGE_OUTPUT_CAP,
    MAX_OUTPUT_TOKENS_HAIKU,
    MAX_OUTPUT_TOKENS_OPUS,
    MAX_OUTPUT_TOKENS_SONNET,
    MODEL_HAIKU_45,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    PHASE_CROSS_CHECK,
    PHASE_REVIEW,
    PHASE_TRIAGE,
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
    REVIEW_OUTPUT_CAP,
    VERIFICATION_OUTPUT_CAP,
    cross_check_max_tokens,
    output_cap_for_model,
    phase_output_cap,
    review_max_tokens,
    triage_max_tokens,
    verification_max_tokens,
)
from src.core.tokenizer import (
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
    """One registry feeds every phase helper."""

    def test_each_phase_resolves_to_its_documented_cap(self):
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47) == REVIEW_OUTPUT_CAP
        assert phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_OPUS_47) == CROSS_CHECK_OUTPUT_CAP
        assert phase_output_cap(PHASE_TRIAGE, model=MODEL_HAIKU_45) == HAIKU_TRIAGE_OUTPUT_CAP
        # Retry / continuation share the verification budget.
        assert phase_output_cap(PHASE_VERIFICATION_RETRY, model=MODEL_SONNET_46) == phase_output_cap(
            PHASE_VERIFICATION, model=MODEL_SONNET_46
        )
        assert phase_output_cap(PHASE_VERIFICATION_CONTINUATION, model=MODEL_SONNET_46) == phase_output_cap(
            PHASE_VERIFICATION, model=MODEL_SONNET_46
        )

    def test_unknown_phase_degrades_to_verification_cap(self):
        # A future phase that forgets to register loses headroom rather than
        # silently inheriting the 128k review cap.
        cap = phase_output_cap("future_phase_we_havent_added", model=MODEL_OPUS_47)
        assert cap == VERIFICATION_OUTPUT_CAP


class TestPhaseCapsRespectModelCeilings:
    """Phase budgets never exceed model output ceilings."""

    def test_no_phase_exceeds_model_ceiling(self):
        phases = [
            PHASE_REVIEW, PHASE_CROSS_CHECK,
            PHASE_VERIFICATION, PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION, PHASE_TRIAGE,
        ]
        ceilings = {
            MODEL_OPUS_47: MAX_OUTPUT_TOKENS_OPUS,
            MODEL_SONNET_46: MAX_OUTPUT_TOKENS_SONNET,
            MODEL_HAIKU_45: MAX_OUTPUT_TOKENS_HAIKU,
        }
        for phase in phases:
            for model, ceiling in ceilings.items():
                cap = phase_output_cap(phase, model=model)
                assert cap <= ceiling, f"phase={phase} model={model} cap={cap} ceiling={ceiling}"

    def test_review_cap_clamps_to_smaller_model_ceilings(self):
        """Review's requested 128k must be clamped on smaller models."""
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_SONNET_46) == MAX_OUTPUT_TOKENS_SONNET
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_HAIKU_45) == MAX_OUTPUT_TOKENS_HAIKU

    def test_only_extended_batch_path_returns_300k(self):
        # Standard phases never grant 300k; the extended path requires the flag.
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47) < BATCH_MAX_OUTPUT_TOKENS
        assert review_max_tokens(
            model=MODEL_OPUS_47, allow_extended_output=True
        ) == BATCH_MAX_OUTPUT_TOKENS


class TestPhaseHelpersRouteThroughRegistry:
    """Thin wrappers must route through ``phase_output_cap``."""

    def test_helpers_match_registry(self):
        assert review_max_tokens(model=MODEL_OPUS_47) == phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47)
        assert cross_check_max_tokens(model=MODEL_OPUS_47) == phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_OPUS_47)
        assert verification_max_tokens(model=MODEL_SONNET_46) == phase_output_cap(PHASE_VERIFICATION, model=MODEL_SONNET_46)
        assert triage_max_tokens(model=MODEL_HAIKU_45) == phase_output_cap(PHASE_TRIAGE, model=MODEL_HAIKU_45)

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


# ---------------------------------------------------------------------------
# Local cl100k_base safety factor
# ---------------------------------------------------------------------------


class TestLocalEstimateSafetyFactor:
    """Directives 4 + 5: the cl100k_base estimate must not create false
    confidence. Apply a model-specific multiplier on the fallback path."""

    def test_safety_factors_have_expected_relative_widths(self):
        """Known models get modest margins; Haiku at least as wide as Opus;
        unknown/None models get the widest margin so a future model can't
        silently sail through a budget check."""
        opus = local_estimate_safety_factor(MODEL_OPUS_47)
        sonnet = local_estimate_safety_factor(MODEL_SONNET_46)
        haiku = local_estimate_safety_factor(MODEL_HAIKU_45)
        unknown = local_estimate_safety_factor("claude-future-2030")
        none_factor = local_estimate_safety_factor(None)
        assert 1.0 < opus <= 1.2
        assert 1.0 < sonnet <= 1.2
        assert haiku >= opus
        for known in (opus, sonnet, haiku):
            assert unknown >= known
        assert none_factor == unknown
        # The model-specific factor must flow through ``safe_local_estimate``:
        # Opus (narrower) pads less than Haiku (wider) for the same input.
        opus_padded = safe_local_estimate(454_000, model=MODEL_OPUS_47)
        haiku_padded = safe_local_estimate(454_000, model=MODEL_HAIKU_45)
        assert opus_padded < haiku_padded

    def test_safe_local_estimate_pads_upward(self):
        padded = safe_local_estimate(10_000, model=MODEL_OPUS_47)
        assert 10_000 < padded < 13_000

    def test_subunity_registry_factor_is_clamped_to_one(self, monkeypatch):
        """A sub-1.0 entry must never shrink the estimate (danger-pad guard).

        The factor is documented as a safety multiplier >= 1.0. If a bad
        value (a typo, or a misguided attempt to trim the margin) lands in
        the registry, the clamp keeps the padded estimate from dropping
        below the raw local count — which would undercount the Claude token
        total and let an over-budget spec slip through the fallback gate.
        """
        import src.core.tokenizer as tok

        monkeypatch.setitem(tok._LOCAL_SAFETY_FACTORS, "claude-typo-0-5", 0.5)
        assert local_estimate_safety_factor("claude-typo-0-5") == 1.0
        # Padded estimate must not undercount the raw input.
        assert safe_local_estimate(100_000, model="claude-typo-0-5") >= 100_000

    def test_subunity_default_factor_is_clamped(self, monkeypatch):
        """The unknown-model fallback is clamped too, not just registry hits."""
        import src.core.tokenizer as tok

        monkeypatch.setattr(tok, "_DEFAULT_LOCAL_SAFETY_FACTOR", 0.9)
        assert local_estimate_safety_factor("claude-unknown-9000") == 1.0
        assert safe_local_estimate(50_000, model="claude-unknown-9000") >= 50_000


class TestExceedsPerCallLimitForModel:
    """The local fallback gate uses the safety factor."""

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
    from src.input.extractor import ExtractedSpec

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
        "src.orchestration.pipeline.extract_multiple_specs_cached",
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

    monkeypatch.setattr("src.core.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr("src.orchestration.pipeline.count_tokens", _fake_count, raising=False)
    # The central review request builder also imports
    # ``count_tokens`` to gate the extended-output decision.
    monkeypatch.setattr(
        "src.review.review_request_builder.count_tokens", _fake_count, raising=False
    )
    return _fake_count


@pytest.fixture
def stub_client(monkeypatch, stub_count_tokens):
    # The pipeline preflight caches exact counts in a module-level dict.
    # Clear it so cross-test state can't make this test see a stale value.
    from src.input.extraction_cache import clear_token_cache

    clear_token_cache()
    client = _StubClient(return_tokens=1_000)
    monkeypatch.setattr("src.core.tokenizer._log", type("L", (), {"warning": lambda *a, **k: None})())
    monkeypatch.setattr("src.review.reviewer._get_client", lambda: client)
    return client


class TestPipelinePreflightSelectsModel:
    """Exact token counting uses the selected model."""

    @pytest.mark.parametrize("selected_model", [MODEL_SONNET_46, MODEL_HAIKU_45])
    def test_preflight_passes_selected_model_to_api(
        self, monkeypatch, patched_extractor, stub_client, selected_model
    ):
        from src.orchestration import pipeline

        # Make sure preflight is on. Pipeline imports get/cache helpers at
        # module scope, so patch the ``src.pipeline`` references directly.
        monkeypatch.setattr(
            "src.orchestration.pipeline.get_cached_token_count", lambda key: None
        )
        monkeypatch.setattr(
            "src.orchestration.pipeline.cache_token_count", lambda key, value: None
        )

        pipeline._prepare_specs(
            input_dir=Path("/tmp"),
            files=patched_extractor,
            model=selected_model,
        )

        assert stub_client.calls, "expected at least one count_tokens call"
        models_used = {call["model"] for call in stub_client.calls}
        assert selected_model in models_used
        # Nothing should have been counted under a model that wasn't selected
        # (Opus is never the selected model in this parametrization).
        assert MODEL_OPUS_47 not in models_used


class TestPipelinePreflightExactCountAuthoritative:
    """Exact count exceeding budget fails early."""

    def test_exact_count_over_budget_raises(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src.orchestration import pipeline

        # Sidestep the cache so the API stub is consulted.
        monkeypatch.setattr("src.orchestration.pipeline.get_cached_token_count", lambda key: None)
        monkeypatch.setattr("src.orchestration.pipeline.cache_token_count", lambda key, value: None)
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
        from src.orchestration import pipeline

        monkeypatch.setattr("src.orchestration.pipeline.get_cached_token_count", lambda key: None)
        monkeypatch.setattr("src.orchestration.pipeline.cache_token_count", lambda key, value: None)
        stub_client.return_tokens = 100  # well under the budget

        # Should not raise.
        pipeline._prepare_specs(
            input_dir=Path("/tmp"),
            files=patched_extractor,
            model=MODEL_OPUS_47,
        )


# ---------------------------------------------------------------------------
# Output cap defense-in-depth
# ---------------------------------------------------------------------------


class TestOutputCapsAreModelLimitAware:
    """``output_cap_for_model`` is the floor under the phase registry."""

    def test_clamps_each_model_to_its_ceiling(self):
        # Under ceiling: passes through.
        assert output_cap_for_model(MODEL_OPUS_47, requested=50_000) == 50_000
        # Over ceiling: clamped to each model's hard limit.
        assert output_cap_for_model(MODEL_OPUS_47, requested=999_999) == MAX_OUTPUT_TOKENS_OPUS
        assert output_cap_for_model(MODEL_SONNET_46, requested=999_999) == MAX_OUTPUT_TOKENS_SONNET
        assert output_cap_for_model(MODEL_HAIKU_45, requested=999_999) == MAX_OUTPUT_TOKENS_HAIKU
        # Unknown → safest known ceiling (Sonnet).
        assert output_cap_for_model("claude-future-2030", requested=999_999) == MAX_OUTPUT_TOKENS_SONNET
