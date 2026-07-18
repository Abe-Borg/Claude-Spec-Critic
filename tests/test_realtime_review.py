"""Real-time review mode: runner, transport plumbing, verification routing.

Hermetic throughout — a scripted streaming client fakes every API response
(builders from ``tests/fixtures/fake_anthropic.py``), the pipeline stages are
driven through monkeypatched seams (the ``test_requirements_research.py``
convention), and ui_state writes go to a tmp file via
``SPEC_CRITIC_UI_STATE_PATH``.

What's locked in:

* The runner produces batch-identical ``{custom_id: ReviewResult}`` /
  ``request_map`` shapes, from the same request builder (cache-prefix pin:
  system/tools/messages byte-equal to the batch build; no ``service_tier``;
  ``max_tokens`` is the 128k phase baseline, never 300k; ``client.beta`` is
  never touched — the fake client has no such attribute).
* Truncation parity: one inline instructed repair (the batch repair pass's
  instruction), then the spec surfaces through ``truncated_specs`` →
  ``PipelineResult.failed_review_specs`` exactly like a failed batch item.
* Retry taxonomy: transient classes retry, non-retryable classes terminate,
  workers never raise (one spec's crash leaves the others intact).
* The ≥200k gate raises before any client call — and only on models the
  extended-output beta whitelists (mirroring ``_resolve_extended_output``).
* Transport plumbing: ``start_batch_review(review_transport="realtime")``
  builds the job-stub submission without touching ``submit_review_batch``;
  collect never touches batch retrieval; the batch default is byte-untouched.
* ``verify_findings_for_run``: realtime arm = pre-pass + ``verify_finding``
  pool with exactly-once results; batch arm delegates to the existing pair.
* No pending state for realtime (``PendingBatch.from_submission`` refuses).
"""
from __future__ import annotations

import json
import threading
import time
import time as _time

import pytest

from src.batch.batch import BatchJob
from src.core.api_config import (
    MODEL_HAIKU_45,
    PHASE_REVIEW,
    REVIEW_MODEL_DEFAULT,
    phase_output_cap,
    realtime_review_max_workers,
)
from src.input.extractor import ExtractedSpec
from src.orchestration import pipeline as pl
from src.orchestration.batch_resume import PendingBatch
from src.orchestration.pipeline import (
    BatchSubmission,
    collect_review_batch_results,
    finalize_batch_result,
    verify_findings_for_run,
)
from src.review import realtime_review as rt
from src.review.realtime_review import REALTIME_JOB_SENTINEL, run_realtime_review
from src.review.review_request_builder import (
    RETRY_TRUNCATED_REVIEW_INSTRUCTION,
    ReviewRequestSpec,
    build_review_request,
)
from src.review.reviewer import Finding, ReviewResult
from src.verification.retry_policy import DEFAULT_REALTIME_RETRY_POLICY
from src.verification.verifier import VerificationResult
from tests.fixtures.fake_anthropic import (
    FakeMessage,
    FakeTextBlock,
    max_tokens_incomplete_response,
    review_tool_use_response,
    sample_review_findings_payload,
)


# ---------------------------------------------------------------------------
# Hermetic guards
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_token_estimate(monkeypatch):
    """Network-free stand-in for the runner's cl100k gate estimate.

    ``estimate_local_request_tokens`` loads tiktoken's ``cl100k_base``
    encoding, which downloads on first use — unavailable in hermetic runs
    (the ``test_compliance_pass.py`` convention is to stub the counter).
    A chars/4 proxy keeps the ≥threshold gate testable: the default ~70-char
    spec body estimates ≈17 tokens, comfortably over a monkeypatched
    threshold of 10 and comfortably under the real 200k.
    """
    monkeypatch.setattr(
        rt,
        "estimate_local_request_tokens",
        lambda request_spec: len(request_spec.spec_content) // 4,
    )


# ---------------------------------------------------------------------------
# Scripted streaming client (the test_compliance_pass.py shape)
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._message


class _FakeMessagesAPI:
    def __init__(self, route):
        self._route = route
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        result = self._route(kwargs)
        if isinstance(result, Exception):
            raise result
        return _FakeStream(result)


class FakeRealtimeClient:
    """Streaming-only client double. Deliberately has NO ``beta`` attribute —
    any code path reaching for ``client.beta`` (the 300k batch surface) fails
    loudly."""

    def __init__(self, route):
        self.messages = _FakeMessagesAPI(route)

    @property
    def calls(self):
        return self.messages.calls


def _route_by_filename(script: dict[str, list]):
    """Route each call by which filename marker appears in the user message.

    Thread-safe: the runner fans calls out over a pool.
    """
    remaining = {marker: list(items) for marker, items in script.items()}
    lock = threading.Lock()

    def route(kwargs):
        content = kwargs["messages"][0]["content"]
        with lock:
            for marker, items in remaining.items():
                if marker in content:
                    if not items:
                        raise AssertionError(f"script exhausted for {marker!r}")
                    return items.pop(0)
        raise AssertionError("no script marker matched the request")

    return route


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _spec(
    filename: str,
    content: str = "Section 23 21 13. Hydronic piping shall comply with the governing code.",
) -> ExtractedSpec:
    return ExtractedSpec(filename=filename, content=content, word_count=len(content.split()))


def _finding(issue: str, filename: str = "a.docx") -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName=filename,
        section="2.1",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="",
    )


def _realtime_submission(specs, results, request_map) -> BatchSubmission:
    """Assemble the submission ``start_batch_review`` would build for a
    realtime run (job stub + carried results)."""
    job = BatchJob(
        batch_id=REALTIME_JOB_SENTINEL,
        job_type="review",
        request_map=dict(request_map),
        created_at=time.time(),
        status="completed",
    )
    ordered = [cid for cid, _ in sorted(request_map.items(), key=lambda i: i[1]["index"])]
    return BatchSubmission(
        job=job,
        files_reviewed=[s.filename for s in specs],
        review_request_ids=ordered,
        prepared_specs=list(specs),
        review_transport="realtime",
        realtime_results=dict(results),
    )


class FakeDiagnostics:
    def __init__(self):
        self.calls: list[dict] = []

    def record_api_call(self, **kwargs):
        self.calls.append(kwargs)


# ===========================================================================
# 1-2. Runner happy path: tool-use parse, text fallback, request-shape pins
# ===========================================================================


class TestRunnerHappyPath:
    def test_two_specs_tool_use(self, monkeypatch):
        specs = [_spec("a.docx"), _spec("b.docx")]
        client = FakeRealtimeClient(
            _route_by_filename(
                {
                    "a.docx": [review_tool_use_response()],
                    "b.docx": [review_tool_use_response()],
                }
            )
        )
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, request_map = run_realtime_review(specs)

        assert set(results) == {"review__a__0", "review__b__1"}
        assert set(request_map) == set(results)
        assert request_map["review__a__0"] == {"filename": "a.docx", "index": 0, "type": "review"}
        assert request_map["review__b__1"] == {"filename": "b.docx", "index": 1, "type": "review"}
        for rr in results.values():
            assert rr.parse_status == "ok"
            assert rr.error is None
            assert len(rr.findings) == 1
            assert rr.structured_payload is not None
        assert len(client.calls) == 2

    def test_text_fallback_parse(self, monkeypatch):
        findings_json = json.dumps(sample_review_findings_payload()["findings"])
        message = FakeMessage(
            content=[
                FakeTextBlock(
                    text=f"Analysis prose.\n<findings_json>{findings_json}</findings_json>"
                )
            ],
            stop_reason="end_turn",
        )
        client = FakeRealtimeClient(lambda kwargs: message)
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("a.docx")])

        rr = results["review__a__0"]
        assert rr.parse_status == "ok"
        assert len(rr.findings) == 1
        assert rr.structured_payload is None  # text path, not tool use

    def test_request_shape_pins(self, monkeypatch):
        spec = _spec("a.docx")
        client = FakeRealtimeClient(lambda kwargs: review_tool_use_response())
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        run_realtime_review([spec])

        (call,) = client.calls
        # Batch-only knobs are absent / pinned.
        assert "service_tier" not in call
        assert call["max_tokens"] == phase_output_cap(PHASE_REVIEW, model=REVIEW_MODEL_DEFAULT)
        assert call["max_tokens"] < 300_000
        # Prompt-cache prefix stability: byte-equal system / tools / messages
        # vs the batch builder for the same spec. ``force_allow_extended_output``
        # is pinned only to keep this comparison build off tiktoken (hermetic);
        # it changes max_tokens, never the prompt bytes compared below.
        batch_built = build_review_request(
            ReviewRequestSpec(
                spec_content=spec.content,
                filename=spec.filename,
                model=REVIEW_MODEL_DEFAULT,
                force_allow_extended_output=False,
            )
        )
        assert call["system"] == batch_built.params["system"]
        assert call["tools"] == batch_built.params["tools"]
        assert call["messages"] == batch_built.params["messages"]

    def test_empty_specs_raises(self):
        with pytest.raises(ValueError):
            run_realtime_review([])


# ===========================================================================
# 3-4. Truncation parity with the batch path (inline repair, failed-review
#      surfacing through collect → finalize)
# ===========================================================================


class TestTruncationParity:
    def test_inline_repair_recovers_truncated_spec(self, monkeypatch):
        client = FakeRealtimeClient(
            _route_by_filename(
                {"a.docx": [max_tokens_incomplete_response(), review_tool_use_response()]}
            )
        )
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("a.docx")])

        assert results["review__a__0"].parse_status == "ok"
        assert len(client.calls) == 2
        # The repair call carries the shared batch-repair instruction; the
        # initial call must not.
        assert RETRY_TRUNCATED_REVIEW_INSTRUCTION not in client.calls[0]["messages"][0]["content"]
        assert RETRY_TRUNCATED_REVIEW_INSTRUCTION in client.calls[1]["messages"][0]["content"]

    def test_twice_truncated_spec_lands_in_failed_review_specs(self, monkeypatch):
        specs = [_spec("bad.docx"), _spec("good.docx")]
        client = FakeRealtimeClient(
            _route_by_filename(
                {
                    # initial + repair both truncate
                    "bad.docx": [
                        max_tokens_incomplete_response(),
                        max_tokens_incomplete_response(),
                    ],
                    "good.docx": [review_tool_use_response()],
                }
            )
        )
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, request_map = run_realtime_review(specs)
        assert results["review__bad__0"].parse_status == "incomplete"
        assert len(client.calls) == 3  # bad initial + bad repair + good

        submission = _realtime_submission(specs, results, request_map)
        state = collect_review_batch_results(submission)
        assert state.truncated_specs == ["bad.docx"]
        assert state.review_result.error  # combined per-spec error summary set

        final = finalize_batch_result(state)
        assert final.failed_review_specs == ["bad.docx"]
        # The healthy spec's findings survived.
        assert len(final.review_result.findings) == 1

    def test_repair_exception_keeps_original_truncated_result(self, monkeypatch):
        script = [
            max_tokens_incomplete_response(),
            ValueError("repair call exploded"),
        ]
        client = FakeRealtimeClient(lambda kwargs: script.pop(0))
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("a.docx")])

        rr = results["review__a__0"]
        assert rr.parse_status == "incomplete"  # original result, not lost
        assert len(client.calls) == 2


# ===========================================================================
# 5. Retry taxonomy — workers never raise
# ===========================================================================


class TestRetryTaxonomy:
    def test_transient_connection_error_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(rt, "compute_backoff_seconds", lambda *a, **k: 0.0)
        script = [
            Exception("peer closed connection without sending complete message body"),
            review_tool_use_response(),
        ]
        client = FakeRealtimeClient(lambda kwargs: script.pop(0))
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("a.docx")])

        assert results["review__a__0"].parse_status == "ok"
        assert len(client.calls) == 2

    def test_non_retryable_is_terminal_after_one_call(self, monkeypatch):
        client = FakeRealtimeClient(lambda kwargs: ValueError("boom"))
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("a.docx")])

        rr = results["review__a__0"]
        assert rr.findings == []
        assert rr.error and "boom" in rr.error
        assert len(client.calls) == 1

    def test_exhausted_retries_are_terminal(self, monkeypatch):
        monkeypatch.setattr(rt, "compute_backoff_seconds", lambda *a, **k: 0.0)
        client = FakeRealtimeClient(lambda kwargs: Exception("connection reset by peer"))
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("a.docx")])

        rr = results["review__a__0"]
        assert rr.error and "failed after" in rr.error.lower()
        assert len(client.calls) == max(1, DEFAULT_REALTIME_RETRY_POLICY.max_attempts)

    def test_one_spec_crash_leaves_others_intact(self, monkeypatch):
        specs = [_spec("bad.docx"), _spec("good.docx")]
        client = FakeRealtimeClient(
            _route_by_filename(
                {
                    "bad.docx": [ValueError("spec-local crash")],
                    "good.docx": [review_tool_use_response()],
                }
            )
        )
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review(specs)

        assert results["review__good__1"].parse_status == "ok"
        assert results["review__bad__0"].error is not None
        assert set(results) == {"review__bad__0", "review__good__1"}


# ===========================================================================
# 6. Concurrency
# ===========================================================================


class TestConcurrency:
    def test_pool_respects_max_workers(self, monkeypatch):
        active = {"now": 0, "max": 0}
        lock = threading.Lock()

        def route(kwargs):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            _time.sleep(0.03)
            with lock:
                active["now"] -= 1
            return review_tool_use_response()

        client = FakeRealtimeClient(route)
        monkeypatch.setattr(rt, "_get_client", lambda: client)
        specs = [_spec(f"s{i}.docx") for i in range(6)]

        results, _ = run_realtime_review(specs, max_workers=2)

        assert len(results) == 6
        assert active["max"] <= 2

    def test_env_workers_parsing(self, monkeypatch):
        env = "SPEC_CRITIC_REALTIME_REVIEW_WORKERS"
        monkeypatch.delenv(env, raising=False)
        assert realtime_review_max_workers() == 2  # default
        monkeypatch.setenv(env, "6")
        assert realtime_review_max_workers() == 6
        monkeypatch.setenv(env, "0")
        assert realtime_review_max_workers() == 1  # clamp floor
        monkeypatch.setenv(env, "99")
        assert realtime_review_max_workers() == 8  # clamp ceiling
        monkeypatch.setenv(env, "not-a-number")
        assert realtime_review_max_workers() == 2  # malformed → default
        monkeypatch.setenv(env, "   ")
        assert realtime_review_max_workers() == 2  # blank → default


# ===========================================================================
# 7. Oversized-input gate (≥ LARGE_REVIEW_INPUT_THRESHOLD)
# ===========================================================================


class TestOversizedInputGate:
    def test_gate_raises_before_any_spend(self, monkeypatch):
        monkeypatch.setattr(rt, "LARGE_REVIEW_INPUT_THRESHOLD", 10)

        def _no_client():
            raise AssertionError("client must never be constructed when the gate fires")

        monkeypatch.setattr(rt, "_get_client", _no_client)

        with pytest.raises(ValueError) as excinfo:
            run_realtime_review([_spec("huge.docx")], model=REVIEW_MODEL_DEFAULT)

        message = str(excinfo.value)
        assert "huge.docx" in message
        assert "batch mode" in message

    def test_gate_skipped_when_model_has_no_extended_output(self, monkeypatch):
        # Mirror of _resolve_extended_output: a model the 300k beta does not
        # whitelist gains nothing from batch, so the gate must not fire.
        monkeypatch.setattr(rt, "LARGE_REVIEW_INPUT_THRESHOLD", 10)
        client = FakeRealtimeClient(lambda kwargs: review_tool_use_response())
        monkeypatch.setattr(rt, "_get_client", lambda: client)

        results, _ = run_realtime_review([_spec("huge.docx")], model=MODEL_HAIKU_45)

        assert results["review__huge__0"].parse_status == "ok"


# ===========================================================================
# 8. Transport plumbing through start_batch_review / collect
# ===========================================================================


def _fake_prepared():
    spec = ExtractedSpec(filename="a.docx", content="body", word_count=1)
    return pl._PreparedSpecs(specs=[spec], leed_alerts=[], placeholder_alerts=[])


class TestModePlumbing:
    def test_realtime_start_builds_job_stub_submission(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pl, "_prepare_specs", lambda **kw: _fake_prepared())

        def _no_batch(*args, **kwargs):
            raise AssertionError("submit_review_batch must not run on the realtime transport")

        monkeypatch.setattr(pl, "submit_review_batch", _no_batch)

        request_map = {"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}}
        fake_results = {"review__a__0": ReviewResult(findings=[])}
        captured: dict = {}

        def fake_runner(specs, **kwargs):
            captured["specs"] = list(specs)
            captured["kwargs"] = kwargs
            return dict(fake_results), dict(request_map)

        monkeypatch.setattr(pl, "run_realtime_review", fake_runner)

        submission = pl.start_batch_review(input_dir=tmp_path, review_transport="realtime")

        assert submission.review_transport == "realtime"
        assert submission.realtime_results == fake_results
        assert submission.job.batch_id == REALTIME_JOB_SENTINEL
        assert submission.job.request_map == request_map
        assert submission.job.created_at > 0
        assert submission.job.status == "completed"
        assert submission.review_request_ids == ["review__a__0"]
        assert [s.filename for s in captured["specs"]] == ["a.docx"]

    def test_batch_default_is_untouched(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pl, "_prepare_specs", lambda **kw: _fake_prepared())

        def _no_realtime(*args, **kwargs):
            raise AssertionError("realtime runner must not run on the batch transport")

        monkeypatch.setattr(pl, "run_realtime_review", _no_realtime)

        def fake_submit(specs, *, project_context, model, cycle, pre_detected_alerts):
            return BatchJob(
                batch_id="batch_1",
                job_type="review",
                request_map={"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}},
                created_at=time.time(),
            )

        monkeypatch.setattr(pl, "submit_review_batch", fake_submit)

        submission = pl.start_batch_review(input_dir=tmp_path)

        assert submission.review_transport == "batch"
        assert submission.realtime_results is None
        assert submission.job.batch_id == "batch_1"

    def test_collect_never_touches_batch_retrieval(self, monkeypatch):
        spec = _spec("a.docx", content="body text for anchors")
        finding = _finding("dangling reference", filename="a.docx")
        request_map = {"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}}
        results = {"review__a__0": ReviewResult(findings=[finding], parse_status="ok")}
        submission = _realtime_submission([spec], results, request_map)

        def _boom(*args, **kwargs):
            raise AssertionError("batch retrieval / repair must not run on realtime collect")

        monkeypatch.setattr(pl, "retrieve_review_results", _boom)
        monkeypatch.setattr(pl, "_recover_retryable_review_batch_results", _boom)

        state = collect_review_batch_results(submission)

        assert len(state.review_result.findings) == 1
        # The shared post-processing ran: dedup stamped the rf- finding id.
        assert state.review_result.findings[0].finding_id.startswith("rf-")
        assert state.truncated_specs == []


# ===========================================================================
# 9. verify_findings_for_run — the shared verification-transport helper
# ===========================================================================


class TestVerifyFindingsForRun:
    def test_realtime_arm_pool_and_exactly_once(self, monkeypatch):
        f_local = _finding("resolved locally")
        f_ok = _finding("verifies cleanly")
        f_crash = _finding("worker crashes")

        def fake_prepass(findings, **kwargs):
            # The pre-pass stamps local results and returns the remainder.
            f_local.verification = VerificationResult(
                verdict="UNVERIFIED", explanation="local skip"
            )
            return [f_ok, f_crash]

        pool_calls: list[Finding] = []

        def fake_verify(finding, **kwargs):
            pool_calls.append(finding)
            if finding is f_crash:
                raise RuntimeError("streaming worker crash")
            return VerificationResult(verdict="CONFIRMED", explanation="grounded")

        monkeypatch.setattr(pl, "prepare_findings_for_verification", fake_prepass)
        monkeypatch.setattr(pl, "verify_finding", fake_verify)

        verify_findings_for_run([f_local, f_ok, f_crash], transport="realtime")

        # Exactly-once: every finding ends with one result; the pre-pass
        # finding never re-enters the pool; the crash surfaces honestly.
        assert f_local.verification.explanation == "local skip"
        assert f_ok.verification.verdict == "CONFIRMED"
        assert f_crash.verification.verification_failed is True
        assert sorted(f.issue for f in pool_calls) == sorted(
            [f_ok.issue, f_crash.issue]
        )

    def test_realtime_arm_all_resolved_locally(self, monkeypatch):
        f_local = _finding("resolved locally")
        monkeypatch.setattr(
            pl, "prepare_findings_for_verification", lambda findings, **kw: []
        )

        def _no_pool(*args, **kwargs):
            raise AssertionError("verify_finding must not run when nothing remains")

        monkeypatch.setattr(pl, "verify_finding", _no_pool)
        verify_findings_for_run([f_local], transport="realtime")

    def test_batch_arm_delegates_to_existing_pair(self, monkeypatch):
        f = _finding("batch verified")
        recorded: dict = {}
        job = BatchJob(batch_id="verify-1", job_type="verify", request_map={}, created_at=0.0)

        def fake_start(findings, **kwargs):
            recorded["start"] = list(findings)
            return job

        def fake_collect(got_job, findings, **kwargs):
            recorded["collect"] = (got_job, list(findings))
            return findings

        monkeypatch.setattr(pl, "start_batch_verification", fake_start)
        monkeypatch.setattr(pl, "collect_batch_verification_results", fake_collect)

        verify_findings_for_run([f], transport="batch")

        assert recorded["start"] == [f]
        assert recorded["collect"][0] is job

    def test_batch_arm_all_resolved_locally_early_out(self, monkeypatch):
        f = _finding("locally resolved on the batch arm")
        monkeypatch.setattr(pl, "start_batch_verification", lambda findings, **kw: None)

        def _no_collect(*args, **kwargs):
            raise AssertionError("collect must not run when no batch was submitted")

        monkeypatch.setattr(pl, "collect_batch_verification_results", _no_collect)
        verify_findings_for_run([f], transport="batch")

    def test_empty_findings_is_a_noop(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("no verification surface may run for zero findings")

        monkeypatch.setattr(pl, "prepare_findings_for_verification", _boom)
        monkeypatch.setattr(pl, "start_batch_verification", _boom)
        verify_findings_for_run([], transport="realtime")
        verify_findings_for_run([], transport="batch")


# ===========================================================================
# 10. Headless driver end-to-end on the realtime transport
# ===========================================================================


class TestHeadlessRealtimeDriver:
    def test_end_to_end_no_batch_surface(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
        spec = _spec("a.docx", content="body text")
        finding = _finding("needs web verification", filename="a.docx")
        request_map = {"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}}
        results = {"review__a__0": ReviewResult(findings=[finding], parse_status="ok")}
        submission = _realtime_submission([spec], results, request_map)

        def _boom(*args, **kwargs):
            raise AssertionError("batch API surface must not be touched on a realtime run")

        monkeypatch.setattr(pl, "retrieve_review_results", _boom)
        monkeypatch.setattr(pl, "start_batch_verification", _boom)
        monkeypatch.setattr(pl, "collect_batch_verification_results", _boom)
        monkeypatch.setattr(
            pl, "prepare_findings_for_verification", lambda findings, **kw: list(findings)
        )
        monkeypatch.setattr(
            pl,
            "verify_finding",
            lambda f, **kw: VerificationResult(verdict="CONFIRMED", explanation="grounded"),
        )

        result = pl.run_batch_collection_headless(submission)

        assert result.failed_review_specs == []
        assert len(result.review_result.findings) == 1
        assert result.review_result.findings[0].verification.verdict == "CONFIRMED"

    def test_end_to_end_with_failed_spec(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
        specs = [_spec("bad.docx"), _spec("good.docx")]
        good_finding = _finding("real issue", filename="good.docx")
        request_map = {
            "review__bad__0": {"filename": "bad.docx", "index": 0, "type": "review"},
            "review__good__1": {"filename": "good.docx", "index": 1, "type": "review"},
        }
        results = {
            "review__bad__0": ReviewResult(
                findings=[],
                parse_status="incomplete",
                error="Review response incomplete (stop_reason: max_tokens)",
            ),
            "review__good__1": ReviewResult(findings=[good_finding], parse_status="ok"),
        }
        submission = _realtime_submission(specs, results, request_map)
        monkeypatch.setattr(
            pl, "prepare_findings_for_verification", lambda findings, **kw: []
        )

        result = pl.run_batch_collection_headless(submission)

        assert result.failed_review_specs == ["bad.docx"]
        assert len(result.review_result.findings) == 1


# ===========================================================================
# 11. No pending state for realtime
# ===========================================================================


class TestNoPendingState:
    def test_from_submission_refuses_realtime(self):
        submission = _realtime_submission(
            [_spec("a.docx")],
            {"review__a__0": ReviewResult(findings=[])},
            {"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}},
        )
        with pytest.raises(ValueError):
            PendingBatch.from_submission(submission)

    def test_from_submission_batch_still_works(self):
        job = BatchJob(
            batch_id="msgbatch_123",
            job_type="review",
            request_map={"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}},
            created_at=time.time(),
        )
        submission = BatchSubmission(job=job, files_reviewed=["a.docx"])
        pending = PendingBatch.from_submission(submission)
        assert pending.batch_id == "msgbatch_123"


# ===========================================================================
# 12. ui_state transport persistence
# ===========================================================================


class TestUiStateTransport:
    @pytest.fixture(autouse=True)
    def _tmp_state(self, monkeypatch, tmp_path):
        self.state_path = tmp_path / "ui_state.json"
        monkeypatch.setenv("SPEC_CRITIC_UI_STATE_PATH", str(self.state_path))

    def test_default_is_batch(self):
        from src.core.ui_state import load_review_transport

        assert load_review_transport() == "batch"

    def test_round_trip(self):
        from src.core.ui_state import load_review_transport, save_review_transport

        save_review_transport("realtime")
        assert load_review_transport() == "realtime"
        save_review_transport("batch")
        assert load_review_transport() == "batch"

    def test_invalid_value_is_never_written(self):
        from src.core.ui_state import load_review_transport, save_review_transport

        save_review_transport("realtime")
        save_review_transport("yolo")  # dropped, not persisted
        assert load_review_transport() == "realtime"

    def test_malformed_file_reads_as_batch(self):
        from src.core.ui_state import load_review_transport

        self.state_path.write_text("{not json", encoding="utf-8")
        assert load_review_transport() == "batch"

    def test_preserves_other_keys(self):
        from src.core.ui_state import (
            load_review_transport,
            load_selected_module_id,
            save_review_transport,
            save_selected_module_id,
        )

        save_selected_module_id("california_k12_mep")
        save_review_transport("realtime")
        assert load_selected_module_id() == "california_k12_mep"
        assert load_review_transport() == "realtime"


class TestUiStateShowTracing:
    @pytest.fixture(autouse=True)
    def _tmp_state(self, monkeypatch, tmp_path):
        self.state_path = tmp_path / "ui_state.json"
        monkeypatch.setenv("SPEC_CRITIC_UI_STATE_PATH", str(self.state_path))

    def test_default_is_false(self):
        from src.core.ui_state import load_show_tracing_tools

        assert load_show_tracing_tools() is False

    def test_round_trip(self):
        from src.core.ui_state import (
            load_show_tracing_tools,
            save_show_tracing_tools,
        )

        save_show_tracing_tools(True)
        assert load_show_tracing_tools() is True
        save_show_tracing_tools(False)
        assert load_show_tracing_tools() is False

    def test_malformed_file_reads_as_false(self):
        from src.core.ui_state import load_show_tracing_tools

        self.state_path.write_text("{not json", encoding="utf-8")
        assert load_show_tracing_tools() is False

    def test_non_bool_value_degrades_to_false(self):
        import json

        from src.core.ui_state import load_show_tracing_tools

        self.state_path.write_text(
            json.dumps({"show_tracing_tools": "yes"}), encoding="utf-8"
        )
        assert load_show_tracing_tools() is False

    def test_preserves_other_keys(self):
        from src.core.ui_state import (
            load_selected_module_id,
            load_show_tracing_tools,
            save_selected_module_id,
            save_show_tracing_tools,
        )

        save_selected_module_id("california_k12_mep")
        save_show_tracing_tools(True)
        assert load_selected_module_id() == "california_k12_mep"
        assert load_show_tracing_tools() is True


# ===========================================================================
# 13. Diagnostics telemetry from the runner
# ===========================================================================


class TestDiagnosticsTelemetry:
    def test_one_row_per_spec_happy_path(self, monkeypatch):
        specs = [_spec("a.docx"), _spec("b.docx")]
        client = FakeRealtimeClient(
            _route_by_filename(
                {
                    "a.docx": [review_tool_use_response()],
                    "b.docx": [review_tool_use_response()],
                }
            )
        )
        monkeypatch.setattr(rt, "_get_client", lambda: client)
        diag = FakeDiagnostics()

        run_realtime_review(specs, diagnostics=diag)

        assert len(diag.calls) == 2
        for row in diag.calls:
            assert row["phase"] == "review"
            assert row["mode"] == "realtime"
            assert row["retry_status"] == "initial"
            assert row["level"] == "info"
            assert row["extra"]["filename"] in {"a.docx", "b.docx"}
            assert row["max_output_tokens"] == phase_output_cap(
                PHASE_REVIEW, model=REVIEW_MODEL_DEFAULT
            )

    def test_repair_adds_retry_row(self, monkeypatch):
        client = FakeRealtimeClient(
            _route_by_filename(
                {"a.docx": [max_tokens_incomplete_response(), review_tool_use_response()]}
            )
        )
        monkeypatch.setattr(rt, "_get_client", lambda: client)
        diag = FakeDiagnostics()

        run_realtime_review([_spec("a.docx")], diagnostics=diag)

        assert [row["retry_status"] for row in diag.calls] == ["initial", "retry"]
        assert diag.calls[0]["level"] == "error"  # truncated initial call
        assert diag.calls[1]["level"] == "info"  # successful repair

    def test_terminal_failure_records_error_row(self, monkeypatch):
        client = FakeRealtimeClient(lambda kwargs: ValueError("boom"))
        monkeypatch.setattr(rt, "_get_client", lambda: client)
        diag = FakeDiagnostics()

        run_realtime_review([_spec("a.docx")], diagnostics=diag)

        assert len(diag.calls) == 1
        assert diag.calls[0]["level"] == "error"
        assert diag.calls[0]["mode"] == "realtime"
