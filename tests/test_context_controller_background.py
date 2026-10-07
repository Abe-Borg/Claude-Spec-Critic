"""Context-controller background flows (B-22.1 / B-22.2).

Attachment extraction and the drawing analysis's read + token count run on
worker threads; every tkinter mutation (cursor restore, dialogs, the merge,
the FILES-panel readout) is marshaled back with ``app.after(0, ...)``. The
fake app records the calling thread of every ``after`` and stores the
callbacks so the test drains them as the "Tk thread".

Imports ``src.gui.context_controller`` (tkinter + customtkinter at module
scope), so this file skips without them, like the other GUI tests.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from src.gui import context_attachment as ca  # noqa: E402
from src.gui import context_controller as cc  # noqa: E402
from src.input.drawing_analysis import (  # noqa: E402
    DrawingAnalysis,
    wrapped_drawing_analysis_block,
)


class _Log:
    def __init__(self):
        self.entries: list[tuple[str, str]] = []

    def log(self, msg, level="info", **_):
        self.entries.append((level, msg))

    def log_warning(self, msg):
        self.entries.append(("warning", msg))


class _Widget:
    def __init__(self):
        self.states: list[str] = []

    def configure(self, **kw):
        if "state" in kw:
            self.states.append(kw["state"])


class _Panel:
    """Records every FILES-panel readout the controller pushes."""

    def __init__(self):
        self.rows: list[list[dict]] = []

    def set_drawings(self, rows):
        self.rows.append(list(rows))


class _FakeApp:
    def __init__(self):
        self.log = _Log()
        self.cursors: list[str] = []
        self.after_threads: list[int] = []
        self._queued: list = []
        self._cv = threading.Condition()
        # Deliberately no ``api_key_entry``: neither attach flow touches a
        # credential, and an AttributeError here would prove one did.
        self.is_processing = False
        self._selected_program_id = "california_k12_mep"
        self.file_list_panel = None

    def configure(self, cursor=None, **_):
        if cursor is not None:
            self.cursors.append(cursor)

    def after(self, delay_ms, fn):
        with self._cv:
            self.after_threads.append(threading.get_ident())
            self._queued.append(fn)
            self._cv.notify_all()
        return f"after-{len(self._queued)}"

    def pump(self, expected=1, timeout=5.0):
        """Run the marshaled callbacks on this thread (the "Tk thread").

        Waits until at least ``expected`` callbacks are queued, then runs a
        SNAPSHOT of the queue — never a live drain, which would also swallow
        callbacks a worker started by one of these callbacks enqueues
        mid-loop and leave the next ``pump`` waiting on an empty queue.
        """
        with self._cv:
            assert self._cv.wait_for(
                lambda: len(self._queued) >= expected, timeout=timeout
            ), f"expected {expected} marshaled callback(s), got {len(self._queued)}"
            batch = list(self._queued)
            self._queued.clear()
        for fn in batch:
            fn()


class _DialogSpy:
    """Records every messagebox call as (name, title, message, kwargs)."""

    def __init__(self, monkeypatch, *, askyesno=False):
        self.calls: list[SimpleNamespace] = []

        def _make(name, ret):
            def _fn(title, message, **kw):
                self.calls.append(
                    SimpleNamespace(name=name, title=title, message=message, kw=kw)
                )
                return ret
            return _fn

        for name, ret in (
            ("showwarning", None), ("showerror", None), ("showinfo", None),
            ("askyesno", askyesno),
        ):
            monkeypatch.setattr(cc.messagebox, name, _make(name, ret))

    def names(self):
        return [c.name for c in self.calls]


@pytest.fixture(autouse=True)
def _stub_tokens(monkeypatch):
    monkeypatch.setattr(ca, "count_tokens", lambda text: len(text.split()))
    monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))


# ---------------------------------------------------------------------------
# B-22.2 — attach_context_files
# ---------------------------------------------------------------------------


class TestAttachContextFiles:
    def _pick(self, monkeypatch, names):
        seen: list[dict] = []

        def _ask(**kw):
            seen.append(kw)
            return tuple(names)

        monkeypatch.setattr(cc.filedialog, "askopenfilenames", _ask)
        return seen

    def test_extraction_runs_off_the_tk_thread_and_merges_on_it(self, monkeypatch):
        app = _FakeApp()
        picker = self._pick(monkeypatch, ["/ctx/notes.docx"])
        dialogs = _DialogSpy(monkeypatch)
        main_ident = threading.get_ident()
        extract_threads: list[int] = []

        def _extract(paths):
            extract_threads.append(threading.get_ident())
            return ("EXTRACTED", [])

        monkeypatch.setattr(cc, "extract_context_attachments", _extract)
        monkeypatch.setattr(cc, "get_project_context", lambda app: "existing")
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))

        cc.attach_context_files(app)
        assert picker[0]["parent"] is app
        assert app.cursors == ["watch"]  # cursor set synchronously
        assert app._context_extraction_running is True
        app.pump()  # the worker's marshaled completion

        assert extract_threads and extract_threads[0] != main_ident
        assert app.after_threads[0] != main_ident  # marshaled from the worker
        assert app.cursors == ["watch", ""]  # restored on the Tk thread
        assert app._context_extraction_running is False
        assert merged == ["existing\n\nEXTRACTED"]
        assert dialogs.calls == []

    def test_errors_and_over_cap_surface_on_the_tk_thread_with_an_owner(self, monkeypatch):
        app = _FakeApp()
        self._pick(monkeypatch, ["/ctx/a.pdf"])
        dialogs = _DialogSpy(monkeypatch)
        monkeypatch.setattr(cc, "extract_context_attachments", lambda paths: ("x " * 5, ["a.pdf: unreadable"]))
        monkeypatch.setattr(cc, "get_project_context", lambda app: "")
        monkeypatch.setattr(cc, "context_within_token_cap", lambda text: (999_999, False))
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))

        cc.attach_context_files(app)
        assert dialogs.calls == []  # nothing shown until the worker reports back
        app.pump()
        assert dialogs.names() == ["showwarning", "showerror"]
        assert all(c.kw["parent"] is app for c in dialogs.calls)
        assert merged == []  # refused, never truncated
        assert app._context_extraction_running is False

    def test_worker_exception_is_reported_and_cursor_restored(self, monkeypatch):
        app = _FakeApp()
        self._pick(monkeypatch, ["/ctx/a.pdf"])
        dialogs = _DialogSpy(monkeypatch)

        def _boom(paths):
            raise RuntimeError("pypdf exploded")

        monkeypatch.setattr(cc, "extract_context_attachments", _boom)
        cc.attach_context_files(app)
        app.pump()
        assert dialogs.names() == ["showerror"]
        assert "pypdf exploded" in dialogs.calls[0].message
        assert app.cursors == ["watch", ""]
        assert app._context_extraction_running is False

    def test_concurrent_attach_is_refused_without_opening_the_picker(self, monkeypatch):
        app = _FakeApp()
        app._context_extraction_running = True
        picker = self._pick(monkeypatch, ["/ctx/a.pdf"])
        dialogs = _DialogSpy(monkeypatch)
        cc.attach_context_files(app)
        assert picker == []
        assert dialogs.names() == ["showinfo"]

    def test_modal_target_textbox_owns_the_dialogs(self, monkeypatch):
        app = _FakeApp()
        toplevel = object()

        class _Textbox:
            def __init__(self):
                self.text = "modal text"

            def winfo_toplevel(self):
                return toplevel

            def get(self, *_):
                return self.text

            def delete(self, *_):
                self.text = ""

            def insert(self, _index, value):
                self.text = value

        box = _Textbox()
        picker = self._pick(monkeypatch, ["/ctx/a.docx", "/ctx/bad.exe"])
        dialogs = _DialogSpy(monkeypatch)
        monkeypatch.setattr(cc, "extract_context_attachments", lambda paths: ("NEW", []))
        cc.attach_context_files(app, target_textbox=box)
        assert picker[0]["parent"] is toplevel
        assert dialogs.names() == ["showwarning"]  # the .exe
        assert dialogs.calls[0].kw["parent"] is toplevel
        app.pump()
        assert box.text == "modal text\n\nNEW"


# ---------------------------------------------------------------------------
# B-22.1 — attach_drawing_analysis: read + count on a worker, merge on Tk
# ---------------------------------------------------------------------------


_ANALYSIS_TEXT = "SHEET INDEX M-101 Mechanical Plan FP-601 Riser Diagram"


def _analysis(name="plans_analysis.txt", text=_ANALYSIS_TEXT):
    return DrawingAnalysis(name=name, text=text, tokens=len(text.split()))


class TestAttachDrawingAnalysis:
    def _pick(self, monkeypatch, names):
        seen: list[dict] = []

        def _ask(**kw):
            seen.append(kw)
            return tuple(names)

        monkeypatch.setattr(cc.filedialog, "askopenfilenames", _ask)
        return seen

    def test_read_and_count_run_off_the_tk_thread_and_merge_on_it(self, monkeypatch):
        app = _FakeApp()
        app.file_list_panel = _Panel()
        picker = self._pick(monkeypatch, ["/dwg/plans_analysis.txt"])
        dialogs = _DialogSpy(monkeypatch)
        main_ident = threading.get_ident()
        load_threads: list[int] = []
        analysis = _analysis()

        def _load(paths):
            load_threads.append(threading.get_ident())
            assert [str(p) for p in paths] == ["/dwg/plans_analysis.txt"]
            return ([analysis], [])

        monkeypatch.setattr(cc, "load_drawing_analyses", _load)
        monkeypatch.setattr(cc, "get_project_context", lambda app: "existing")
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))

        cc.attach_drawing_analysis(app)
        assert picker[0]["parent"] is app
        assert "text" in picker[0]["title"].lower()
        assert app.cursors == ["watch"]
        assert app._context_extraction_running is True
        app.pump()  # the worker's marshaled completion

        assert load_threads and load_threads[0] != main_ident
        assert app.after_threads[0] != main_ident
        assert app.cursors == ["watch", ""]
        assert app._context_extraction_running is False
        assert merged == ["existing\n\n" + wrapped_drawing_analysis_block(analysis)]
        # The token counter: the FILES panel row and the activity log both
        # carry the file's count, immediately (not after the typing debounce).
        assert app.file_list_panel.rows == [[{"name": "plans_analysis.txt", "tokens": analysis.tokens}]]
        assert app._drawing_analyses == [{"name": "plans_analysis.txt", "tokens": analysis.tokens}]
        success = [m for lvl, m in app.log.entries if lvl == "success"]
        assert len(success) == 1
        assert "plans_analysis.txt" in success[0]
        assert f"{analysis.tokens:,} tokens" in success[0]
        totals = [m for lvl, m in app.log.entries if lvl == "info"]
        assert totals and "Project Context is now" in totals[0]
        assert dialogs.calls == []

    def test_the_flow_makes_no_api_call_and_reads_no_key(self, monkeypatch):
        # Spec Critic does not read drawings: no credential, no client, no
        # cost dialog. The fake app has no key field at all.
        app = _FakeApp()
        assert not hasattr(app, "api_key_entry")
        self._pick(monkeypatch, ["/dwg/a.txt"])
        dialogs = _DialogSpy(monkeypatch)
        monkeypatch.setattr(cc, "load_drawing_analyses", lambda paths: ([_analysis("a.txt")], []))
        monkeypatch.setattr(cc, "get_project_context", lambda app: "")
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))
        cc.attach_drawing_analysis(app)
        app.pump()
        assert merged and merged[0].startswith("--- BEGIN ATTACHMENT: Construction Drawing Digest ---")
        assert dialogs.names() == []  # no "Analyze drawings?" cost confirmation
        for name in ("credential_from_text", "run_with_credential", "preflight_digest_cost"):
            assert not hasattr(cc, name), name

    def test_unusable_files_warn_and_nothing_is_merged(self, monkeypatch):
        app = _FakeApp()
        self._pick(monkeypatch, ["/dwg/plans.pdf"])
        dialogs = _DialogSpy(monkeypatch)
        monkeypatch.setattr(
            cc, "load_drawing_analyses",
            lambda paths: ([], ["plans.pdf: not a text analysis file (expected .json, .md, .txt)"]),
        )
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))
        cc.attach_drawing_analysis(app)
        assert dialogs.calls == []  # nothing shown until the worker reports back
        app.pump()
        assert dialogs.names() == ["showwarning"]
        assert dialogs.calls[0].kw["parent"] is app
        assert "plans.pdf" in dialogs.calls[0].message
        assert merged == []
        assert app._context_extraction_running is False
        assert app.cursors == ["watch", ""]

    def test_over_cap_is_refused_with_the_counts_never_truncated(self, monkeypatch):
        app = _FakeApp()
        self._pick(monkeypatch, ["/dwg/plans_analysis.txt"])
        dialogs = _DialogSpy(monkeypatch)
        analysis = _analysis()
        monkeypatch.setattr(cc, "load_drawing_analyses", lambda paths: ([analysis], []))
        monkeypatch.setattr(cc, "get_project_context", lambda app: "")
        monkeypatch.setattr(cc, "context_within_token_cap", lambda text: (150_000, False))
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))
        cc.attach_drawing_analysis(app)
        app.pump()
        assert merged == []
        assert dialogs.names() == ["showerror"]
        assert dialogs.calls[0].title == "Drawing analysis too large for Project Context"
        assert dialogs.calls[0].kw["parent"] is app
        message = dialogs.calls[0].message
        assert f"plans_analysis.txt is {analysis.tokens:,} tokens" in message
        assert "150,000" in message
        assert app._context_extraction_running is False

    def test_worker_exception_is_reported_and_cursor_restored(self, monkeypatch):
        app = _FakeApp()
        self._pick(monkeypatch, ["/dwg/a.txt"])
        dialogs = _DialogSpy(monkeypatch)

        def _boom(paths):
            raise RuntimeError("disk vanished")

        monkeypatch.setattr(cc, "load_drawing_analyses", _boom)
        cc.attach_drawing_analysis(app)
        app.pump()
        assert dialogs.names() == ["showerror"]
        assert "disk vanished" in dialogs.calls[0].message
        assert app.cursors == ["watch", ""]
        assert app._context_extraction_running is False

    def test_concurrent_attach_is_refused_without_opening_the_picker(self, monkeypatch):
        # Both attach flows write the same textbox, so they share one flag.
        app = _FakeApp()
        app._context_extraction_running = True
        picker = self._pick(monkeypatch, ["/dwg/a.txt"])
        dialogs = _DialogSpy(monkeypatch)
        cc.attach_drawing_analysis(app)
        assert picker == []
        assert dialogs.names() == ["showinfo"]

    def test_cancelled_picker_changes_nothing(self, monkeypatch):
        app = _FakeApp()
        self._pick(monkeypatch, [])
        dialogs = _DialogSpy(monkeypatch)
        cc.attach_drawing_analysis(app)
        assert app.cursors == []
        assert not getattr(app, "_context_extraction_running", False)
        assert dialogs.calls == []

    def test_modal_target_textbox_receives_the_block(self, monkeypatch):
        app = _FakeApp()
        app.file_list_panel = _Panel()
        toplevel = object()

        class _Textbox:
            def __init__(self):
                self.text = "modal text"

            def winfo_toplevel(self):
                return toplevel

            def get(self, *_):
                return self.text

            def delete(self, *_):
                self.text = ""

            def insert(self, _index, value):
                self.text = value

        box = _Textbox()
        picker = self._pick(monkeypatch, ["/dwg/a.txt"])
        dialogs = _DialogSpy(monkeypatch)
        analysis = _analysis("a.txt")
        monkeypatch.setattr(cc, "load_drawing_analyses", lambda paths: ([analysis], []))
        cc.attach_drawing_analysis(app, target_textbox=box)
        assert picker[0]["parent"] is toplevel
        app.pump()
        assert box.text == "modal text\n\n" + wrapped_drawing_analysis_block(analysis)
        assert dialogs.calls == []
        # The readout follows the main textbox, which Save & Close updates.
        assert app.file_list_panel.rows == []
        assert any("Save & Close" in m for _, m in app.log.entries)


class TestDrawingReadoutFollowsTheTextbox:
    def test_rows_track_blocks_and_their_live_counts(self):
        app = _FakeApp()
        app.file_list_panel = _Panel()
        a = wrapped_drawing_analysis_block(_analysis("a.txt", "alpha one"))
        b = wrapped_drawing_analysis_block(_analysis("b.md", "beta two three"))
        cc._sync_drawings_readout(app, f"notes\n\n{a}\n\n{b}")
        assert app.file_list_panel.rows[-1] == [
            {"name": "a.txt", "tokens": 2},
            {"name": "b.md", "tokens": 3},
        ]
        # Typing elsewhere: same rows, no re-render.
        cc._sync_drawings_readout(app, f"edited notes\n\n{a}\n\n{b}")
        assert len(app.file_list_panel.rows) == 1
        # Trimming one block shrinks its count; deleting the other drops it.
        trimmed = wrapped_drawing_analysis_block(_analysis("b.md", "beta"))
        cc._sync_drawings_readout(app, trimmed)
        assert app.file_list_panel.rows[-1] == [{"name": "b.md", "tokens": 1}]
        cc._sync_drawings_readout(app, "nothing attached any more")
        assert app.file_list_panel.rows[-1] == []
        assert app._drawing_analyses == []

    def test_an_empty_readout_never_touches_the_panel(self):
        app = _FakeApp()
        app.file_list_panel = _Panel()
        cc._sync_drawings_readout(app, "plain notes")
        assert app.file_list_panel.rows == []

    def test_do_context_change_syncs_the_readout(self, monkeypatch):
        app = _FakeApp()
        app.file_list_panel = _Panel()
        app._context_debounce_id = None
        app._loaded_file_data = []
        block = wrapped_drawing_analysis_block(_analysis("a.txt", "alpha one"))
        monkeypatch.setattr(cc, "get_project_context", lambda app: f"notes\n\n{block}")
        monkeypatch.setattr(cc, "update_context_token_label", lambda app: None)
        cc.do_context_change(app)
        assert app.file_list_panel.rows[-1] == [{"name": "a.txt", "tokens": 2}]
        monkeypatch.setattr(cc, "get_project_context", lambda app: "notes")
        cc.do_context_change(app)
        assert app.file_list_panel.rows[-1] == []
