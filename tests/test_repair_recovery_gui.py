"""The GUI's collection paths keep paid repair batches recoverable (plan WP-14).

The GUI half of ``test_repair_recovery.py``: the single-module collect
thread, the routed-program branch, the startup resume prompt, the
"Recover batch…" dialog, and the completion handler — each driven against a
fake app, with the same fake batch service and the same parity table as the
CLI tests, so the GUI and ``scripts/recover_batch.py`` are held to one rule.

Before this, the single-module collect deleted the saved record after any
collection that returned (a still-running repair included, and another
run's record when the collected batch had been recovered by id), and ran
every dependent paid stage on the primary results alone.

Imports ``src.gui.batch_controller`` (``tkinter`` at module scope), so it is
registered in ``conftest._GUI_DEPENDENT_TESTS``.
"""
from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from src.gui import batch_controller as gui_batch  # noqa: E402
from src.gui import review_run_controller as rrc  # noqa: E402
from src.orchestration import pipeline as pl  # noqa: E402
from src.orchestration import program_pipeline as pp  # noqa: E402
from src.orchestration.batch_resume import (  # noqa: E402
    PendingBatch,
    PendingProgramRun,
    load_pending_batch,
    load_pending_run,
    save_pending_batch,
    save_pending_program_run,
)
from src.programs import (  # noqa: E402
    HYPERSCALE_DATACENTER_PROGRAM,
    RoutingState,
    SpecAssignment,
    SpecRoutingDecision,
)
from tests.fixtures.batch_service import (  # noqa: E402
    PARITY_NAMES,
    PARITY_SCENARIOS,
    PRIMARY_ID,
    FakeBatchService,
    parity_primary,
    request_id,
    review_ok,
    review_truncated,
    submission,
    write_docx_specs,
)


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "state" / "pending_batch.json"
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(path))
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", "0")
    return path


class _SyncThread:
    """Runs the collect worker inline so a test can observe its effects."""

    def __init__(self, target=None, daemon=None, args=(), **_kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _fake_app(sub, *, input_dir="", files=()):
    events = {"complete": None, "error": None, "logs": [], "warnings": []}
    app = types.SimpleNamespace()
    app._batch_submission = sub
    app._diagnostics_report = None
    app._trace_recorder = None
    app.input_dir = input_dir
    app._selected_files_for_review = list(files)
    app._next_run_epoch = lambda: 1
    app._dispatch_if_current = lambda _epoch, fn: fn()
    app._make_diag_log = lambda *_a: (
        lambda msg, **_k: events["logs"].append(str(msg))
    )
    app._make_diag_progress = lambda *_a: (lambda *_a2, **_k: None)
    app._on_review_complete = lambda result: events.__setitem__("complete", result)
    app._on_review_error = lambda err: events.__setitem__("error", err)
    app.run_button = types.SimpleNamespace(configure=lambda **_k: None)
    app.log = types.SimpleNamespace(
        log_warning=lambda msg, **_k: events["warnings"].append(str(msg)),
        log_step=lambda *_a, **_k: None,
        log_success=lambda *_a, **_k: None,
        log=lambda *_a, **_k: None,
    )
    return app, events


def _collect(app, monkeypatch):
    monkeypatch.setattr(
        gui_batch,
        "threading",
        types.SimpleNamespace(Thread=_SyncThread, Lock=threading.Lock),
    )
    # The controller binds the verification entry point at import; route it
    # to the fake service's counter like the pipeline's own binding, so no
    # test here can reach the network.
    monkeypatch.setattr(gui_batch, "verify_findings_for_run", pl.verify_findings_for_run)
    gui_batch.collect_batch_results(app)


def _primary_b_truncated():
    return {
        PRIMARY_ID: {
            request_id(0): review_ok("A.docx"),
            request_id(1): review_truncated(),
        }
    }


# ===========================================================================
# 1. Single-module collection
# ===========================================================================


class TestSingleModuleCollection:
    def test_pending_repair_keeps_the_record_and_runs_no_paid_stage(
        self, monkeypatch, state_path
    ):
        sub = submission(PARITY_NAMES)
        save_pending_batch(PendingBatch.from_submission(sub))
        service = FakeBatchService(monkeypatch, primary=_primary_b_truncated())
        service.default_repair_status = "processing"
        app, events = _fake_app(sub)

        _collect(app, monkeypatch)

        assert events["error"] is None
        result = events["complete"]
        assert result is not None and result.provisional
        assert service.paid_downstream == 0
        assert service.repair_submits == [["B.docx"]]
        saved = load_pending_batch()
        assert saved is not None and saved.repair_batch_id == "msgbatch_REPAIR_1"
        assert any("Saved batch state kept" in line for line in events["logs"])
        assert any("⏳ Review repair still outstanding for B.docx" in w for w in events["warnings"])

    def test_resume_collects_the_same_repair_and_then_clears(
        self, monkeypatch, state_path, tmp_path
    ):
        # Real files, so the resumed run re-extracts its specs from disk.
        files = write_docx_specs(tmp_path, PARITY_NAMES)
        sub = submission(PARITY_NAMES)
        save_pending_batch(
            PendingBatch.from_submission(sub, input_dir=tmp_path, files=files)
        )
        service = FakeBatchService(monkeypatch, primary=_primary_b_truncated())
        service.default_repair_status = "processing"
        app, _events = _fake_app(sub)
        _collect(app, monkeypatch)

        # Restart: rebuild the submission from the saved record, as the
        # startup resume prompt does; the repair has now ended.
        service.status["msgbatch_REPAIR_1"] = "ended"
        resumed = load_pending_batch().to_submission()
        app, events = _fake_app(resumed)
        _collect(app, monkeypatch)

        assert service.repair_submits == [["B.docx"]]  # never resubmitted
        assert service.verification_rounds == [2]
        assert service.cross_checks == 1
        assert not events["complete"].provisional
        assert not state_path.exists()

    def test_a_recovered_batch_never_deletes_another_runs_record(
        self, monkeypatch, state_path
    ):
        # "Recover batch…" of batch X while a detached run Y's record is on
        # disk. X's collection is complete; Y's record must survive.
        other = submission(["Z.docx"], batch_id="msgbatch_OTHER_DETACHED_RUN")
        save_pending_batch(PendingBatch.from_submission(other))
        FakeBatchService(
            monkeypatch,
            primary={
                "msgbatch_RECOVERED": {
                    request_id(0): review_ok("A.docx"),
                    request_id(1): review_ok("B.docx"),
                }
            },
        )
        app, events = _fake_app(submission(PARITY_NAMES, batch_id="msgbatch_RECOVERED"))

        _collect(app, monkeypatch)

        assert events["complete"] is not None
        assert load_pending_batch().batch_id == "msgbatch_OTHER_DETACHED_RUN"

    def test_a_provisional_recovered_batch_is_saved_when_the_slot_is_free(
        self, monkeypatch, state_path
    ):
        service = FakeBatchService(monkeypatch, primary=_primary_b_truncated())
        service.default_repair_status = "processing"
        app, _events = _fake_app(
            submission(PARITY_NAMES), input_dir="C:/specs",
            files=[Path("C:/specs/A.docx"), Path("C:/specs/B.docx")],
        )

        _collect(app, monkeypatch)

        saved = load_pending_batch()
        assert saved is not None and saved.batch_id == PRIMARY_ID
        assert saved.repair_batch_id == "msgbatch_REPAIR_1"
        assert saved.files == [str(Path("C:/specs/A.docx")), str(Path("C:/specs/B.docx"))]

    def test_a_real_time_run_leaves_an_earlier_batch_record_alone(
        self, monkeypatch, state_path
    ):
        save_pending_batch(
            PendingBatch.from_submission(submission(["Z.docx"], batch_id="msgbatch_EARLIER"))
        )
        FakeBatchService(monkeypatch, primary={})
        sub = submission(PARITY_NAMES, transport="realtime")
        sub.realtime_results = {
            request_id(0): review_ok("A.docx"),
            request_id(1): review_ok("B.docx"),
        }
        app, events = _fake_app(sub)
        _collect(app, monkeypatch)
        assert events["complete"] is not None
        assert load_pending_batch().batch_id == "msgbatch_EARLIER"

    @pytest.mark.parametrize("name", list(PARITY_SCENARIOS))
    def test_parity_with_the_recovery_cli(self, name, monkeypatch, state_path):
        kind, repair_status, kept = PARITY_SCENARIOS[name]
        sub = submission(PARITY_NAMES)
        save_pending_batch(PendingBatch.from_submission(sub))
        service = FakeBatchService(monkeypatch, primary=parity_primary(kind))
        service.default_repair_status = repair_status
        app, events = _fake_app(sub)
        _collect(app, monkeypatch)
        assert events["error"] is None
        assert state_path.exists() is kept


# ===========================================================================
# 2. Routed-program collection
# ===========================================================================


def _program_submission():
    def assignment(name, module_id):
        return SpecAssignment(
            source_path=str(Path("C:/specs") / name),
            decision=SpecRoutingDecision(
                spec_id=name,
                program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
                automatic_state=RoutingState.SUPPORTED,
                automatic_module_ids=(module_id,),
                confidence=0.95,
                evidence=(),
            ),
        )

    return pp.ProgramSubmission(
        program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
        assignments=(
            assignment("21 13 13 Wet.docx", "datacenter_fire"),
            assignment("07 27 26 Air.docx", "datacenter_architecture"),
        ),
        partitions={
            "datacenter_fire": submission(
                ["21 13 13 Wet.docx"], batch_id="msgbatch_datacenter_fire",
                module_id="datacenter_fire",
            ),
            "datacenter_architecture": submission(
                ["07 27 26 Air.docx"], batch_id="msgbatch_datacenter_architecture",
                module_id="datacenter_architecture",
            ),
        },
    )


class TestProgramCollection:
    def _primary(self, *, fire):
        return {
            "msgbatch_datacenter_fire": {request_id(0): fire},
            "msgbatch_datacenter_architecture": {
                request_id(0): review_ok("07 27 26 Air.docx")
            },
        }

    def test_a_childs_pending_repair_keeps_the_manifest(self, monkeypatch, state_path):
        program = _program_submission()
        save_pending_program_run(PendingProgramRun.from_submission(program))
        service = FakeBatchService(monkeypatch, primary=self._primary(fire=review_truncated()))
        service.default_repair_status = "processing"
        app, events = _fake_app(program)

        _collect(app, monkeypatch)

        result = events["complete"]
        assert result is not None and result.provisional
        assert service.paid_downstream == 0
        saved = load_pending_run()
        assert isinstance(saved, PendingProgramRun)
        assert saved.partitions["datacenter_fire"]["repair_batch_id"] == "msgbatch_REPAIR_1"

    def test_a_settled_program_clears_its_manifest(self, monkeypatch, state_path):
        program = _program_submission()
        save_pending_program_run(PendingProgramRun.from_submission(program))
        FakeBatchService(monkeypatch, primary=self._primary(fire=review_ok("21 13 13 Wet.docx")))
        app, events = _fake_app(program)
        _collect(app, monkeypatch)
        assert events["complete"] is not None and not events["complete"].provisional
        assert not state_path.exists()


# ===========================================================================
# 3. The resume prompt and the Recover dialog
# ===========================================================================


class _PromptApp:
    def __init__(self):
        self.is_processing = False
        self.logged: list[str] = []
        self.log = types.SimpleNamespace(log=lambda msg, **_k: self.logged.append(str(msg)))


class TestResumePrompt:
    def test_discard_deletes_the_record_it_showed(self, monkeypatch, state_path):
        save_pending_batch(PendingBatch.from_submission(submission(PARITY_NAMES)))
        monkeypatch.setattr(gui_batch.messagebox, "askyesno", lambda *a, **k: False)
        gui_batch.offer_batch_resume(_PromptApp())
        assert not state_path.exists()

    def test_discard_never_deletes_a_record_saved_after_the_prompt_loaded(
        self, monkeypatch, state_path
    ):
        save_pending_batch(PendingBatch.from_submission(submission(PARITY_NAMES)))

        def answer_no_after_another_run_saved(*_a, **_k):
            save_pending_batch(
                PendingBatch.from_submission(submission(["Z.docx"], batch_id="msgbatch_NEW"))
            )
            return False

        monkeypatch.setattr(gui_batch.messagebox, "askyesno", answer_no_after_another_run_saved)
        gui_batch.offer_batch_resume(_PromptApp())
        assert load_pending_batch().batch_id == "msgbatch_NEW"

    def test_the_prompt_names_a_recorded_repair_batch(self, monkeypatch, state_path):
        sub = submission(PARITY_NAMES)
        sub.repair_batch_id = "msgbatch_REPAIR_SAVED"
        save_pending_batch(PendingBatch.from_submission(sub))
        seen: dict = {}

        def ask(title, message, **_k):
            seen["message"] = message
            return False

        monkeypatch.setattr(gui_batch.messagebox, "askyesno", ask)
        gui_batch.offer_batch_resume(_PromptApp())
        assert "msgbatch_REPAIR_SAVED" in seen["message"]
        assert "without submitting a new one" in seen["message"]


class TestRecoverDialog:
    def _run(self, monkeypatch, batch_id):
        resumed: list = []
        monkeypatch.setattr(
            gui_batch, "start_batch_resume", lambda app, pending: resumed.append(pending)
        )
        monkeypatch.setattr(
            gui_batch, "_begin_reconnect_run",
            lambda *a, **k: pytest.fail("a saved batch must resume from its record"),
        )
        import tkinter.simpledialog as simpledialog

        monkeypatch.setattr(simpledialog, "askstring", lambda *a, **k: batch_id)
        app = _PromptApp()
        gui_batch.recover_batch_dialog(app)
        return resumed, app

    def test_a_saved_batch_id_resumes_from_its_record(self, monkeypatch, state_path):
        sub = submission(PARITY_NAMES)
        sub.repair_batch_id = "msgbatch_REPAIR_SAVED"
        save_pending_batch(PendingBatch.from_submission(sub))
        resumed, _app = self._run(monkeypatch, PRIMARY_ID)
        assert len(resumed) == 1
        assert resumed[0].repair_batch_id == "msgbatch_REPAIR_SAVED"

    def test_a_saved_program_childs_id_resumes_that_child(self, monkeypatch, state_path):
        save_pending_program_run(PendingProgramRun.from_submission(_program_submission()))
        resumed, _app = self._run(monkeypatch, "msgbatch_datacenter_architecture")
        assert len(resumed) == 1
        assert isinstance(resumed[0], PendingBatch)
        assert resumed[0].module_id == "datacenter_architecture"


# ===========================================================================
# 4. The completion handler
# ===========================================================================


class _TerminalApp:
    def __init__(self):
        self.warnings: list[str] = []
        self.finalized: list[tuple] = []
        self.terminal: list[str] = []
        self.progress_bar = types.SimpleNamespace(set=lambda _v: None)
        self.log = types.SimpleNamespace(
            log_warning=lambda msg, **_k: self.warnings.append(str(msg)),
            log_success=lambda *_a, **_k: None,
            log=lambda *_a, **_k: None,
        )
        self.run_button = types.SimpleNamespace(
            set_complete=lambda: self.terminal.append("green"),
            set_complete_with_errors=lambda: self.terminal.append("amber"),
        )
        self._last_result = None

    def after(self, _ms, _fn):
        return None

    def _reset_ui(self):
        return None

    def _finalize_diagnostics(self, phase, level, message):
        self.finalized.append((phase, level, message))

    def _export_report_to_file(self, _result):
        return "success"


class TestCompletionHandler:
    def test_a_provisional_result_ends_amber_and_says_why(self, monkeypatch, state_path):
        service = FakeBatchService(monkeypatch, primary=_primary_b_truncated())
        service.default_repair_status = "processing"
        result = pl.run_batch_collection_headless(submission(PARITY_NAMES))
        app = _TerminalApp()

        rrc.on_review_complete(app, result)

        assert app.terminal == ["amber"]
        assert any(w.startswith("Provisional result: review repair batch msgbatch_REPAIR_1") for w in app.warnings)
        assert app.finalized[-1][1] == "warning"
        assert "provisionally" in app.finalized[-1][2]
