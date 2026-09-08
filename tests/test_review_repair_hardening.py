"""Review-repair pass hardening (batch transport).

``pipeline._recover_retryable_review_batch_results`` re-submits the retryable
failed items of a review batch as one instructed repair batch. What's locked
in here:

* A model refusal (``parse_status="refusal"``) is never re-submitted — the
  instructed repair cannot address it — but it still lands in
  ``truncated_specs`` / ``errors`` / ``combined.error`` so the report banner
  and the amber terminal state fire (Fix 5, batch half).
* The repair submit → poll → retrieve body can no longer raise out of
  ``collect_review_batch_results`` and discard the already-paid primary
  results: every failure (submit exception, poll exception, detach, retrieve
  exception) logs the repair batch id when one exists and returns the primary
  ``results_by_request`` unchanged (A-5).
* The repair batch id is logged at submit and in every non-success branch,
  and stamped onto the parent's saved ``PendingBatch`` (``repair_batch_id``)
  when a matching record exists — never breaking the repair when persistence
  fails (A-6).
* Repair-poll progress is forwarded to ``log`` (B-9).
* The repair ``<pre_detected>`` block matches the original: ``profile_country``
  is derived from the submission's project profile (so ``polity_alerts``
  populate on a profile-bearing run) and the project-level ``naming_alerts``
  are merged in (B-9).
"""
from __future__ import annotations

import types

import pytest

from src.batch.batch import BatchJob, BatchStatus
from src.batch.batch_runtime import PollOutcome
from src.input.extractor import ExtractedSpec
from src.orchestration import batch_resume as br
from src.orchestration import pipeline as pl
from src.orchestration.batch_resume import PendingBatch, load_pending_batch, save_pending_batch
from src.orchestration.pipeline import (
    BatchSubmission,
    _is_retryable_batch_review_result,
    _recover_retryable_review_batch_results,
    collect_review_batch_results,
    finalize_batch_result,
)
from src.review.reviewer import ReviewResult


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _spec(name: str) -> ExtractedSpec:
    body = f"PART 1 - GENERAL. Body of {name}. Provide equipment per code."
    return ExtractedSpec(filename=name, content=body, word_count=len(body.split()))


def _submission(
    filenames: list[str],
    *,
    batch_id: str = "msgbatch_PARENT",
    module_id: str = "california_k12_mep",
    project_profile: dict | None = None,
) -> BatchSubmission:
    request_map = {
        f"review__{i}__{i}": {"filename": name, "index": i, "type": "review"}
        for i, name in enumerate(filenames)
    }
    job = BatchJob(batch_id=batch_id, job_type="review", request_map=request_map, created_at=0.0)
    return BatchSubmission(
        job=job,
        files_reviewed=list(filenames),
        review_request_ids=list(request_map),
        model="m",
        prepared_specs=[_spec(name) for name in filenames],
        module_id=module_id,
        project_profile=project_profile,
    )


def _rid(i: int) -> str:
    return f"review__{i}__{i}"


class _Log:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def __call__(self, msg: str, *, level: str = "info", **_kw):
        self.lines.append((msg, level))

    def text(self, level: str | None = None) -> str:
        return "\n".join(m for m, lvl in self.lines if level is None or lvl == level)


def _fake_submit(batch_id: str = "msgbatch_REPAIR", *, captured: dict | None = None):
    def submit(repair_specs, **kwargs):
        if captured is not None:
            captured["specs"] = list(repair_specs)
            captured["kwargs"] = dict(kwargs)
        return BatchJob(
            batch_id=batch_id,
            job_type="review",
            request_map={
                f"review__r__{i}": {"filename": s.filename, "index": i, "type": "review"}
                for i, s in enumerate(repair_specs)
            },
            created_at=0.0,
        )

    return submit


def _ended(*_a, **_k):
    return PollOutcome(terminal=True, terminal_status="ended")


def _ok_retrieve(job, *, model):
    return {cid: ReviewResult(findings=[], parse_status="ok") for cid in job.request_map}


@pytest.fixture
def isolated_pending_state(tmp_path, monkeypatch):
    path = tmp_path / "pending_batch.json"
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(path))
    return path


# ===========================================================================
# 1. Refusals are never re-submitted (Fix 5, batch half)
# ===========================================================================


class TestRefusalIsNotRepaired:
    def test_is_retryable_matrix(self):
        assert _is_retryable_batch_review_result(None) is True
        assert _is_retryable_batch_review_result(ReviewResult(parse_status="incomplete")) is True
        assert _is_retryable_batch_review_result(ReviewResult(parse_status="parse_error")) is True
        assert _is_retryable_batch_review_result(ReviewResult(parse_status="ok")) is False
        assert _is_retryable_batch_review_result(
            ReviewResult(findings=[], error="Batch request errored: overloaded")
        ) is True
        assert _is_retryable_batch_review_result(
            ReviewResult(parse_status="refusal", error="Review refused by the model (stop_reason: refusal)")
        ) is False

    def test_refused_item_is_not_resubmitted_and_still_fails_the_spec(self, monkeypatch):
        sub = _submission(["A.docx", "B.docx"])
        results = {
            _rid(0): ReviewResult(findings=[], parse_status="ok"),
            _rid(1): ReviewResult(
                findings=[], parse_status="refusal", stop_reason="refusal",
                error="Review refused by the model (stop_reason: refusal, category: general_harms)",
            ),
        }

        def _no_submit(*_a, **_k):
            raise AssertionError("a refusal must not trigger a repair batch")

        monkeypatch.setattr(pl, "submit_review_batch", _no_submit)
        monkeypatch.setattr(pl, "retrieve_review_results", lambda job, *, model: dict(results))

        state = collect_review_batch_results(sub)

        assert state.truncated_specs == ["B.docx"]
        assert "refus" in state.review_result.error.lower()
        assert "general_harms" in state.review_result.error
        assert "truncated" not in state.review_result.error.lower()
        assert "not retried" in state.review_result.error
        final = finalize_batch_result(state)
        assert final.failed_review_specs == ["B.docx"]

    def test_mixed_refusal_and_truncation_only_resubmits_the_truncated_item(self, monkeypatch):
        sub = _submission(["A.docx", "B.docx", "C.docx"])
        results = {
            _rid(0): ReviewResult(findings=[], parse_status="refusal", error="refused"),
            _rid(1): ReviewResult(findings=[], parse_status="incomplete", stop_reason="max_tokens"),
            _rid(2): ReviewResult(findings=[], parse_status="ok"),
        }
        captured: dict = {}
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit(captured=captured))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)

        out = _recover_retryable_review_batch_results(sub, dict(results), log=_Log())

        assert [s.filename for s in captured["specs"]] == ["B.docx"]
        assert out[_rid(1)].parse_status == "ok"  # repaired
        assert out[_rid(0)].parse_status == "refusal"  # untouched, still failed


# ===========================================================================
# 2. The repair body never discards primary results (A-5)
# ===========================================================================


class TestRepairFailuresKeepPrimaryResults:
    def _results(self):
        return {
            _rid(0): ReviewResult(findings=[], parse_status="ok"),
            _rid(1): ReviewResult(findings=[], parse_status="incomplete", stop_reason="max_tokens"),
        }

    def test_submit_exception_returns_primary_results_and_logs_error(self, monkeypatch):
        sub = _submission(["A.docx", "B.docx"])
        results = self._results()

        def _submit_boom(*_a, **_k):
            raise RuntimeError("529 overloaded_error")

        monkeypatch.setattr(pl, "submit_review_batch", _submit_boom)
        monkeypatch.setattr(pl, "poll_batch_bounded", lambda *a, **k: pytest.fail("poll must not run"))
        log = _Log()

        out = _recover_retryable_review_batch_results(sub, results, log=log)

        assert out is results
        assert out[_rid(0)].parse_status == "ok"
        assert out[_rid(1)].parse_status == "incomplete"
        errors = log.text("error")
        assert "Review repair batch failed" in errors
        assert "529 overloaded_error" in errors
        assert "primary review results are retained" in errors

    def test_poll_exception_logs_repair_batch_id_and_keeps_primary(self, monkeypatch):
        sub = _submission(["A.docx", "B.docx"])
        results = self._results()
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))

        def _poll_boom(*_a, **_k):
            raise ConnectionError("connection reset")

        monkeypatch.setattr(pl, "poll_batch_bounded", _poll_boom)
        log = _Log()

        out = _recover_retryable_review_batch_results(sub, results, log=log)

        assert out is results
        assert out[_rid(1)].parse_status == "incomplete"
        assert "msgbatch_REPAIR_777" in log.text("error")
        assert "connection reset" in log.text("error")

    def test_retrieve_exception_keeps_primary(self, monkeypatch):
        sub = _submission(["A.docx", "B.docx"])
        results = self._results()
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)

        def _retrieve_boom(*_a, **_k):
            raise RuntimeError("results expired")

        monkeypatch.setattr(pl, "retrieve_review_results", _retrieve_boom)
        log = _Log()

        out = _recover_retryable_review_batch_results(sub, results, log=log)

        assert out is results
        assert "msgbatch_REPAIR_777" in log.text("error")
        assert "results expired" in log.text("error")

    def test_collect_survives_repair_exception_end_to_end(self, monkeypatch):
        # The contract that matters to the operator: collect returns with the
        # healthy spec's result and the failed spec surfaced, instead of
        # raising and losing everything.
        sub = _submission(["A.docx", "B.docx"])
        from src.review.reviewer import Finding

        good = Finding(
            severity="MEDIUM", fileName="A.docx", section="1", issue="x",
            actionType="REPORT_ONLY", existingText=None, replacementText=None, codeReference="",
        )
        results = {
            _rid(0): ReviewResult(findings=[good], parse_status="ok"),
            _rid(1): ReviewResult(findings=[], parse_status="incomplete", stop_reason="max_tokens"),
        }
        monkeypatch.setattr(pl, "retrieve_review_results", lambda job, *, model: dict(results))

        def _submit_boom(*_a, **_k):
            raise RuntimeError("529")

        monkeypatch.setattr(pl, "submit_review_batch", _submit_boom)

        state = collect_review_batch_results(sub, log=_Log())

        assert state.truncated_specs == ["B.docx"]
        assert [f.issue for f in state.review_result.findings] == ["x"]


# ===========================================================================
# 3. Repair batch id is logged and persisted (A-6)
# ===========================================================================


class TestRepairBatchIdVisibility:
    def _results(self):
        return {
            _rid(0): ReviewResult(findings=[], parse_status="ok"),
            _rid(1): ReviewResult(findings=[], parse_status="incomplete", stop_reason="max_tokens"),
        }

    def test_id_logged_at_submit_and_on_success(self, monkeypatch, isolated_pending_state):
        sub = _submission(["A.docx", "B.docx"])
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        log = _Log()

        _recover_retryable_review_batch_results(sub, self._results(), log=log)

        assert "Review repair batch submitted: msgbatch_REPAIR_777" in log.text("step")
        assert "msgbatch_REPAIR_777 recovered 1/1" in log.text("success")

    def test_id_logged_when_poll_detaches(self, monkeypatch, isolated_pending_state):
        sub = _submission(["A.docx", "B.docx"])
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(
            pl, "poll_batch_bounded",
            lambda *a, **k: PollOutcome(detached=True, detach_reason="max_elapsed"),
        )
        monkeypatch.setattr(
            pl, "retrieve_review_results", lambda *a, **k: pytest.fail("must not retrieve")
        )
        log = _Log()
        results = self._results()

        out = _recover_retryable_review_batch_results(sub, results, log=log)

        assert out is results
        warning = log.text("warning")
        assert "msgbatch_REPAIR_777" in warning
        assert "max_elapsed" in warning
        assert "still be running" in warning

    def test_id_logged_when_poll_fails(self, monkeypatch, isolated_pending_state):
        sub = _submission(["A.docx", "B.docx"])
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(
            pl, "poll_batch_bounded",
            lambda *a, **k: PollOutcome(poll_failed=True, poll_error="poll_error_threshold: 503"),
        )
        log = _Log()

        _recover_retryable_review_batch_results(sub, self._results(), log=log)

        warning = log.text("warning")
        assert "msgbatch_REPAIR_777" in warning
        assert "503" in warning

    def test_id_persisted_onto_matching_pending_record(self, monkeypatch, isolated_pending_state):
        sub = _submission(["A.docx", "B.docx"], batch_id="msgbatch_PARENT")
        save_pending_batch(PendingBatch.from_submission(sub))
        assert load_pending_batch().repair_batch_id is None
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        log = _Log()

        _recover_retryable_review_batch_results(sub, self._results(), log=log)

        loaded = load_pending_batch()
        assert loaded is not None
        assert loaded.batch_id == "msgbatch_PARENT"  # parent record kept
        assert loaded.repair_batch_id == "msgbatch_REPAIR_777"
        assert "Recorded repair batch msgbatch_REPAIR_777" in log.text("info")

    def test_id_not_persisted_without_matching_record(self, monkeypatch, isolated_pending_state):
        # A different batch's record on disk (or a program manifest, which
        # load_pending_batch reads as None) must never be stamped.
        other = _submission(["X.docx"], batch_id="msgbatch_OTHER")
        save_pending_batch(PendingBatch.from_submission(other))
        sub = _submission(["A.docx", "B.docx"], batch_id="msgbatch_PARENT")
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        log = _Log()

        _recover_retryable_review_batch_results(sub, self._results(), log=log)

        loaded = load_pending_batch()
        assert loaded.batch_id == "msgbatch_OTHER"
        assert loaded.repair_batch_id is None
        info = log.text("info")
        assert "msgbatch_REPAIR_777" in info
        assert "not recorded" in info

    def test_id_not_persisted_for_program_manifest(self, monkeypatch, isolated_pending_state):
        isolated_pending_state.write_text(
            '{"schema_version": 2, "record_type": "program", "program_id": "hyperscale_datacenter", '
            '"assignments": [], "partitions": {"datacenter_fire": {"batch_id": "msgbatch_PARENT"}}}',
            encoding="utf-8",
        )
        before = isolated_pending_state.read_text(encoding="utf-8")
        sub = _submission(["A.docx", "B.docx"], batch_id="msgbatch_PARENT", module_id="datacenter_fire")
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        log = _Log()

        out = _recover_retryable_review_batch_results(sub, self._results(), log=log)

        assert out[_rid(1)].parse_status == "ok"  # repair still worked
        assert isolated_pending_state.read_text(encoding="utf-8") == before  # manifest untouched
        assert "msgbatch_REPAIR_777" in log.text("info")

    def test_persistence_failure_never_breaks_the_repair(self, monkeypatch, isolated_pending_state):
        sub = _submission(["A.docx", "B.docx"], batch_id="msgbatch_PARENT")
        save_pending_batch(PendingBatch.from_submission(sub))

        def _save_boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(br, "save_pending_batch", _save_boom)
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit("msgbatch_REPAIR_777"))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        log = _Log()

        out = _recover_retryable_review_batch_results(sub, self._results(), log=log)

        assert out[_rid(1)].parse_status == "ok"
        warning = log.text("warning")
        assert "msgbatch_REPAIR_777" in warning
        assert "disk full" in warning


# ===========================================================================
# 4. Repair-poll progress reaches the log (B-9)
# ===========================================================================


class TestRepairPollProgress:
    def test_progress_forwarded_to_log(self, monkeypatch, isolated_pending_state):
        sub = _submission(["A.docx", "B.docx"])
        results = {
            _rid(0): ReviewResult(findings=[], parse_status="ok"),
            _rid(1): ReviewResult(findings=[], parse_status="incomplete", stop_reason="max_tokens"),
        }
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit())

        def _poll(batch_id, *, policy, log, progress_cb, **_k):
            progress_cb(BatchStatus(status="in_progress", processing=1, succeeded=0, errored=0,
                                    canceled=0, expired=0, total=1))
            progress_cb(BatchStatus(status="ended", processing=0, succeeded=1, errored=0,
                                    canceled=0, expired=0, total=1))
            return PollOutcome(terminal=True, terminal_status="ended")

        monkeypatch.setattr(pl, "poll_batch_bounded", _poll)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        log = _Log()

        _recover_retryable_review_batch_results(sub, results, log=log)

        info = log.text("info")
        assert "Repair batch: 0/1 done, 1 processing, 0 errored" in info
        assert "Repair batch: 1/1 done, 0 processing, 0 errored" in info


# ===========================================================================
# 5. Repair <pre_detected> parity with the original request (B-9)
# ===========================================================================


_COMPLETE_PROFILE = {
    "city": "Ashburn",
    "state_or_province": "VA",
    "country": "US",
    "client_name": "Example Client",
}


class TestRepairPreDetectedParity:
    # Two space-separated CSI names + one dash-separated one: the dash file
    # is the non-dominant style, so ``detect_inconsistent_file_naming``
    # (the real detector) flags it — exactly the project-level alert the
    # original ``_prepare_specs`` merges into that file's block.
    FILES = ["21 13 13 - Wet Pipe.docx", "21 13 16 - Dry Pipe.docx", "21-30-00 - Fire Pumps.docx"]

    def _run(self, monkeypatch, sub, failed_index: int = 2):
        results = {
            _rid(i): ReviewResult(findings=[], parse_status="ok") for i in range(len(self.FILES))
        }
        results[_rid(failed_index)] = ReviewResult(
            findings=[], parse_status="incomplete", stop_reason="max_tokens"
        )
        captured: dict = {}
        seen: dict = {}

        def fake_preprocess(content, filename, *, cycle, profile_country=None):
            seen[filename] = profile_country
            polity = (
                [{"filename": filename, "type": "Wrong-polity token", "match": "UL listed",
                  "deterministic_rule": "wrong_polity_token"}]
                if profile_country
                else []
            )
            return types.SimpleNamespace(
                leed_alerts=[], placeholder_alerts=[{"filename": filename, "type": "Placeholder", "match": "TBD"}],
                code_cycle_alerts=[], structural_alerts=[], template_marker_alerts=[],
                invalid_code_cycle_alerts=[], duplicate_paragraph_alerts=[],
                polity_alerts=polity,
            )

        monkeypatch.setattr(pl, "preprocess_spec", fake_preprocess)
        monkeypatch.setattr(pl, "submit_review_batch", _fake_submit(captured=captured))
        monkeypatch.setattr(pl, "poll_batch_bounded", _ended)
        monkeypatch.setattr(pl, "retrieve_review_results", _ok_retrieve)
        _recover_retryable_review_batch_results(sub, results, log=_Log())
        return captured, seen

    def test_profile_bearing_run_carries_polity_and_naming_alerts(self, monkeypatch, isolated_pending_state):
        sub = _submission(self.FILES, module_id="datacenter_fire", project_profile=_COMPLETE_PROFILE)

        captured, seen = self._run(monkeypatch, sub)

        failed = "21-30-00 - Fire Pumps.docx"
        assert [s.filename for s in captured["specs"]] == [failed]
        assert seen == {failed: "US"}  # profile country reached preprocess_spec
        alerts = captured["kwargs"]["pre_detected_alerts"][failed]
        rules = [a.get("deterministic_rule") for a in alerts]
        assert "wrong_polity_token" in rules
        assert "inconsistent_filename" in rules
        naming = [a for a in alerts if a.get("deterministic_rule") == "inconsistent_filename"]
        assert naming[0]["filename"] == failed
        assert naming[0]["dominant_style"] == "space"
        # Per-spec alerts first (original order), naming alerts appended last.
        assert alerts[0]["type"] == "Placeholder"
        assert alerts[-1]["deterministic_rule"] == "inconsistent_filename"
        # Cycle + instruction parity with the original builder.
        assert captured["kwargs"]["cycle"] is pl.get_module("datacenter_fire").cycle
        assert captured["kwargs"]["retry_instruction"]

    def test_profile_less_module_keeps_country_none_even_with_profile_dict(
        self, monkeypatch, isolated_pending_state
    ):
        # The CA module never enables the polity detector, so a stray
        # profile dict must not switch it on for the repair either.
        sub = _submission(self.FILES, module_id="california_k12_mep", project_profile=_COMPLETE_PROFILE)

        captured, seen = self._run(monkeypatch, sub)

        failed = "21-30-00 - Fire Pumps.docx"
        assert seen == {failed: None}
        rules = [a.get("deterministic_rule") for a in captured["kwargs"]["pre_detected_alerts"][failed]]
        assert "wrong_polity_token" not in rules
        assert "inconsistent_filename" in rules  # naming still merged

    def test_incomplete_profile_keeps_country_none(self, monkeypatch, isolated_pending_state):
        partial = dict(_COMPLETE_PROFILE, client_name="")
        sub = _submission(self.FILES, module_id="datacenter_fire", project_profile=partial)

        _captured, seen = self._run(monkeypatch, sub)

        assert seen == {"21-30-00 - Fire Pumps.docx": None}

    def test_naming_alert_for_another_file_is_not_attached_to_the_repair_spec(
        self, monkeypatch, isolated_pending_state
    ):
        # The dash-named file is the flagged one; repairing a SPACE-named
        # file must not inherit that alert (alerts route to their own file).
        sub = _submission(self.FILES, module_id="datacenter_fire", project_profile=_COMPLETE_PROFILE)

        captured, _seen = self._run(monkeypatch, sub, failed_index=0)

        repaired = "21 13 13 - Wet Pipe.docx"
        assert [s.filename for s in captured["specs"]] == [repaired]
        rules = [a.get("deterministic_rule") for a in captured["kwargs"]["pre_detected_alerts"][repaired]]
        assert "inconsistent_filename" not in rules
        assert "wrong_polity_token" in rules
