"""Recovering a still-running batch: poll to completion first, then reconstruct.

The bare-id recovery paths (the GUI "Recover batch…" dialog and
``scripts/recover_batch.py --batch-id``) rebuild the request map from the
batch's *results* stream (``thin_submission_from_batch_results``), which does
not exist until the batch ends — pointing them at an in-progress batch failed
with the SDK's raw error ("No ``results_url`` for the given batch; Has it
finished processing? in_progress"; observed live recovering a batch ~4h into
slow remote processing). ``batch_runtime.ensure_batch_ended`` closes the gap:

- one immediate status check first, so a typo'd batch id / auth failure raises
  at once (never absorbed into the poll loop's consecutive-error backoff) and
  an already-ended batch passes through with no waiting;
- the standard bounded poll loop when the batch is still processing;
- a typed ``BatchNotFinishedError`` when the poll bound is hit, so both
  recovery surfaces render "still processing — try again later" instead of a
  raw SDK error.
"""
from __future__ import annotations

import pytest

import src.batch.batch_runtime as rt
from src.batch.batch import BatchStatus
from src.batch.batch_runtime import (
    BatchNotFinishedError,
    DEFAULT_REVIEW_POLL_POLICY,
    PollPolicy,
    ensure_batch_ended,
    is_terminal_batch_status,
)


def _status(
    *, processing: int = 2, succeeded: int = 0, status: str = "in_progress"
) -> BatchStatus:
    return BatchStatus(
        status=status,
        processing=processing,
        succeeded=succeeded,
        errored=0,
        canceled=0,
        expired=0,
        total=processing + succeeded,
    )


def _no_sleep(_seconds):  # pragma: no cover - failure path only
    raise AssertionError("must not sleep for an already-ended batch")


def _noop_log(*_a, **_k):
    return None


class TestTerminalStatusHelper:
    def test_terminal_and_non_terminal_statuses(self):
        assert is_terminal_batch_status("ended")
        assert is_terminal_batch_status("canceled")
        assert is_terminal_batch_status("expired")
        assert is_terminal_batch_status("failed")
        assert not is_terminal_batch_status("in_progress")
        assert not is_terminal_batch_status("canceling")
        assert not is_terminal_batch_status("")

    def test_hyphenated_variants_normalize(self):
        # Defensive parity with poll_batch_bounded's legacy normalization.
        assert not is_terminal_batch_status("in-progress")


class TestEnsureBatchEnded:
    def test_ended_batch_returns_after_single_status_check(self, monkeypatch):
        calls = {"n": 0}

        def fake_poll(_bid):
            calls["n"] += 1
            return _status(processing=0, succeeded=2, status="ended")

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        monkeypatch.setattr(rt.time, "sleep", _no_sleep)

        out = ensure_batch_ended(
            "msgbatch_done", policy=PollPolicy(), log=_noop_log
        )
        assert out.status == "ended"
        assert out.succeeded == 2
        assert calls["n"] == 1

    def test_in_progress_batch_is_polled_to_completion(self, monkeypatch):
        # First status is consumed by the immediate pre-check; the poll loop
        # then sees progress and finally the terminal state.
        seq = [
            _status(),
            _status(),
            _status(processing=1, succeeded=1),
            _status(processing=0, succeeded=2, status="ended"),
        ]

        def fake_poll(_bid):
            return seq.pop(0) if seq else _status(
                processing=0, succeeded=2, status="ended"
            )

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        clock = {"t": 0.0}
        monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)
        )

        observed: list[BatchStatus] = []
        out = ensure_batch_ended(
            "msgbatch_slow",
            policy=PollPolicy(),
            log=_noop_log,
            progress_cb=observed.append,
        )
        assert out.status == "ended"
        assert out.succeeded == 2
        # The caller's progress callback saw every poll-loop status, ending
        # with the terminal one.
        assert observed
        assert observed[-1].status == "ended"

    def test_never_finishing_batch_raises_typed_error(self, monkeypatch):
        monkeypatch.setattr(rt, "poll_batch", lambda _bid: _status())
        clock = {"t": 0.0}
        monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + 600)
        )

        with pytest.raises(BatchNotFinishedError) as exc_info:
            ensure_batch_ended(
                "msgbatch_stuck",
                policy=DEFAULT_REVIEW_POLL_POLICY,
                log=_noop_log,
            )
        err = exc_info.value
        assert err.batch_id == "msgbatch_stuck"
        assert err.reason == "max_elapsed"
        # str(exc) must be presentable on its own (the CLI prints it verbatim).
        assert "msgbatch_stuck" in str(err)
        assert "has not finished processing" in str(err)
        assert "0 of 2 requests done" in str(err)

    def test_transient_preflight_failure_falls_through_to_poll_loop(
        self, monkeypatch
    ):
        """A retryable failure (connection / 5xx / rate limit) on the
        preflight status check must NOT abort the recovery — it falls through
        to the bounded poll loop, which re-polls under its own
        consecutive-error backoff. (Codex review P2: the results-download
        path this guard fronts already had retry handling; the preflight
        must not regress an already-ended recovery on a single blip.)"""
        calls = {"n": 0}

        def flaky_poll(_bid):
            calls["n"] += 1
            if calls["n"] == 1:
                # Message matches retry_policy's connection-pattern heuristic
                # -> FailureClass.CONNECTION -> retryable.
                raise ConnectionError("connection reset by peer")
            return _status(processing=0, succeeded=2, status="ended")

        monkeypatch.setattr(rt, "poll_batch", flaky_poll)
        clock = {"t": 0.0}
        monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)
        )

        out = ensure_batch_ended(
            "msgbatch_blip", policy=PollPolicy(), log=_noop_log
        )
        assert out.status == "ended"
        assert calls["n"] == 2

    def test_bad_batch_id_fails_fast_without_retry_backoff(self, monkeypatch):
        """The immediate pre-check must let a non-retryable error (404 /
        auth / unknown) propagate unchanged — never absorbed into the poll
        loop's 10-strike consecutive-error backoff (minutes of sleeping for
        a typo'd id)."""

        class FakeNotFound(Exception):
            status_code = 404

        calls = {"n": 0}

        def fake_poll(_bid):
            calls["n"] += 1
            raise FakeNotFound("not_found")

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        monkeypatch.setattr(rt.time, "sleep", _no_sleep)

        with pytest.raises(FakeNotFound):
            ensure_batch_ended(
                "msgbatch_typo", policy=PollPolicy(), log=_noop_log
            )
        assert calls["n"] == 1

    def test_user_cancel_raises_typed_error(self, monkeypatch):
        import threading

        monkeypatch.setattr(rt, "poll_batch", lambda _bid: _status())
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(BatchNotFinishedError) as exc_info:
            ensure_batch_ended(
                "msgbatch_cxl",
                policy=PollPolicy(),
                log=_noop_log,
                cancel_event=cancel,
            )
        assert exc_info.value.reason == "user_canceled"


# ===========================================================================
# scripts/recover_batch.py — routed-program runs + no defaulted module
# ===========================================================================
#
# The CLI used to call only ``load_pending_batch`` (which reads a program
# manifest as "no pending batch") and defaulted ``--module`` to the CA K-12
# module on the bare-id path — so a detached hyperscale run reported "No
# saved pending batch found", and a bare data-center batch id was collected,
# cross-checked, and verified under CA prompts and cycle.


import json
import types
from pathlib import Path

from src.batch.batch import BatchJob
from src.modules import require_module
from src.orchestration import program_pipeline as pp
from src.orchestration.batch_resume import (
    PendingBatch,
    PendingProgramRun,
    save_pending_batch,
    save_pending_program_run,
)
from src.orchestration.pipeline import BatchSubmission
from src.programs import (
    HYPERSCALE_DATACENTER_PROGRAM,
    RoutingState,
    SpecAssignment,
    SpecRoutingDecision,
)
from src.review.reviewer import ReviewResult


def _load_recovery_cli():
    import importlib.util

    script = Path(__file__).resolve().parent.parent / "scripts" / "recover_batch.py"
    spec = importlib.util.spec_from_file_location("recover_batch_cli_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def cli():
    return _load_recovery_cli()


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(path))
    return path


def _assignment(name: str, module_ids: tuple[str, ...]) -> SpecAssignment:
    return SpecAssignment(
        source_path=str(Path("C:/specs") / name),
        decision=SpecRoutingDecision(
            spec_id=name,
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            automatic_state=RoutingState.SUPPORTED,
            automatic_module_ids=module_ids,
            confidence=0.95,
            evidence=(),
        ),
    )


def _child(module_id: str, name: str) -> BatchSubmission:
    module = require_module(module_id)
    request_id = f"review__{module_id}__0"
    return BatchSubmission(
        job=BatchJob(
            batch_id=f"msgbatch_{module_id}",
            job_type="review",
            request_map={request_id: {"filename": name, "index": 0, "type": "review"}},
            created_at=1_700_000_000.0,
        ),
        files_reviewed=[name],
        review_request_ids=[request_id],
        model="test-model",
        cycle_label=module.cycle.label,
        module_id=module_id,
    )


_PROGRAM_MODULES = ("datacenter_fire", "datacenter_architecture")


def _save_program_manifest(state_path: Path) -> None:
    name = "21 13 13 Wet Pipe.docx"
    submission = pp.ProgramSubmission(
        program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
        assignments=(_assignment(name, _PROGRAM_MODULES),),
        partitions={module_id: _child(module_id, name) for module_id in _PROGRAM_MODULES},
    )
    save_pending_program_run(PendingProgramRun.from_submission(submission), path=state_path)


def _stub_result(**overrides):
    base = dict(
        review_result=ReviewResult(findings=[], parse_status="ok"),
        failed_review_specs=[],
        module_errors={},
        review_transport="batch",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _stub_exports(cli, monkeypatch):
    monkeypatch.setattr(cli, "export_report", lambda result, path: path)
    monkeypatch.setattr(cli, "write_edit_instructions_sidecar", lambda result, path: path)
    monkeypatch.setattr(cli, "write_requirements_profile_sidecar", lambda result, path: None)


def _ended(*_a, **_k):
    return rt.PollOutcome(terminal=True, terminal_status="ended")


def _never(*_a, **_k):
    raise AssertionError("must not be called on this path")


class TestRecoveryCliProgramRuns:
    def test_saved_program_manifest_takes_the_program_path(self, cli, state_path, tmp_path, monkeypatch):
        _save_program_manifest(state_path)
        assert state_path.exists()
        monkeypatch.setattr(cli, "poll_batch_bounded", _ended)
        monkeypatch.setattr(cli, "run_batch_collection_headless", _never)
        monkeypatch.setattr(cli, "thin_submission_from_batch_results", _never)
        collected: dict = {}

        def fake_collect(submission, *, log, progress, diagnostics=None):
            collected["submission"] = submission
            return _stub_result()

        monkeypatch.setattr(cli, "collect_program_results", fake_collect)
        _stub_exports(cli, monkeypatch)

        rc = cli.main(["-o", str(tmp_path / "out.docx")])

        assert rc == 0
        submission = collected["submission"]
        assert isinstance(submission, pp.ProgramSubmission)
        assert submission.program_id == HYPERSCALE_DATACENTER_PROGRAM.program_id
        assert tuple(submission.partitions) == _PROGRAM_MODULES
        # Every child kept its own module identity — nothing defaulted.
        for module_id, child in submission.partitions.items():
            assert child.module_id == module_id
            assert child.cycle_label == require_module(module_id).cycle.label
        # Full success clears the manifest (the GUI's rule).
        assert not state_path.exists()

    def test_program_polls_every_child_batch(self, cli, state_path, tmp_path, monkeypatch):
        _save_program_manifest(state_path)
        polled: list[str] = []

        def fake_poll(batch_id, **_k):
            polled.append(batch_id)
            return rt.PollOutcome(terminal=True, terminal_status="ended")

        monkeypatch.setattr(cli, "poll_batch_bounded", fake_poll)
        monkeypatch.setattr(cli, "collect_program_results", lambda s, **_k: _stub_result())
        _stub_exports(cli, monkeypatch)

        cli.main(["-o", str(tmp_path / "out.docx")])

        assert sorted(polled) == sorted(f"msgbatch_{m}" for m in _PROGRAM_MODULES)

    def test_program_with_module_error_keeps_state_and_exits_2(self, cli, state_path, tmp_path, monkeypatch):
        _save_program_manifest(state_path)
        monkeypatch.setattr(cli, "poll_batch_bounded", _ended)
        monkeypatch.setattr(
            cli, "collect_program_results",
            lambda s, **_k: _stub_result(module_errors={"datacenter_architecture": "boom"}),
        )
        _stub_exports(cli, monkeypatch)

        rc = cli.main(["-o", str(tmp_path / "out.docx")])

        assert rc == 2
        assert state_path.exists()  # kept for a retry

    def test_program_child_batch_id_uses_the_saved_module(self, cli, state_path, tmp_path, monkeypatch):
        # Recovering ONE child of a saved program run by its bare id must use
        # the module the manifest recorded for it — and must not clear the
        # manifest (the sibling batch stays resumable).
        _save_program_manifest(state_path)
        monkeypatch.setattr(cli, "poll_batch_bounded", _ended)
        monkeypatch.setattr(cli, "collect_program_results", _never)
        monkeypatch.setattr(cli, "thin_submission_from_batch_results", _never)
        seen: dict = {}

        def fake_headless(submission, *, log, progress, diagnostics=None):
            seen["submission"] = submission
            return _stub_result()

        monkeypatch.setattr(cli, "run_batch_collection_headless", fake_headless)
        _stub_exports(cli, monkeypatch)

        rc = cli.main(["--batch-id", "msgbatch_datacenter_architecture", "-o", str(tmp_path / "o.docx")])

        assert rc == 0
        assert seen["submission"].module_id == "datacenter_architecture"
        assert seen["submission"].job.batch_id == "msgbatch_datacenter_architecture"
        assert state_path.exists()


class TestRecoveryCliBareBatchId:
    def test_bare_batch_id_requires_module(self, cli, state_path, capsys):
        assert not state_path.exists()
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--batch-id", "msgbatch_X"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--module is required" in err
        assert "does not carry its discipline" in err
        assert "california_k12_mep" in err
        assert "datacenter_fire" in err

    def test_bare_batch_id_with_module_reaches_thin_reconstruction(self, cli, state_path, monkeypatch):
        monkeypatch.setattr(cli, "ensure_batch_ended", lambda batch_id, **_k: None)

        class _Stop(Exception):
            pass

        captured: dict = {}

        def fake_thin(batch_id, **kwargs):
            captured["batch_id"] = batch_id
            captured.update(kwargs)
            raise _Stop()

        monkeypatch.setattr(cli, "thin_submission_from_batch_results", fake_thin)

        with pytest.raises(_Stop):
            cli.main(["--batch-id", "msgbatch_X", "--module", "datacenter_fire"])

        assert captured["batch_id"] == "msgbatch_X"
        assert captured["module"].module_id == "datacenter_fire"

    def test_unknown_module_is_rejected_by_argparse(self, cli, state_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--batch-id", "msgbatch_X", "--module", "not_a_module"])
        assert exc_info.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_saved_single_batch_ignores_module_flag(self, cli, state_path, tmp_path, monkeypatch):
        child = _child("datacenter_fire", "21 13 13 Wet Pipe.docx")
        save_pending_batch(PendingBatch.from_submission(child), path=state_path)
        monkeypatch.setattr(cli, "poll_batch_bounded", _ended)
        monkeypatch.setattr(cli, "thin_submission_from_batch_results", _never)
        seen: dict = {}

        def fake_headless(submission, *, log, progress, diagnostics=None):
            seen["submission"] = submission
            return _stub_result()

        monkeypatch.setattr(cli, "run_batch_collection_headless", fake_headless)
        _stub_exports(cli, monkeypatch)
        printed: list[str] = []
        monkeypatch.setattr(cli, "_log", lambda msg, *, level="info": printed.append(f"{level}:{msg}"))

        rc = cli.main(["--module", "california_k12_mep", "-o", str(tmp_path / "o.docx")])

        assert rc == 0
        assert seen["submission"].module_id == "datacenter_fire"  # saved state wins
        assert any("Ignoring --module california_k12_mep" in line for line in printed)
        assert not state_path.exists()

    def test_no_state_and_no_batch_id_errors_with_module_hint(self, cli, state_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cli.main([])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "No saved pending batch or program run" in err
        assert "--module" in err


# ---------------------------------------------------------------------------
# scripts/recover_batch.py — a recovered run prices its own collection
# ---------------------------------------------------------------------------
#
# The headless driver recorded no API-call diagnostics, so a recovery produced
# a report with no cost summary — which is why CLAUDE.md listed recovered runs
# as contributing no evidence to the prompt-cache / research-cache decisions
# that depend on measured repetition. The driver records now, so the tool
# builds a report, threads it in, and can export it.
#
# What a recovery still cannot account for is the review submission itself:
# the batch was submitted (and any research fan-out billed) by the session
# that created the pending state. The reported figure says so rather than
# presenting the collection half as the run total.


class TestRecoveryCliDiagnostics:
    def _run(self, cli, state_path, tmp_path, monkeypatch, extra_args=()):
        child = _child("datacenter_fire", "21 13 13 Wet Pipe.docx")
        save_pending_batch(PendingBatch.from_submission(child), path=state_path)
        monkeypatch.setattr(cli, "poll_batch_bounded", _ended)
        monkeypatch.setattr(cli, "thin_submission_from_batch_results", _never)
        seen: dict = {}

        def fake_headless(submission, *, log, progress, diagnostics=None):
            seen["diagnostics"] = diagnostics
            # Stand in for the real driver's recording so the cost summary has
            # something to price.
            if diagnostics is not None:
                diagnostics.record_api_call(
                    phase="batch_collect",
                    model="claude-opus-5",
                    message="Review results collected",
                    input_tokens=50_000,
                    output_tokens=20_000,
                    mode="batch",
                )
            return _stub_result()

        monkeypatch.setattr(cli, "run_batch_collection_headless", fake_headless)
        _stub_exports(cli, monkeypatch)
        printed: list[str] = []
        monkeypatch.setattr(
            cli, "_log", lambda msg, *, level="info": printed.append(f"{level}:{msg}")
        )
        rc = cli.main(["-o", str(tmp_path / "o.docx"), *extra_args])
        return rc, seen, printed

    def test_a_diagnostics_report_reaches_the_driver(
        self, cli, state_path, tmp_path, monkeypatch
    ):
        rc, seen, _printed = self._run(cli, state_path, tmp_path, monkeypatch)
        assert rc == 0
        assert seen["diagnostics"] is not None
        assert seen["diagnostics"].module_id == "datacenter_fire"

    def test_collection_cost_is_reported_and_scoped_honestly(
        self, cli, state_path, tmp_path, monkeypatch
    ):
        """The line must not present the collection half as the run total."""
        _rc, _seen, printed = self._run(cli, state_path, tmp_path, monkeypatch)
        cost_lines = [line for line in printed if "Collection cost" in line]
        assert len(cost_lines) == 1
        assert "excludes the original review submission" in cost_lines[0]

    def test_diagnostics_json_export_round_trips(
        self, cli, state_path, tmp_path, monkeypatch
    ):
        out = tmp_path / "diag" / "run.json"
        rc, _seen, printed = self._run(
            cli, state_path, tmp_path, monkeypatch,
            extra_args=("--diagnostics-json", str(out)),
        )
        assert rc == 0
        assert out.exists()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["cost_summary"]["estimated_cost_usd"]["total"] > 0
        assert any("Diagnostics saved" in line for line in printed)

    def test_no_json_written_without_the_flag(
        self, cli, state_path, tmp_path, monkeypatch
    ):
        self._run(cli, state_path, tmp_path, monkeypatch)
        assert not list(tmp_path.glob("*.json"))

    def test_a_telemetry_failure_never_sinks_the_recovery(
        self, cli, state_path, tmp_path, monkeypatch
    ):
        """The report is already on disk by this point; losing the cost line is
        not a reason to fail the run."""
        child = _child("datacenter_fire", "21 13 13 Wet Pipe.docx")
        save_pending_batch(PendingBatch.from_submission(child), path=state_path)
        monkeypatch.setattr(cli, "poll_batch_bounded", _ended)
        monkeypatch.setattr(cli, "thin_submission_from_batch_results", _never)
        monkeypatch.setattr(
            cli,
            "run_batch_collection_headless",
            lambda submission, *, log, progress, diagnostics=None: _stub_result(),
        )
        _stub_exports(cli, monkeypatch)
        printed: list[str] = []
        monkeypatch.setattr(
            cli, "_log", lambda msg, *, level="info": printed.append(f"{level}:{msg}")
        )

        class _Exploding:
            mode = ""
            module_id = ""

            def finish(self):
                raise RuntimeError("telemetry is broken")

        monkeypatch.setattr(cli, "DiagnosticsReport", _Exploding)

        rc = cli.main(["-o", str(tmp_path / "o.docx")])

        assert rc == 0
        assert any("Diagnostics not reported" in line for line in printed)
