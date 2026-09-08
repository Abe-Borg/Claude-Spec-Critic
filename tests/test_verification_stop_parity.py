"""A-8: real-time and batch classify an incomplete verification stop the same way.

``classify_verification_stop_reason`` puts ``max_tokens`` (and every other
non-terminal, non-pause stop such as ``refusal``) in ``STOP_CLASS_INCOMPLETE``
for BOTH paths — but the two paths used to turn that shared class into
different results:

* batch (``_classify_wave_results`` → wave loop): ``terminal_unverified`` /
  ``FailureClass.PARSE_ERROR`` → ``verification_failed=True`` →
  VERIFICATION_FAILED, refused by the cache, counted in the Run Diagnostics
  "verification failures" row;
* real-time (``_run_verification_call``): a bare ``_make_unverified(...)``
  whose defaults are ``failed=False`` and zero counters → INSUFFICIENT_EVIDENCE
  ("the verifier ran cleanly and found nothing"), which is not what happened.

The real-time incomplete-stop return now stamps ``failed=True``, the
search / fetch counters accumulated over every response so far, and the same
PARSE_ERROR ``retry_telemetry`` the batch path records. These tests drive the
same incomplete message through both paths and assert the results agree.

Unchanged by the fix (pinned here so the scope is explicit):

* the stop-reason classifier itself — ``refusal`` stays INCOMPLETE and
  ``pause_turn`` stays PAUSE;
* pause-loop exhaustion — a finding that keeps pausing until the continuation
  cap is a clean UNVERIFIED (``verification_failed=False`` →
  INSUFFICIENT_EVIDENCE) on both paths (batch side also pinned by
  ``test_batch_continuation_cap``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.verification.verifier as V
from src.core.code_cycles import DEFAULT_CYCLE
from src.output.report_status import ReportStatus, classify_status, is_budget_exhausted
from src.review.reviewer import Finding
from src.verification.verification_cache import VerificationCache
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    STOP_CLASS_COMPLETE,
    STOP_CLASS_INCOMPLETE,
    STOP_CLASS_PAUSE,
    classify_verification_stop_reason,
    collect_verification_batch_results,
)
from tests.fixtures.fake_anthropic import (
    FakeBatchResult,
    FakeBatchResultEnvelope,
    FakeMessage,
    FakeServerToolUsage,
    FakeServerToolUseBlock,
    FakeTextBlock,
    FakeUsage,
    FakeWebSearchResultBlock,
    pause_turn_response,
)

SEARCHED_URL = "https://www.dgs.ca.gov/DSA/"
SEARCHES_BEFORE_STOP = 2


def _finding() -> Finding:
    """MEDIUM + a non-empty ``codeReference``.

    The code reference keeps the keyword classifier from local-skipping the
    finding (so the real-time path actually calls the API); MEDIUM keeps it
    out of the escalation tier on both paths, so each path makes exactly
    one verification call and the comparison is one-to-one.
    """
    return Finding(
        severity="MEDIUM",
        fileName="21 13 13 - Wet-Pipe Sprinkler.docx",
        section="2.4",
        issue="Sprinkler spacing cited exceeds the maximum for the listed hazard class.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13 §10.2.4",
        confidence=0.5,
    )


def _incomplete_message(stop_reason: str):
    """A verifier response that searched (2 requests, one usable result) and
    then stopped without finishing its turn — no verdict tool call."""
    return FakeMessage(
        content=[
            FakeServerToolUseBlock(
                name="web_search", input={"query": "NFPA 13 sprinkler spacing"}
            ),
            FakeWebSearchResultBlock(
                content=[
                    {
                        "type": "web_search_result",
                        "url": SEARCHED_URL,
                        "title": "DSA",
                        "encrypted_content": "fake-encrypted-blob",
                    }
                ]
            ),
            FakeTextBlock(text="Reviewing the spacing table… (truncated mid-sentence"),
        ],
        stop_reason=stop_reason,
        usage=FakeUsage(
            server_tool_use=FakeServerToolUsage(
                web_search_requests=SEARCHES_BEFORE_STOP, web_fetch_requests=0
            )
        ),
    )


# ---------------------------------------------------------------------------
# Scripted streaming client (real-time path)
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessagesAPI:
    def __init__(self, route):
        self._route = route
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self._route(kwargs))


class _FakeClient:
    def __init__(self, route):
        self.messages = _FakeMessagesAPI(route)

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls


def _run_realtime(monkeypatch, route) -> tuple[V.VerificationResult, _FakeClient]:
    client = _FakeClient(route)
    monkeypatch.setattr(V, "_get_client", lambda **_: client)
    result = V.verify_finding(_finding(), max_retries=0, cycle=DEFAULT_CYCLE, cache=None)
    return result, client


# ---------------------------------------------------------------------------
# Batch driver (wave loop with mocked primitives)
# ---------------------------------------------------------------------------


def _run_batch(monkeypatch, route, *, max_waves: int = 1) -> Finding:
    """``route(custom_id) -> message`` decides each wave's response."""

    def fake_poll(batch_id, *, policy, log, progress_cb):
        return SimpleNamespace(detached=False, poll_failed=False)

    def fake_retrieve(job):
        return {
            cid: FakeBatchResult(
                custom_id=cid,
                result=FakeBatchResultEnvelope(type="succeeded", message=route(cid)),
            )
            for cid in job.request_map
        }

    counter = {"n": 0}

    def fake_submit(requests, request_map, *, extra_headers=None):
        counter["n"] += 1
        return SimpleNamespace(
            batch_id=f"wave{counter['n'] + 1}-batch", request_map=request_map, job_type="verify"
        )

    def fake_verify_finding(finding, **_kwargs):
        raise AssertionError("real-time fallback must not run in the batch parity driver")

    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)
    monkeypatch.setattr(V, "verify_finding", fake_verify_finding)

    finding = _finding()
    job = SimpleNamespace(
        batch_id="init-batch",
        request_map={"verify__0": {"finding_idx": 0}},
        job_type="verify",
    )
    collect_verification_batch_results(
        job,
        [finding],
        log=lambda *_a, **_k: None,
        progress=lambda _p, _m: None,
        cycle=DEFAULT_CYCLE,
        poll_policy=DEFAULT_VERIFICATION_POLL_POLICY,
        max_waves=max_waves,
        cache=None,
        realtime_fallback_threshold=0,
    )
    assert finding.verification is not None
    return finding


def _cache_refuses(result: V.VerificationResult) -> bool:
    cache = VerificationCache()
    finding = _finding()
    cache.put(finding, cycle=DEFAULT_CYCLE, result=result)
    return cache.stats()["size"] == 0 and cache.get(finding, cycle=DEFAULT_CYCLE) is None


# ---------------------------------------------------------------------------
# 1. The stop-reason classifier is unchanged
# ---------------------------------------------------------------------------


class TestStopClassifierUnchanged:
    @pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "stop_sequence", None])
    def test_non_terminal_non_pause_stops_are_incomplete(self, stop_reason):
        assert classify_verification_stop_reason(stop_reason) == STOP_CLASS_INCOMPLETE

    def test_pause_turn_is_pause(self):
        assert classify_verification_stop_reason("pause_turn") == STOP_CLASS_PAUSE

    @pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use"])
    def test_terminal_stops_are_complete(self, stop_reason):
        assert classify_verification_stop_reason(stop_reason) == STOP_CLASS_COMPLETE


# ---------------------------------------------------------------------------
# 2. max_tokens: both paths produce the same operational-failure result
# ---------------------------------------------------------------------------


class TestIncompleteStopParity:
    def _assert_failed_parity(self, rt: V.VerificationResult, bt: V.VerificationResult, stop_reason: str):
        expected_explanation = f"Verification response incomplete (stop_reason: {stop_reason})."
        for label, r in (("realtime", rt), ("batch", bt)):
            assert r.verdict == "UNVERIFIED", label
            assert r.verification_failed is True, label
            assert r.grounded is False, label
            assert r.explanation == expected_explanation, label
            # Honest telemetry: the searches burned before the stop.
            assert r.web_search_requests == SEARCHES_BEFORE_STOP, label
            assert r.web_fetch_requests == 0, label
            # Operational failure, not a budget shortfall.
            assert r.budget_exhausted is False, label
            tel = r.retry_telemetry or {}
            assert tel.get("failure_class") == "parse_error", label
            assert tel.get("terminal_reason") == "terminal_unverified", label
            assert tel.get("attempts") == 1, label
            assert tel.get("continuation_count") == 0, label

    def test_max_tokens_stop_is_verification_failed_on_both_paths(self, monkeypatch):
        msg = _incomplete_message("max_tokens")

        rt_result, client = _run_realtime(monkeypatch, lambda _kwargs: msg)
        # One streaming call, no retry, no escalation.
        assert len(client.calls) == 1

        bt_finding = _run_batch(monkeypatch, lambda _cid: msg)
        bt_result = bt_finding.verification

        self._assert_failed_parity(rt_result, bt_result, "max_tokens")

        rt_finding = _finding()
        rt_finding.verification = rt_result
        assert classify_status(rt_finding) is ReportStatus.VERIFICATION_FAILED
        assert classify_status(bt_finding) is ReportStatus.VERIFICATION_FAILED
        assert is_budget_exhausted(rt_finding) is False
        assert is_budget_exhausted(bt_finding) is False

        # Neither result may be frozen into the cache — a re-run must
        # re-attempt verification.
        assert _cache_refuses(rt_result)
        assert _cache_refuses(bt_result)

    def test_refusal_stop_agrees_across_paths(self, monkeypatch):
        """``refusal`` was already an INCOMPLETE stop on both paths and the
        batch path already marked it failed; the real-time path now agrees
        instead of reporting a clean INSUFFICIENT_EVIDENCE."""
        msg = _incomplete_message("refusal")
        rt_result, _client = _run_realtime(monkeypatch, lambda _kwargs: msg)
        bt_result = _run_batch(monkeypatch, lambda _cid: msg).verification
        self._assert_failed_parity(rt_result, bt_result, "refusal")

    def test_realtime_counters_accumulate_across_continuations_before_stop(
        self, monkeypatch
    ):
        """Pause (1 search) → max_tokens (2 searches): the failed result
        reports all 3 searches and the one continuation it consumed."""
        responses = iter(
            [
                pause_turn_response(searched_urls=[SEARCHED_URL], web_search_requests=1),
                _incomplete_message("max_tokens"),
            ]
        )
        rt_result, client = _run_realtime(monkeypatch, lambda _kwargs: next(responses))
        assert len(client.calls) == 2
        assert rt_result.verification_failed is True
        assert rt_result.web_search_requests == 1 + SEARCHES_BEFORE_STOP
        assert (rt_result.retry_telemetry or {}).get("continuation_count") == 1


# ---------------------------------------------------------------------------
# 3. Pause-loop exhaustion keeps its classification (not touched by A-8)
# ---------------------------------------------------------------------------


class TestPauseLoopExhaustionUnchanged:
    def test_realtime_cap_terminal_is_clean_insufficient_evidence(self, monkeypatch):
        pause = pause_turn_response(searched_urls=[SEARCHED_URL], web_search_requests=1)
        rt_result, client = _run_realtime(monkeypatch, lambda _kwargs: pause)
        # One initial call + ``max_continuations`` resumes, every one paused.
        assert len(client.calls) >= 2
        assert rt_result.verdict == "UNVERIFIED"
        assert rt_result.verification_failed is False
        assert "maximum continuation attempts" in (rt_result.explanation or "")
        assert rt_result.web_search_requests == len(client.calls)
        f = _finding()
        f.verification = rt_result
        assert classify_status(f) is ReportStatus.INSUFFICIENT_EVIDENCE

    def test_batch_cap_terminal_is_clean_insufficient_evidence(self, monkeypatch):
        pause = pause_turn_response(searched_urls=[SEARCHED_URL], web_search_requests=1)
        finding = _run_batch(monkeypatch, lambda _cid: pause, max_waves=6)
        v = finding.verification
        assert v.verdict == "UNVERIFIED"
        assert v.verification_failed is False
        assert "maximum continuation attempts" in (v.explanation or "")
        assert classify_status(finding) is ReportStatus.INSUFFICIENT_EVIDENCE
