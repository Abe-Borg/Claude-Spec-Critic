"""Context-controller background flows (B-22.1 / B-22.2) and digest progress.

Attachment extraction and the drawing set's validate + chunk-pack step run
on worker threads; every tkinter mutation (cursor restore, dialogs, the
merge, the progress bar) is marshaled back with ``app.after(0, ...)``. The
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
from src.input.drawing_digest import ChunkStatus, DrawingDigestResult  # noqa: E402


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


class _Bar:
    def __init__(self):
        self.values: list[float] = []
        self.packed = False
        self.pack_calls = 0

    def winfo_ismapped(self):
        return self.packed

    def pack(self, **_):
        self.packed = True
        self.pack_calls += 1

    def pack_forget(self):
        self.packed = False

    def configure(self, **_):
        pass

    def set(self, value):
        self.values.append(value)


class _FakeApp:
    def __init__(self):
        self.log = _Log()
        self.cursors: list[str] = []
        self.after_threads: list[int] = []
        self._queued: list = []
        self._cv = threading.Condition()
        self.api_key_entry = SimpleNamespace(get=lambda: "sk-ant-test")
        self.attach_drawings_button = _Widget()
        self.progress_bar = _Bar()
        self.run_button = object()
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
# B-22.1 — attach_drawing_files: validate + pack on a worker
# ---------------------------------------------------------------------------


def _fake_chunk(index=0):
    return SimpleNamespace(index=index, parts=("p",), labels=["a.pdf"], known_page_count=2)


def _digest_result():
    return DrawingDigestResult(
        digest_text="DIGEST BODY",
        chunk_statuses=[
            ChunkStatus(chunk_index=0, file_labels=["a.pdf"], page_count=2, status="completed",
                        input_tokens=10, output_tokens=5)
        ],
        model="test-digest-model",
    )


class TestAttachDrawingFiles:
    def _prepare(self, monkeypatch, *, files=("/dwg/a.pdf",), errors=(), chunks=None):
        monkeypatch.setattr(cc.filedialog, "askopenfilenames", lambda **kw: tuple(files))
        threads: dict[str, int] = {}

        def _validate(paths):
            threads["validate"] = threading.get_ident()
            return ([SimpleNamespace(name="a.pdf", page_count=2)], list(errors))

        def _build(drawing_files, model):
            threads["build"] = threading.get_ident()
            return list(chunks) if chunks is not None else [_fake_chunk()]

        monkeypatch.setattr(cc, "validate_drawing_files", _validate)
        monkeypatch.setattr(cc, "build_digest_chunks", _build)
        return threads

    def test_validate_and_pack_run_off_the_tk_thread_under_the_running_flag(self, monkeypatch):
        app = _FakeApp()
        threads = self._prepare(monkeypatch)
        dialogs = _DialogSpy(monkeypatch, askyesno=False)
        preflight_threads: list[int] = []

        def _preflight(chunks, **kw):
            preflight_threads.append(threading.get_ident())
            return SimpleNamespace(over_window_chunk_indices=[])

        monkeypatch.setattr(cc, "preflight_digest_cost", _preflight)
        monkeypatch.setattr(cc, "format_digest_confirm_message", lambda *a, **k: "confirm?")
        main_ident = threading.get_ident()

        cc.attach_drawing_files(app)
        # Synchronously: the flow is guarded and busy before any pypdf work.
        assert app._drawing_digest_running is True
        assert app.attach_drawings_button.states == ["disabled"]
        assert app.cursors == ["watch"]

        app.pump()  # _on_prepared -> starts the preflight worker
        assert threads["validate"] != main_ident
        assert threads["build"] != main_ident
        assert app.cursors == ["watch", ""]  # cursor restored once packing is done
        assert app._drawing_digest_running is True  # still busy: preflight running
        app.pump()  # _on_preflight_done -> confirm dialog (answered No)
        assert preflight_threads and preflight_threads[0] != main_ident
        assert dialogs.names() == ["askyesno"]
        assert dialogs.calls[0].kw["parent"] is app
        # Declined: flag + button + cursor reset on the Tk thread.
        assert app._drawing_digest_running is False
        assert app.attach_drawings_button.states == ["disabled", "normal"]
        assert app.cursors[-1] == ""

    def test_validation_errors_without_chunks_warn_and_reset(self, monkeypatch):
        app = _FakeApp()
        self._prepare(monkeypatch, errors=("b.pdf: encrypted",), chunks=[])
        dialogs = _DialogSpy(monkeypatch)
        preflight_calls: list = []
        monkeypatch.setattr(cc, "preflight_digest_cost", lambda *a, **k: preflight_calls.append(1))
        cc.attach_drawing_files(app)
        app.pump()
        assert dialogs.names() == ["showwarning"]
        assert dialogs.calls[0].kw["parent"] is app
        assert preflight_calls == []
        assert app._drawing_digest_running is False
        assert app.attach_drawings_button.states == ["disabled", "normal"]

    def test_prepare_failure_is_reported_and_reset(self, monkeypatch):
        app = _FakeApp()
        monkeypatch.setattr(cc.filedialog, "askopenfilenames", lambda **kw: ("/dwg/a.pdf",))

        def _boom(paths):
            raise RuntimeError("page tree unreadable")

        monkeypatch.setattr(cc, "validate_drawing_files", _boom)
        dialogs = _DialogSpy(monkeypatch)
        cc.attach_drawing_files(app)
        app.pump()
        assert dialogs.names() == ["showerror"]
        assert "page tree unreadable" in dialogs.calls[0].message
        assert app._drawing_digest_running is False
        assert app.cursors == ["watch", ""]

    def test_digest_progress_drives_the_bar_and_the_over_cap_refusal_is_owned(self, monkeypatch):
        app = _FakeApp()
        self._prepare(monkeypatch)
        dialogs = _DialogSpy(monkeypatch, askyesno=True)
        monkeypatch.setattr(cc, "preflight_digest_cost", lambda chunks, **kw: SimpleNamespace(over_window_chunk_indices=[]))
        monkeypatch.setattr(cc, "format_digest_confirm_message", lambda *a, **k: "confirm?")

        def _run(chunks, *, progress, log, **kw):
            progress(0.0, "start")
            progress(50.0, "half")
            progress(100.0, "done")
            log("chunk done", level="info")
            return _digest_result()

        monkeypatch.setattr(cc, "run_drawing_digest", _run)
        monkeypatch.setattr(cc, "get_project_context", lambda app: "")
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))

        cc.attach_drawing_files(app)
        app.pump()  # prepared -> preflight
        app.pump()  # preflight done -> confirm (Yes) -> digest worker
        assert app.progress_bar.packed is True
        assert app.progress_bar.values[0] == 0.0
        # Digest worker marshals 3 progress ticks + 1 log + completion.
        app.pump(expected=5)
        assert app.progress_bar.values == [0.0, 0.0, 0.5, 1.0]
        assert ("info", "chunk done") in app.log.entries
        assert merged and "DIGEST BODY" in merged[0]
        assert app.progress_bar.packed is False  # hidden on reset
        assert app._drawing_digest_running is False
        assert dialogs.names() == ["askyesno"]

    def test_over_cap_digest_is_refused_with_an_owned_dialog(self, monkeypatch):
        app = _FakeApp()
        self._prepare(monkeypatch)
        dialogs = _DialogSpy(monkeypatch, askyesno=True)
        monkeypatch.setattr(cc, "preflight_digest_cost", lambda chunks, **kw: SimpleNamespace(over_window_chunk_indices=[]))
        monkeypatch.setattr(cc, "format_digest_confirm_message", lambda *a, **k: "confirm?")
        monkeypatch.setattr(cc, "run_drawing_digest", lambda chunks, **kw: _digest_result())
        monkeypatch.setattr(cc, "get_project_context", lambda app: "")
        monkeypatch.setattr(cc, "context_within_token_cap", lambda text: (200_000, False))
        merged: list[str] = []
        monkeypatch.setattr(cc, "set_context_text", lambda app, text: merged.append(text))
        cc.attach_drawing_files(app)
        app.pump()
        app.pump()
        app.pump()  # digest done
        assert merged == []
        assert dialogs.names() == ["askyesno", "showerror"]
        assert dialogs.calls[1].title == "Digest too large for Project Context"
        assert dialogs.calls[1].kw["parent"] is app

    def test_review_owns_the_progress_bar_while_processing(self):
        app = _FakeApp()
        app.is_processing = True
        cc._show_digest_progress(app, 50.0)
        cc._hide_digest_progress(app)
        assert app.progress_bar.values == []
        assert app.progress_bar.packed is False
