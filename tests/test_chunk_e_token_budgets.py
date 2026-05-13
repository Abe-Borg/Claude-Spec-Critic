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

These tests run hermetically against a local stub client so they cover
the preflight path without making network calls.
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
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    PHASE_BATCH_REVIEW,
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
from src.tokenizer import (
    RECOMMENDED_MAX,
    exceeds_per_call_limit,
    exceeds_per_call_limit_for_model,
    local_estimate_safety_factor,
    safe_local_estimate,
)


pytestmark = pytest.mark.token_budget




class TestPhaseOutputCapRegistry:
    """Directive 6: one registry feeds every phase helper."""

    def test_each_phase_resolves_to_its_documented_cap(self):
        assert phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47) == REVIEW_OUTPUT_CAP
        assert phase_output_cap(PHASE_BATCH_REVIEW, model=MODEL_OPUS_47) == REVIEW_OUTPUT_CAP
        assert phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_OPUS_47) == CROSS_CHECK_OUTPUT_CAP
        assert phase_output_cap(PHASE_TRIAGE, model=MODEL_HAIKU_45) == HAIKU_TRIAGE_OUTPUT_CAP
        assert phase_output_cap(PHASE_VERIFICATION_RETRY, model=MODEL_SONNET_46) == phase_output_cap(
            PHASE_VERIFICATION, model=MODEL_SONNET_46
        )
        assert phase_output_cap(PHASE_VERIFICATION_CONTINUATION, model=MODEL_SONNET_46) == phase_output_cap(
            PHASE_VERIFICATION, model=MODEL_SONNET_46
        )

    def test_unknown_phase_degrades_to_verification_cap(self):
        cap = phase_output_cap("future_phase_we_havent_added", model=MODEL_OPUS_47)
        assert cap == VERIFICATION_OUTPUT_CAP


class TestPhaseCapsRespectModelCeilings:
    """Phase budgets never exceed model output ceilings."""

    def test_no_phase_exceeds_model_ceiling(self):
        phases = [
            PHASE_REVIEW, PHASE_BATCH_REVIEW, PHASE_CROSS_CHECK,
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
        assert phase_output_cap(PHASE_BATCH_REVIEW, model=MODEL_OPUS_47) < BATCH_MAX_OUTPUT_TOKENS
        assert review_max_tokens(
            batch=True, model=MODEL_OPUS_47, allow_extended_output=True
        ) == BATCH_MAX_OUTPUT_TOKENS


class TestPhaseHelpersRouteThroughRegistry:
    """Thin wrappers must route through ``phase_output_cap``."""

    def test_helpers_match_registry(self):
        assert review_max_tokens(model=MODEL_OPUS_47) == phase_output_cap(PHASE_REVIEW, model=MODEL_OPUS_47)
        assert cross_check_max_tokens(model=MODEL_OPUS_47) == phase_output_cap(PHASE_CROSS_CHECK, model=MODEL_OPUS_47)
        assert verification_max_tokens(model=MODEL_SONNET_46) == phase_output_cap(PHASE_VERIFICATION, model=MODEL_SONNET_46)
        assert triage_max_tokens(model=MODEL_HAIKU_45) == phase_output_cap(PHASE_TRIAGE, model=MODEL_HAIKU_45)

    def test_verification_max_tokens_routes_through_registry(self):
        assert verification_max_tokens(model=MODEL_SONNET_46) == phase_output_cap(
            PHASE_VERIFICATION, model=MODEL_SONNET_46
        )

    def test_verification_max_tokens_phase_parameter(self):
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

    def test_triage_max_tokens_routes_through_registry(self):
        assert triage_max_tokens(model=MODEL_HAIKU_45) == phase_output_cap(
            PHASE_TRIAGE, model=MODEL_HAIKU_45
        )




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

    def test_safe_local_estimate_pads_upward(self):
        padded = safe_local_estimate(10_000, model=MODEL_OPUS_47)
        assert 10_000 < padded < 13_000


class TestExceedsPerCallLimitForModel:
    """Directive 5: the local fallback gate uses the safety factor."""

    def test_returns_false_when_well_under_budget(self):
        assert (
            exceeds_per_call_limit_for_model(80_000, 20_000, model=MODEL_OPUS_47)
            is False
        )

    def test_safety_factor_pushes_borderline_over_limit(self):
        local_total = 450_000
        spec = 430_000
        overhead = 20_000
        assert exceeds_per_call_limit(spec, overhead) is False
        assert (
            exceeds_per_call_limit_for_model(spec, overhead, model=MODEL_HAIKU_45)
            is True
        )

    def test_safety_factor_uses_model_specific_value(self):
        spec = 444_000
        overhead = 10_000
        opus_padded = safe_local_estimate(spec + overhead, model=MODEL_OPUS_47)
        haiku_padded = safe_local_estimate(spec + overhead, model=MODEL_HAIKU_45)
        assert opus_padded < haiku_padded




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
    monkeypatch.setattr(
        "src.review_request_builder.count_tokens", _fake_count, raising=False
    )
    return _fake_count


@pytest.fixture
def stub_client(monkeypatch, stub_count_tokens):
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
        assert MODEL_OPUS_47 not in models_used

    def test_preflight_uses_haiku_when_haiku_selected(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src import pipeline

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

        monkeypatch.setattr("src.pipeline.get_cached_token_count", lambda key: None)
        monkeypatch.setattr("src.pipeline.cache_token_count", lambda key, value: None)
        stub_client.return_tokens = RECOMMENDED_MAX + 50_000

        with pytest.raises(ValueError) as excinfo:
            pipeline._prepare_specs(
                input_dir=Path("/tmp"),
                files=patched_extractor,
                model=MODEL_OPUS_47,
            )

        msg = str(excinfo.value)
        assert "spec_0.docx" in msg
        assert "exact" in msg.lower()
        assert "claude-opus-4-7" in msg

    def test_exact_count_under_budget_does_not_raise(
        self, monkeypatch, patched_extractor, stub_client
    ):
        from src import pipeline

        monkeypatch.setattr("src.pipeline.get_cached_token_count", lambda key: None)
        monkeypatch.setattr("src.pipeline.cache_token_count", lambda key, value: None)
        stub_client.return_tokens = 100

        pipeline._prepare_specs(
            input_dir=Path("/tmp"),
            files=patched_extractor,
            model=MODEL_OPUS_47,
        )




class TestOutputCapsAreModelLimitAware:
    """``output_cap_for_model`` is the floor under the phase registry."""

    def test_clamps_each_model_to_its_ceiling(self):
        assert output_cap_for_model(MODEL_OPUS_47, requested=50_000) == 50_000
        assert output_cap_for_model(MODEL_OPUS_47, requested=999_999) == MAX_OUTPUT_TOKENS_OPUS
        assert output_cap_for_model(MODEL_SONNET_46, requested=999_999) == MAX_OUTPUT_TOKENS_SONNET
        assert output_cap_for_model(MODEL_HAIKU_45, requested=999_999) == MAX_OUTPUT_TOKENS_HAIKU
        assert output_cap_for_model("claude-future-2030", requested=999_999) == MAX_OUTPUT_TOKENS_SONNET
