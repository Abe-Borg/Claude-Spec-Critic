"""Graceful fallback when the 300k extended-output beta header is rejected
(TRUST_AUDIT P0-4).

``BATCH_OUTPUT_BETA`` (``output-300k-2026-03-24``) is hardcoded and attached
to the review batch submit for >=200k-token inputs. Betas get retired/renamed,
and an *unrecognized* anthropic-beta value is rejected by the API with HTTP
400 — the exact failure mode the retired ``web-fetch-2026-02-09`` header caused
on the common path. Before this change ``submit_review_batch`` would crash the
entire run at submit if the header were ever retired.

These tests pin the graceful fallback: a beta-header rejection clamps the
extended requests to the model ceiling and re-submits on the non-beta path
(output may truncate, the run survives), while any *other* error still
propagates so unrelated failures are never masked.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.api_config import (
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_OUTPUT_BETA,
    MAX_OUTPUT_TOKENS_OPUS,
    MAX_OUTPUT_TOKENS_SONNET,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    assert_extended_output_allowed,
)
import src.batch.batch as B
from src.batch.batch import (
    _clamp_requests_to_model_ceiling,
    _create_review_batch,
    _is_beta_header_rejection,
    submit_review_batch,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBadRequest(Exception):
    """Duck-typed stand-in for ``anthropic.BadRequestError`` (HTTP 400)."""

    def __init__(self, message: str, *, status_code: int | None = 400):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


def _beta_rejection_exc() -> FakeBadRequest:
    return FakeBadRequest(
        'invalid_request_error: Unexpected value(s) "output-300k-2026-03-24" '
        "for the anthropic-beta header"
    )


class _RecordingBatches:
    def __init__(self, *, on_create):
        self._on_create = on_create
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._on_create(kwargs)


class _Namespace:
    def __init__(self, batches):
        self.messages = SimpleNamespace(batches=batches)


class FakeClient:
    """Anthropic-shaped client with separate beta and non-beta batch paths."""

    def __init__(self, *, beta_create, plain_create):
        self.beta_batches = _RecordingBatches(on_create=beta_create)
        self.plain_batches = _RecordingBatches(on_create=plain_create)
        self.beta = _Namespace(self.beta_batches)
        self.messages = SimpleNamespace(batches=self.plain_batches)


def _ok_batch(_kwargs):
    return SimpleNamespace(id="batch_ok")


def _raise_beta_rejection(_kwargs):
    raise _beta_rejection_exc()


# ---------------------------------------------------------------------------
# 1. _is_beta_header_rejection — precise detection
# ---------------------------------------------------------------------------


class TestIsBetaHeaderRejection:
    def test_400_naming_anthropic_beta_is_rejection(self):
        assert _is_beta_header_rejection(_beta_rejection_exc()) is True

    def test_400_beta_plus_header_wording_is_rejection(self):
        exc = FakeBadRequest("Beta feature not enabled for this header value")
        assert _is_beta_header_rejection(exc) is True

    def test_non_400_with_beta_text_is_not_rejection(self):
        # A 500 mentioning the header is a server error, not a header rejection;
        # the fallback must not swallow it.
        exc = FakeBadRequest("anthropic-beta upstream error", status_code=500)
        assert _is_beta_header_rejection(exc) is False

    def test_400_unrelated_message_is_not_rejection(self):
        exc = FakeBadRequest("rate limit exceeded for organization")
        assert _is_beta_header_rejection(exc) is False

    def test_unknown_status_falls_back_to_message_signature(self):
        # A duck-typed exception with no status_code still matches on the
        # header-name signature alone.
        exc = Exception("Unexpected value for the anthropic-beta header")
        assert _is_beta_header_rejection(exc) is True


# ---------------------------------------------------------------------------
# 2. _clamp_requests_to_model_ceiling
# ---------------------------------------------------------------------------


class TestClampRequests:
    def test_clamps_extended_leaves_small_untouched(self):
        reqs = [
            {"custom_id": "a", "params": {"max_tokens": 300_000}},
            {"custom_id": "b", "params": {"max_tokens": 50_000}},
        ]
        _clamp_requests_to_model_ceiling(reqs, model=MODEL_OPUS_47)
        assert reqs[0]["params"]["max_tokens"] == MAX_OUTPUT_TOKENS_OPUS
        assert reqs[1]["params"]["max_tokens"] == 50_000  # already below ceiling

    def test_clamps_to_sonnet_ceiling_for_sonnet_model(self):
        reqs = [{"custom_id": "a", "params": {"max_tokens": 300_000}}]
        _clamp_requests_to_model_ceiling(reqs, model=MODEL_SONNET_46)
        assert reqs[0]["params"]["max_tokens"] == MAX_OUTPUT_TOKENS_SONNET

    def test_tolerates_missing_or_malformed_params(self):
        reqs = [{"custom_id": "a"}, {"custom_id": "b", "params": {}}, "garbage"]
        # Must not raise.
        _clamp_requests_to_model_ceiling(reqs, model=MODEL_OPUS_47)


# ---------------------------------------------------------------------------
# 3. _create_review_batch — the fallback decision
# ---------------------------------------------------------------------------


class TestCreateReviewBatch:
    def test_beta_success_uses_beta_path(self):
        client = FakeClient(beta_create=_ok_batch, plain_create=_ok_batch)
        reqs = [{"custom_id": "a", "params": {"max_tokens": 300_000}}]
        mb, used_beta = _create_review_batch(
            client, reqs, use_beta=True, model=MODEL_OPUS_47
        )
        assert used_beta is True
        assert mb.id == "batch_ok"
        # Beta path was used with the header; non-beta path untouched.
        assert len(client.beta_batches.calls) == 1
        assert client.beta_batches.calls[0]["betas"] == [BATCH_OUTPUT_BETA]
        assert client.plain_batches.calls == []
        # No clamp on the success path.
        assert reqs[0]["params"]["max_tokens"] == 300_000

    def test_beta_rejection_falls_back_and_clamps(self):
        client = FakeClient(
            beta_create=_raise_beta_rejection, plain_create=_ok_batch
        )
        reqs = [{"custom_id": "a", "params": {"max_tokens": 300_000}}]
        mb, used_beta = _create_review_batch(
            client, reqs, use_beta=True, model=MODEL_OPUS_47
        )
        # Fell back: non-beta submit, beta flag now False, run survives.
        assert used_beta is False
        assert mb.id == "batch_ok"
        assert len(client.beta_batches.calls) == 1  # attempted once
        assert len(client.plain_batches.calls) == 1  # re-submitted
        # The non-beta resubmission carries no betas kwarg ...
        assert "betas" not in client.plain_batches.calls[0]
        # ... and the extended request was clamped to the model ceiling.
        assert reqs[0]["params"]["max_tokens"] == MAX_OUTPUT_TOKENS_OPUS

    def test_non_beta_error_propagates_unmasked(self):
        def _raise_other(_kwargs):
            raise FakeBadRequest("model is overloaded", status_code=529)

        client = FakeClient(beta_create=_raise_other, plain_create=_ok_batch)
        reqs = [{"custom_id": "a", "params": {"max_tokens": 300_000}}]
        with pytest.raises(FakeBadRequest):
            _create_review_batch(client, reqs, use_beta=True, model=MODEL_OPUS_47)
        # Did NOT silently fall back to the non-beta path.
        assert client.plain_batches.calls == []

    def test_use_beta_false_takes_plain_path_directly(self):
        client = FakeClient(beta_create=_ok_batch, plain_create=_ok_batch)
        reqs = [{"custom_id": "a", "params": {"max_tokens": 100_000}}]
        mb, used_beta = _create_review_batch(
            client, reqs, use_beta=False, model=MODEL_OPUS_47
        )
        assert used_beta is False
        assert client.beta_batches.calls == []
        assert len(client.plain_batches.calls) == 1
        # No clamp when not using beta.
        assert reqs[0]["params"]["max_tokens"] == 100_000


# ---------------------------------------------------------------------------
# 4. End-to-end through submit_review_batch (proves the wiring)
# ---------------------------------------------------------------------------


class TestSubmitReviewBatchWiring:
    def _patch_builder(self, monkeypatch, *, max_tokens: int):
        """Force every spec to the extended-output path with ``max_tokens``."""
        built = SimpleNamespace(
            allow_extended_output=True,
            params={"max_tokens": max_tokens, "model": MODEL_OPUS_47},
        )
        monkeypatch.setattr(B, "build_review_request", lambda spec: built)
        return built

    def test_submit_survives_beta_rejection(self, monkeypatch):
        self._patch_builder(monkeypatch, max_tokens=300_000)
        client = FakeClient(
            beta_create=_raise_beta_rejection,
            plain_create=lambda _k: SimpleNamespace(id="batch_recovered"),
        )
        monkeypatch.setattr(B, "_get_client", lambda: client)

        specs = [SimpleNamespace(filename="23 21 13.docx", content="x", paragraph_map=None)]
        job = submit_review_batch(specs, model=MODEL_OPUS_47)

        # The run produced a usable BatchJob instead of crashing at submit.
        assert job.batch_id == "batch_recovered"
        assert job.job_type == "review"
        # The recovered submission went through the non-beta path with the
        # request clamped down from 300k to the model ceiling.
        plain_call = client.plain_batches.calls[0]
        assert "betas" not in plain_call
        assert plain_call["requests"][0]["params"]["max_tokens"] == MAX_OUTPUT_TOKENS_OPUS


# ---------------------------------------------------------------------------
# TRUST_AUDIT P2-3: assert_extended_output_allowed threshold is model-derived
# ---------------------------------------------------------------------------


class TestAssertExtendedOutputAllowed:
    """The fail-fast guard's threshold is the *selected model's* baseline
    output ceiling, not a hardcoded 128k. Sonnet's baseline is 64k, so a
    64k-128k Sonnet request without the beta — which the API would reject —
    must now be caught at the call site. Opus behavior is unchanged, and an
    omitted model falls back to the 128k Opus ceiling so the guard never
    over-fires on a legitimate sub-ceiling request."""

    def test_opus_300k_without_beta_raises(self):
        with pytest.raises(ValueError, match="beta header"):
            assert_extended_output_allowed(
                max_tokens=BATCH_MAX_OUTPUT_TOKENS, betas=None, model=MODEL_OPUS_47
            )

    def test_opus_300k_with_beta_ok(self):
        # No raise: the beta is present.
        assert_extended_output_allowed(
            max_tokens=BATCH_MAX_OUTPUT_TOKENS,
            betas=[BATCH_OUTPUT_BETA],
            model=MODEL_OPUS_47,
        )

    def test_opus_at_baseline_ceiling_ok(self):
        # 128k == Opus baseline ceiling: no beta needed.
        assert_extended_output_allowed(
            max_tokens=MAX_OUTPUT_TOKENS_OPUS, betas=None, model=MODEL_OPUS_47
        )

    def test_sonnet_above_its_64k_baseline_without_beta_raises(self):
        # The core P2-3 fix: 100k is below the old 128k threshold but ABOVE
        # Sonnet's 64k baseline, so the beta is required — the old guard let
        # this slip through to an API rejection.
        with pytest.raises(ValueError, match="beta header"):
            assert_extended_output_allowed(
                max_tokens=100_000, betas=None, model=MODEL_SONNET_46
            )

    def test_sonnet_at_its_baseline_ok(self):
        assert_extended_output_allowed(
            max_tokens=MAX_OUTPUT_TOKENS_SONNET, betas=None, model=MODEL_SONNET_46
        )

    def test_sonnet_above_baseline_with_beta_ok(self):
        assert_extended_output_allowed(
            max_tokens=100_000, betas=[BATCH_OUTPUT_BETA], model=MODEL_SONNET_46
        )

    def test_omitted_model_falls_back_to_opus_ceiling(self):
        # Backward-compatible: with no model, the threshold is the 128k Opus
        # ceiling, so a 100k request does NOT over-fire.
        assert_extended_output_allowed(max_tokens=100_000, betas=None, model=None)

    def test_omitted_model_still_catches_300k_without_beta(self):
        with pytest.raises(ValueError, match="beta header"):
            assert_extended_output_allowed(
                max_tokens=BATCH_MAX_OUTPUT_TOKENS, betas=None, model=None
            )
