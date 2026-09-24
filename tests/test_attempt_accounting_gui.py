"""The GUI prices every paid attempt and says what the figure is (plan WP-15).

The GUI half of ``test_attempt_accounting.py``: the single-module collect
thread records the review phase from the combined carrier's attempt records
(so the truncated primary a repair replaced stays priced), and the
Diagnostics window shows the same estimate lines as the text export and the
recovery CLI — labelled an estimate, never an invoice, with attempts of
unknown usage named rather than priced at zero.

Both run without a display: the collect thread against a fake app (the
``test_repair_recovery_gui.py`` pattern), the window's renderer against fake
widget classes on an instance built with ``object.__new__``.
"""
from __future__ import annotations

import threading
import types

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from src.core.attempt_usage import (  # noqa: E402
    OPERATION_REVIEW,
    TRANSPORT_BATCH,
    unknown_attempt,
)
from src.core.pricing import estimate_cost_breakdown  # noqa: E402
from src.gui import batch_controller as gui_batch  # noqa: E402
from src.gui import widgets  # noqa: E402
from src.gui.widgets import DiagnosticsWindow  # noqa: E402
from src.orchestration import pipeline as pl  # noqa: E402
from src.orchestration.diagnostics import (  # noqa: E402
    ESTIMATE_NOTE,
    DiagnosticsReport,
    cost_summary_lines,
)
from tests.fixtures.batch_service import (  # noqa: E402
    PRIMARY_ID,
    FakeBatchService,
    request_id,
    review_ok,
    review_truncated,
    submission,
)


class _SyncThread:
    def __init__(self, target=None, daemon=None, args=(), **_kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _fake_app(sub, diag):
    app = types.SimpleNamespace()
    app._batch_submission = sub
    app._diagnostics_report = diag
    app._trace_recorder = None
    app.input_dir = ""
    app._selected_files_for_review = []
    app._next_run_epoch = lambda: 1
    app._dispatch_if_current = lambda _epoch, fn: fn()
    app._make_diag_log = lambda *_a: (lambda *_m, **_k: None)
    app._make_diag_progress = lambda *_a: (lambda *_a2, **_k: None)
    app._on_review_complete = lambda result: None
    app._on_review_error = lambda err: (_ for _ in ()).throw(err)
    app.run_button = types.SimpleNamespace(configure=lambda **_k: None)
    app.log = types.SimpleNamespace(
        log_warning=lambda *_a, **_k: None,
        log_step=lambda *_a, **_k: None,
        log_success=lambda *_a, **_k: None,
        log=lambda *_a, **_k: None,
    )
    return app


def test_the_gui_collect_prices_the_primary_a_repair_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", "0")
    FakeBatchService(
        monkeypatch,
        primary={PRIMARY_ID: {
            request_id(0): review_ok("A.docx"),
            request_id(1): review_truncated(),
        }},
    )
    monkeypatch.setattr(
        gui_batch, "threading", types.SimpleNamespace(Thread=_SyncThread, Lock=threading.Lock)
    )
    monkeypatch.setattr(gui_batch, "verify_findings_for_run", pl.verify_findings_for_run)
    diag = DiagnosticsReport()

    gui_batch.collect_batch_results(_fake_app(submission(["A.docx", "B.docx"]), diag))

    summary = diag.summary()["cost_summary"]
    assert summary["by_category"]["review"]["attempts"] == 2
    assert summary["by_category"]["review_repair"]["attempts"] == 1
    expected = estimate_cost_breakdown(
        3_000, 128_800, model="claude-opus-5", batch=True
    ).total
    assert summary["estimated_cost_usd"]["total"] == pytest.approx(expected, abs=1e-6)


class _FakeWidget:
    labels: list[str] = []

    def __init__(self, *_args, **kwargs):
        if "text" in kwargs:
            _FakeWidget.labels.append(kwargs["text"])

    def _noop(self, *_a, **_k):
        return None

    pack = grid = place = configure = grid_propagate = grid_columnconfigure = _noop


def test_the_diagnostics_window_shows_the_shared_estimate_lines(monkeypatch):
    _FakeWidget.labels = []
    monkeypatch.setattr(
        widgets,
        "ctk",
        types.SimpleNamespace(
            CTkFrame=_FakeWidget, CTkLabel=_FakeWidget, CTkFont=lambda **_k: None
        ),
    )
    diag = DiagnosticsReport()
    diag.record_api_call(
        phase="batch_collect", model="claude-opus-5", mode="batch",
        input_tokens=1_000, output_tokens=400, operation=OPERATION_REVIEW,
    )
    diag.record_api_call(
        phase="batch_collect", model="claude-opus-5", mode="batch",
        operation=OPERATION_REVIEW,
        attempts=[unknown_attempt(operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH)],
    )
    summary = diag.summary()

    window = object.__new__(DiagnosticsWindow)
    window._render_actionable_section(None, summary)

    lines = cost_summary_lines(summary)
    assert "Estimated Cost:" in _FakeWidget.labels
    for line in lines:
        assert f"  {line.strip()}" in _FakeWidget.labels
    assert any(ESTIMATE_NOTE in label for label in _FakeWidget.labels)
    assert any("unknown usage are not in the estimate" in label for label in _FakeWidget.labels)
