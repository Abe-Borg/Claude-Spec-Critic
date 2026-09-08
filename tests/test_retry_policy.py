"""B-4: batch-failure taxonomy — rate limit and overload are distinct classes.

An ``errored`` batch item whose *message* said "rate limit" used to classify
SERVER_ERROR while the same failure with a structured ``error.type`` of
``rate_limit_error`` classified RATE_LIMIT, so the two shapes backed off on
different multipliers. The message-scan branch now mirrors the structured
split exactly.
"""
from __future__ import annotations

import pytest

from src.verification.retry_policy import (
    DEFAULT_VERIFICATION_RETRY_POLICY,
    FailureClass,
    classify_batch_failure,
    compute_backoff_seconds,
    should_retry_batch_failure,
)


class TestRateLimitVersusOverloadSplit:
    @pytest.mark.parametrize(
        "message",
        [
            "Rate limit exceeded",
            "rate_limit_error: Number of request tokens has exceeded your limit",
            "This request was rate-limited; retry later",
        ],
    )
    def test_rate_limit_message_in_errored_item(self, message: str) -> None:
        assert (
            classify_batch_failure(result_type="errored", error_message=message)
            is FailureClass.RATE_LIMIT
        )

    def test_overloaded_message_is_server_error(self) -> None:
        assert (
            classify_batch_failure(result_type="errored", error_message="Overloaded")
            is FailureClass.SERVER_ERROR
        )

    def test_structured_rate_limit_type(self) -> None:
        assert (
            classify_batch_failure(result_type="errored", error_type="rate_limit_error")
            is FailureClass.RATE_LIMIT
        )

    def test_structured_overloaded_type(self) -> None:
        assert (
            classify_batch_failure(result_type="errored", error_type="overloaded_error")
            is FailureClass.SERVER_ERROR
        )

    @pytest.mark.parametrize(
        ("error_type", "message", "expected"),
        [
            ("rate_limit_error", "Rate limit exceeded", FailureClass.RATE_LIMIT),
            ("overloaded_error", "Overloaded", FailureClass.SERVER_ERROR),
            ("invalid_request_error", "invalid request: bad field", FailureClass.INVALID_REQUEST),
            ("api_error", "Internal server error", FailureClass.SERVER_ERROR),
        ],
    )
    def test_structured_and_message_only_paths_agree(
        self, error_type: str, message: str, expected: FailureClass
    ) -> None:
        with_type = classify_batch_failure(
            result_type="errored", error_type=error_type, error_message=message
        )
        message_only = classify_batch_failure(result_type="errored", error_message=message)
        assert with_type is expected
        assert message_only is expected

    def test_rate_limit_stays_retryable_on_its_own_backoff(self) -> None:
        policy = DEFAULT_VERIFICATION_RETRY_POLICY
        assert should_retry_batch_failure(FailureClass.RATE_LIMIT)
        assert compute_backoff_seconds(
            policy, attempt=1, failure_class=FailureClass.RATE_LIMIT
        ) == policy.base_backoff_seconds * policy.rate_limit_multiplier
        # The policy keeps the two multipliers distinct, which is why the
        # classification split matters.
        assert policy.rate_limit_multiplier != policy.server_error_multiplier

    def test_other_errored_messages_unchanged(self) -> None:
        assert (
            classify_batch_failure(result_type="errored", error_message="internal server error")
            is FailureClass.SERVER_ERROR
        )
        assert (
            classify_batch_failure(result_type="errored", error_message="something odd")
            is FailureClass.BATCH_ERRORED
        )
        assert classify_batch_failure(result_type="expired") is FailureClass.BATCH_EXPIRED
        assert classify_batch_failure(result_type="canceled") is FailureClass.BATCH_CANCELED
