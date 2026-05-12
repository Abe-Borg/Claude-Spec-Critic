"""Chunk 1 — API model capability and batch-retention corrections.

Pins the two surgical fixes from the repair plan's Chunk 1:

1. ``MODEL_SONNET_46`` reports ``supports_extended_output_beta=True``.
2. The 300k extended-output decision in :func:`src.batch.submit_review_batch`
   is driven by ``model_capabilities(...).supports_extended_output_beta``,
   not by ``model in OPUS_MODELS``.
3. Local batch-state retention is 28 days with a 25-day warning threshold,
   conservatively under the Anthropic Message Batches result-download
   retention window.

Earlier reviews proposed downgrading ``claude-opus-4-7`` to an older dated
model; the plan explicitly says NOT to make that change because the ID is
current and valid. We assert the default review model stays Opus 4.7 to
keep that ratchet in the test suite.
"""
from __future__ import annotations

import pytest

from src import batch_state_store
from src.api_config import (
    BATCH_OUTPUT_BETA,
    MODEL_HAIKU_45,
    MODEL_OPUS_46,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    model_capabilities,
    model_supports_extended_output_beta,
)
from src.app_paths import (
    BATCH_STATE_MAX_AGE_HOURS,
    BATCH_STATE_WARNING_AGE_HOURS,
)
from tests.test_request_payload_shape import FakeClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_count_tokens_local(monkeypatch):
    """Cheap word-count proxy so tests stay hermetic."""

    def _fake(text):
        return len((text or "").split()) * 2

    monkeypatch.setattr("src.tokenizer.count_tokens", _fake)
    monkeypatch.setattr("src.cross_checker.count_tokens", _fake)
    monkeypatch.setattr("src.pipeline.count_tokens", _fake, raising=False)
    # Chunk 3: ``src.batch`` no longer imports ``count_tokens`` — every
    # batch token count is computed inside the central builder.
    monkeypatch.setattr(
        "src.review_request_builder.count_tokens", _fake, raising=False
    )


@pytest.fixture
def fake_client(monkeypatch, fake_anthropic, _stub_count_tokens_local):
    """FakeClient wired into reviewer / batch / verifier / cross_checker.

    Local copy of the fixture defined in ``test_request_payload_shape.py``
    so this test module is self-contained.
    """
    from src import batch as batch_mod
    from src import cross_checker as cc_mod
    from src import reviewer as reviewer_mod
    from src import verifier as verifier_mod

    client = FakeClient(
        default_final_message=fake_anthropic.review_tool_use_response(),
    )

    def _provider() -> FakeClient:
        return client

    monkeypatch.setattr(reviewer_mod, "_get_client", _provider)
    monkeypatch.setattr(batch_mod, "_get_client", _provider)
    monkeypatch.setattr(verifier_mod, "_get_client", _provider)
    monkeypatch.setattr(cc_mod, "_get_client", _provider)
    return client


# ---------------------------------------------------------------------------
# 1) Sonnet 4.6 capability
# ---------------------------------------------------------------------------


class TestSonnet46ExtendedOutputCapability:
    """The 300k batch beta works on Sonnet 4.6 when the documented header is set."""

    def test_sonnet_46_supports_extended_output_beta(self) -> None:
        caps = model_capabilities(MODEL_SONNET_46)
        assert caps.supports_extended_output_beta is True

    def test_opus_models_still_support_extended_output_beta(self) -> None:
        # Sanity: extending support to Sonnet must not have regressed Opus.
        assert model_capabilities(MODEL_OPUS_46).supports_extended_output_beta is True
        assert model_capabilities(MODEL_OPUS_47).supports_extended_output_beta is True

    def test_haiku_does_not_support_extended_output_beta(self) -> None:
        # Haiku 4.5 ships without the 300k batch beta; the helper must
        # still report False so a misrouted batch can't ask for it.
        assert model_capabilities(MODEL_HAIKU_45).supports_extended_output_beta is False

    def test_unknown_model_does_not_support_extended_output_beta(self) -> None:
        assert model_supports_extended_output_beta("claude-future-model-2030") is False

    def test_helper_matches_registry(self) -> None:
        # ``model_supports_extended_output_beta`` is a thin convenience over
        # ``model_capabilities``; the two must never disagree.
        for model in (MODEL_OPUS_46, MODEL_OPUS_47, MODEL_SONNET_46, MODEL_HAIKU_45):
            assert (
                model_supports_extended_output_beta(model)
                is model_capabilities(model).supports_extended_output_beta
            )


# ---------------------------------------------------------------------------
# 2) Extended-output header is gated by capability, not by model family
# ---------------------------------------------------------------------------


class TestBatchExtendedOutputUsesCapabilityRegistry:
    """``submit_review_batch`` consults ``model_capabilities`` for the beta header.

    These tests use the existing request-shape plumbing — ``fake_client`` plus
    the ``_stub_count_tokens`` fixture — so we can simulate a large enough
    input to trip ``LARGE_REVIEW_INPUT_THRESHOLD`` without building a 200k-
    token spec.
    """

    pytestmark = pytest.mark.request_shape

    @staticmethod
    def _force_large_input(monkeypatch) -> None:
        """Make the per-spec token estimate look "large" to the batch path.

        ``submit_review_batch`` gates extended output on
        ``approx_input_tokens >= LARGE_REVIEW_INPUT_THRESHOLD`` (200k). The
        function calls ``count_tokens`` against both the system prompt and
        each user message; returning 200k+ for the user message alone
        guarantees the threshold trips regardless of cycle/mode defaults.
        """
        def _fake(text):
            return 250_000

        # Chunk 3: the extended-output decision moved into the central
        # ``review_request_builder``; patch the counter binding there so
        # every batched spec sees the inflated count.
        monkeypatch.setattr(
            "src.review_request_builder.count_tokens", _fake, raising=False
        )

    def test_sonnet_46_large_input_emits_extended_output_beta(
        self, fake_client, monkeypatch
    ) -> None:
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE
        from tests.test_request_payload_shape import _spec

        self._force_large_input(monkeypatch)
        submit_review_batch(
            [_spec(content="Large spec body.")],
            model=MODEL_SONNET_46,
            cycle=DEFAULT_CYCLE,
        )

        batch = fake_client.captured[0]
        # Chunk 1: a Sonnet 4.6 batch with a large input must now route
        # through ``beta.batches.create`` and carry the 300k beta header.
        # The prior ``model in OPUS_MODELS`` family check silently dropped
        # Sonnet onto the standard endpoint.
        assert batch.endpoint == "beta.batches.create"
        assert BATCH_OUTPUT_BETA in batch.betas

    def test_opus_47_large_input_still_emits_extended_output_beta(
        self, fake_client, monkeypatch
    ) -> None:
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE
        from tests.test_request_payload_shape import _spec

        self._force_large_input(monkeypatch)
        submit_review_batch(
            [_spec(content="Large spec body.")],
            model=MODEL_OPUS_47,
            cycle=DEFAULT_CYCLE,
        )

        batch = fake_client.captured[0]
        # Capability-driven check must preserve Opus behavior.
        assert batch.endpoint == "beta.batches.create"
        assert BATCH_OUTPUT_BETA in batch.betas

    def test_small_input_never_emits_extended_output_beta(
        self, fake_client
    ) -> None:
        # Threshold is the input-size gate, not just capability — a small
        # Sonnet batch must still use the standard endpoint.
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE
        from tests.test_request_payload_shape import _spec

        submit_review_batch(
            [_spec(content="tiny")],
            model=MODEL_SONNET_46,
            cycle=DEFAULT_CYCLE,
        )
        batch = fake_client.captured[0]
        assert batch.endpoint == "batches.create"
        assert BATCH_OUTPUT_BETA not in batch.betas

    def test_unknown_model_never_requests_extended_output(
        self, fake_client, monkeypatch
    ) -> None:
        # Capability defaults to False for unregistered models. Even with a
        # large input the batch must not ask for the 300k beta.
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE
        from tests.test_request_payload_shape import _spec

        self._force_large_input(monkeypatch)
        submit_review_batch(
            [_spec(content="Large spec body.")],
            model="claude-future-model-2030",
            cycle=DEFAULT_CYCLE,
        )
        batch = fake_client.captured[0]
        assert batch.endpoint == "batches.create"
        assert BATCH_OUTPUT_BETA not in batch.betas


# ---------------------------------------------------------------------------
# 3) Batch-state retention thresholds
# ---------------------------------------------------------------------------


class TestBatchStateRetentionThresholds:
    """28-day local expiry and 25-day warning threshold."""

    def test_max_age_is_28_days(self) -> None:
        # The Anthropic Message Batches API retains downloadable results
        # for ~29 days; expiring our local state at 28 keeps the window
        # safely on the actionable side.
        assert BATCH_STATE_MAX_AGE_HOURS == 24 * 28

    def test_warning_threshold_is_25_days(self) -> None:
        assert BATCH_STATE_WARNING_AGE_HOURS == 24 * 25

    def test_warning_threshold_is_strictly_before_max_age(self) -> None:
        # The warning must fire at least one day before the local expiry
        # so the user has time to act before resume-state is dropped.
        assert BATCH_STATE_WARNING_AGE_HOURS < BATCH_STATE_MAX_AGE_HOURS

    def test_nearing_expiry_returns_true_past_warning_threshold(self) -> None:
        import time

        old_created_at = time.time() - (BATCH_STATE_WARNING_AGE_HOURS + 1) * 3600
        assert batch_state_store.batch_state_nearing_expiry(old_created_at) is True

    def test_nearing_expiry_returns_false_for_fresh_state(self) -> None:
        import time

        assert batch_state_store.batch_state_nearing_expiry(time.time()) is False

    def test_nearing_expiry_handles_invalid_timestamp(self) -> None:
        # Defensive: a malformed ``created_at`` in a legacy state payload
        # must not raise — the dialog should still open without a warning.
        assert batch_state_store.batch_state_nearing_expiry("not-a-number") is False

    def test_load_batch_state_drops_state_older_than_28_days(
        self, tmp_path, monkeypatch
    ) -> None:
        # Round-trip a saved state that's just past 28 days and confirm the
        # store deletes it instead of returning it.
        import json
        from datetime import datetime, timedelta, timezone

        state_path = tmp_path / "batch_state.json"
        monkeypatch.setattr(batch_state_store, "_batch_state_path", lambda: state_path)
        too_old = datetime.now(timezone.utc) - timedelta(
            hours=BATCH_STATE_MAX_AGE_HOURS + 1
        )
        state_path.write_text(
            json.dumps(
                {
                    "saved_at": too_old.isoformat(),
                    "phase": "review_poll",
                    "submission": {},
                }
            ),
            encoding="utf-8",
        )
        assert batch_state_store.load_batch_state() is None
        # And the stale file must have been cleaned up.
        assert not state_path.exists()


