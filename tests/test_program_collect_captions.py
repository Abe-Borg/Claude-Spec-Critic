"""Program-run run-button captions during collect (C3).

The program collect branch hands the Tk-free pipeline one progress callback;
stage transitions happen inside ``collect_program_results`` /
``run_batch_collection_headless``. ``_make_program_collect_progress`` maps
the ``stage=`` kwarg riding each emission to a run-button caption so the
button no longer sits on "Researching location requirements..." for the
whole collect phase.

Imports ``batch_controller`` (→ tkinter at module scope), so this file is
registered in ``conftest._GUI_DEPENDENT_TESTS``.
"""
from __future__ import annotations

from src.gui.batch_controller import _STAGE_CAPTIONS, _make_program_collect_progress


class _FakeApp:
    def __init__(self):
        self.captions: list[str] = []
        self.diag_calls: list[tuple[float, str]] = []
        outer = self

        class _Button:
            def configure(self, **kwargs):
                if "text" in kwargs:
                    outer.captions.append(kwargs["text"])

        self.run_button = _Button()

    def _make_diag_progress(self, _phase: str, _run_epoch: int):
        def _progress(pct, msg, **_kwargs):
            self.diag_calls.append((float(pct), str(msg)))

        return _progress

    def _dispatch_if_current(self, _epoch: int, fn):
        fn()


class TestProgramCollectCaptions:
    def test_stage_transitions_update_caption_once(self):
        app = _FakeApp()
        progress = _make_program_collect_progress(app, run_epoch=1)

        progress(55.0, "Collecting review results...", stage="review_collect")
        progress(59.5, "Review results collected.", stage="review_collect")
        progress(60.0, "Verifying 3 finding(s)...", stage="verify_round1")
        progress(70.0, "Verified 1/3 findings", stage="verify_round1")
        progress(77.5, "Running cross-spec coordination check...", stage="cross_check")
        progress(83.8, "Cross-spec coordination check complete.", stage="cross_check")
        progress(83.8, "Running local-code compliance check...", stage="compliance")
        progress(90.1, "Verifying 2 finding(s)...", stage="verify_round2")

        assert app.captions == [
            "Collecting results...",
            "Verifying findings...",
            "Cross-check (live API)...",
            "Compliance check (live API)...",
            "Verifying cross-check...",
        ]
        # Every emission still reaches the diag/bar callback.
        assert len(app.diag_calls) == 8
        assert app.diag_calls[0] == (55.0, "Collecting review results...")

    def test_unknown_or_missing_stage_leaves_caption_alone(self):
        app = _FakeApp()
        progress = _make_program_collect_progress(app, run_epoch=1)

        progress(55.0, "message with no stage")
        progress(56.0, "future stage", stage="not_a_known_stage")

        assert app.captions == []
        assert len(app.diag_calls) == 2

    def test_caption_map_covers_every_visible_stage(self):
        for stage in (
            "review_collect",
            "verify_round1",
            "cross_check",
            "compliance",
            "verify_round2",
            "drawing_impact",
        ):
            assert _STAGE_CAPTIONS[stage]
