"""Integration tests for the threaded "Attach Drawings…" flow.

Exercises ``context_controller.attach_drawings`` end-to-end without the network,
PyMuPDF, or a real Tk window:

- the drawing engine is replaced at the ``_run_drawing_extraction`` seam with a
  fake returning a synthetic ``DrawingContext`` (duck-typed),
- the worker ``_spawn`` is made synchronous and the fake app's ``after`` runs
  callbacks inline, so the whole flow completes within the call,
- ``get_project_context`` / ``set_context_text`` are redirected to an in-memory
  store so no Tk textbox is needed, and
- ``count_tokens`` is stubbed (word count) so the cap check needs no download.

The whole module skips when customtkinter / tkinter are unavailable (the common
headless-CI case), matching the suite's GUI-test convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from src.gui import context_attachment as ca  # noqa: E402
from src.gui import context_controller as cc  # noqa: E402
from src.core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    """Duck-typed stand-in for ``drawings.DrawingContext`` (no PyMuPDF)."""

    combined_text: str
    sheet_count: int = 1
    file_count: int = 1
    _ok: int = 1
    errors: list = field(default_factory=list)

    @property
    def ok_sheet_count(self) -> int:
        return self._ok


@dataclass
class _Estimate:
    """Minimal stand-in for ``drawings.cost.DrawingCostEstimate`` (no PyMuPDF)."""

    sheet_count: int = 1


class _FakeLog:
    def __init__(self):
        self.entries: list[tuple] = []

    def log(self, msg, level="info", **kw):
        self.entries.append(("log", msg, level))

    def log_step(self, msg):
        self.entries.append(("step", msg))

    def log_success(self, msg):
        self.entries.append(("success", msg))

    def log_warning(self, msg):
        self.entries.append(("warning", msg))

    def log_error(self, msg):
        self.entries.append(("error", msg))

    def kinds(self) -> list[str]:
        return [e[0] for e in self.entries]


class _FakeProgressBar:
    def __init__(self):
        self.values: list[float] = []
        self.packed = False

    def pack(self, **kw):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def set(self, v):
        self.values.append(v)

    def configure(self, **kw):
        pass


class _FakeEntry:
    def __init__(self, val):
        self._val = val

    def get(self):
        return self._val


class _FakeApp:
    def __init__(self, key="sk-ant-test"):
        self.is_processing = False
        self._drawings_busy = False
        self.api_key_entry = _FakeEntry(key)
        self.log = _FakeLog()
        self.progress_bar = _FakeProgressBar()
        self.run_button = object()

    def after(self, delay, func=None, *args):
        if func is not None:
            func(*args)

    def configure(self, **kw):  # cursor="watch" during the sync .docx/.md attach
        pass

    def update_idletasks(self):
        pass


@pytest.fixture
def env(monkeypatch):
    """Wire the synchronous, network-free, PyMuPDF-free test harness.

    Returns a namespace with the fake app, the in-memory context store, the
    messagebox-call recorder, and an ``extract_calls`` list so a test can assert
    extraction was (or was not) reached.
    """
    # ``attach_drawings`` writes ``os.environ["ANTHROPIC_API_KEY"]`` (like the
    # review / recover flows). Pin it via monkeypatch so that write is reverted
    # at teardown and can't leak the fake key into later tests (e.g.
    # test_live_capture's sentinel-key guard).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")

    store = {"ctx": ""}
    monkeypatch.setattr(cc, "get_project_context", lambda app: store["ctx"])
    monkeypatch.setattr(cc, "set_context_text", lambda app, text: store.__setitem__("ctx", text))
    monkeypatch.setattr(cc, "_spawn", lambda target, args: target(*args))
    monkeypatch.setattr(ca, "count_tokens", lambda s: len(s.split()))

    rec = {"warning": [], "error": [], "info": []}
    monkeypatch.setattr(cc.messagebox, "showwarning", lambda *a, **k: rec["warning"].append(a))
    monkeypatch.setattr(cc.messagebox, "showerror", lambda *a, **k: rec["error"].append(a))
    monkeypatch.setattr(cc.messagebox, "showinfo", lambda *a, **k: rec["info"].append(a))

    # Cost-confirm gate seams (W4): stub so the flow never touches real PyMuPDF /
    # ``list_sheets`` or a real Tk dialog (which crashes headless). Default: a
    # non-None estimate + an auto-"Yes", so existing flows proceed as before. A
    # test can flip ``confirm["return"]`` to False or re-stub the estimate.
    confirm = {"return": True, "calls": []}
    monkeypatch.setattr(
        cc, "_estimate_drawing_cost", lambda pdfs: _Estimate(len(pdfs))
    )

    def _fake_confirm(app, estimate):
        confirm["calls"].append(estimate)
        return confirm["return"]

    monkeypatch.setattr(cc, "_confirm_drawing_cost", _fake_confirm)

    extract_calls: list = []

    class NS:
        app = _FakeApp()
        store_ = store
        rec_ = rec
        calls = extract_calls

    ns = NS()

    def set_picker(paths):
        monkeypatch.setattr(cc.filedialog, "askopenfilenames", lambda **kw: tuple(paths))

    def set_extraction(ctx=None, *, raises=None):
        def _fake(pdfs, *, progress):
            extract_calls.append(list(pdfs))
            progress(0, len(pdfs), "Analyzing sheet 1")
            if raises is not None:
                raise raises
            progress(len(pdfs), len(pdfs), "Done")
            return ctx

        monkeypatch.setattr(cc, "_run_drawing_extraction", _fake)

    ns.set_picker = set_picker
    ns.set_extraction = set_extraction
    ns.confirm = confirm
    return ns


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_attach_drawings_happy_path(env):
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(_Ctx(combined_text="VAV-3 serves Rm 120", sheet_count=1, file_count=1, _ok=1))

    cc.attach_drawings(env.app)

    assert "--- BEGIN ATTACHMENT: DRAWING DIGEST" in env.store_["ctx"]
    assert "VAV-3 serves Rm 120" in env.store_["ctx"]
    assert env.rec_["error"] == [] and env.rec_["warning"] == []
    assert "success" in env.app.log.kinds()
    # UI reset to idle.
    assert env.app.is_processing is False
    assert env.app._drawings_busy is False
    assert env.app.progress_bar.packed is False
    assert 1.0 in env.app.progress_bar.values  # completion progress marshaled


def test_attach_drawings_surfaces_sheet_errors_but_still_attaches(env):
    env.set_picker(["/tmp/set.pdf"])
    env.set_extraction(
        _Ctx(combined_text="partial body", sheet_count=2, _ok=1, errors=["set.pdf p2: boom"])
    )

    cc.attach_drawings(env.app)

    assert len(env.rec_["warning"]) == 1  # the "some sheets could not be analyzed" warning
    assert "partial body" in env.store_["ctx"]  # readable sheet still attached
    assert "success" in env.app.log.kinds()


def test_attach_drawings_refuses_over_cap(env):
    env.set_picker(["/tmp/big.pdf"])
    big = "word " * (PROJECT_CONTEXT_MAX_TOKENS + 1)
    env.set_extraction(_Ctx(combined_text=big, sheet_count=50, file_count=1, _ok=50))

    cc.attach_drawings(env.app)

    assert len(env.rec_["error"]) == 1  # over-cap showerror
    assert env.store_["ctx"] == ""  # context unchanged — refused, not truncated
    assert "warning" in env.app.log.kinds()


def test_attach_drawings_empty_digest_attaches_nothing(env):
    env.set_picker(["/tmp/blank.pdf"])
    env.set_extraction(_Ctx(combined_text="", sheet_count=1, _ok=0, errors=["blank.pdf: failed"]))

    cc.attach_drawings(env.app)

    assert env.store_["ctx"] == ""  # nothing attached
    # the sheet error is still surfaced, plus the "no digest" warning
    assert env.rec_["warning"]
    assert "warning" in env.app.log.kinds()
    assert "success" not in env.app.log.kinds()


def test_attach_drawings_all_sheets_failed_attaches_nothing(env):
    # The engine returns non-empty combined_text even when every sheet fails
    # (header + failure blockquotes); a 0/N set must still attach nothing.
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(
        _Ctx(
            combined_text="## Sheet 1/1: M-101\n\n> [drawing analysis failed: 401]",
            sheet_count=1,
            _ok=0,
            errors=["M-101: 401 invalid x-api-key"],
        )
    )

    cc.attach_drawings(env.app)

    assert env.store_["ctx"] == ""  # nothing attached despite non-empty digest text
    assert env.rec_["warning"]  # sheet errors surfaced
    assert "success" not in env.app.log.kinds()
    # All-failed dialog must NOT promise an attachment; it points at a retry.
    title, body = env.rec_["warning"][0][0], env.rec_["warning"][0][1]
    assert title == "No sheets could be analyzed"
    assert "nothing was attached" in body
    assert "readable sheets will still be attached" not in body


def test_attach_drawings_cost_confirm_proceeds(env):
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(_Ctx(combined_text="VAV-3 serves Rm 120"))
    # env.confirm["return"] defaults to True (user clicks "Yes").

    cc.attach_drawings(env.app)

    assert env.confirm["calls"]  # the cost gate was consulted
    assert env.calls == [["/tmp/M-101.pdf"]]  # and the run proceeded
    assert "VAV-3 serves Rm 120" in env.store_["ctx"]


def test_attach_drawings_cost_confirm_cancel_aborts(env):
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(_Ctx(combined_text="VAV-3 serves Rm 120"))
    env.confirm["return"] = False  # user clicks "No"

    cc.attach_drawings(env.app)

    assert env.confirm["calls"]  # gate consulted
    assert env.calls == []  # extraction never started
    assert env.store_["ctx"] == ""  # nothing attached
    assert env.app.is_processing is False  # UI never went busy
    assert env.app._drawings_busy is False
    assert any("cancel" in str(e).lower() for e in env.app.log.entries)


def test_attach_drawings_cost_estimate_none_skips_prompt(env, monkeypatch):
    # A failed sheet-count (None estimate) must NOT block the run — the worker
    # surfaces the real error. The confirmation prompt is skipped entirely.
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(_Ctx(combined_text="VAV-3 serves Rm 120"))
    monkeypatch.setattr(cc, "_estimate_drawing_cost", lambda pdfs: None)

    cc.attach_drawings(env.app)

    assert env.confirm["calls"] == []  # prompt skipped when estimate is None
    assert env.calls == [["/tmp/M-101.pdf"]]  # run proceeded anyway


def test_attach_drawings_no_selection_is_noop(env):
    env.set_picker([])  # user cancelled the dialog
    env.set_extraction(_Ctx(combined_text="unused"))

    cc.attach_drawings(env.app)

    assert env.calls == []  # extraction never started
    assert env.store_["ctx"] == ""
    assert env.app.is_processing is False


def test_attach_drawings_skips_non_pdf_selection(env):
    env.set_picker(["/tmp/notes.txt"])  # only a non-PDF picked
    env.set_extraction(_Ctx(combined_text="unused"))

    cc.attach_drawings(env.app)

    assert len(env.rec_["warning"]) == 1  # unsupported-files warning
    assert env.calls == []  # no PDFs => extraction never started
    assert env.store_["ctx"] == ""


def test_attach_drawings_requires_api_key(env):
    env.app.api_key_entry = _FakeEntry("   ")  # blank key
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(_Ctx(combined_text="unused"))

    cc.attach_drawings(env.app)

    assert len(env.rec_["error"]) == 1  # "API key required"
    assert env.calls == []
    assert env.app.is_processing is False


def test_attach_drawings_busy_guard_blocks_reentry(env, monkeypatch):
    env.app.is_processing = True  # a review / resume is already running
    picked = []
    monkeypatch.setattr(
        cc.filedialog, "askopenfilenames", lambda **kw: picked.append(1) or ()
    )

    cc.attach_drawings(env.app)

    assert picked == []  # returned before even opening the picker


def test_attach_drawings_extraction_failure_resets_ui(env):
    env.set_picker(["/tmp/M-101.pdf"])
    env.set_extraction(_Ctx(combined_text="unused"), raises=RuntimeError("render exploded"))

    cc.attach_drawings(env.app)

    assert len(env.rec_["error"]) == 1
    assert "render exploded" in env.rec_["error"][0][1]
    assert "error" in env.app.log.kinds()
    assert env.app.is_processing is False
    assert env.app._drawings_busy is False
    assert env.app.progress_bar.packed is False


# --------------------------------------------------------------------------- #
# The .md / .txt file-attachment path (the standalone tool's saved digest)
# --------------------------------------------------------------------------- #


def test_extract_context_attachments_wraps_each_md_txt_file(tmp_path):
    (tmp_path / "a.md").write_text("alpha digest", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta notes", encoding="utf-8")
    combined, errors = cc.extract_context_attachments(
        [tmp_path / "a.md", tmp_path / "b.txt"]
    )
    assert errors == []
    assert "--- BEGIN ATTACHMENT: a.md ---\nalpha digest\n--- END ATTACHMENT: a.md ---" in combined
    assert "--- BEGIN ATTACHMENT: b.txt ---\nbeta notes\n--- END ATTACHMENT: b.txt ---" in combined


def test_attach_context_files_accepts_markdown_digest(env, tmp_path):
    md = tmp_path / "drawing_context.md"
    md.write_text("# Drawing Set Context Digest\n\nVAV-3 serves Rm 120", encoding="utf-8")
    env.set_picker([str(md)])

    cc.attach_context_files(env.app)

    assert "Drawing Set Context Digest" in env.store_["ctx"]
    assert "--- BEGIN ATTACHMENT: drawing_context.md ---" in env.store_["ctx"]
    assert env.rec_["warning"] == [] and env.rec_["error"] == []
