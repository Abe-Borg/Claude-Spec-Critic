"""Every tkinter dialog under ``src/gui`` names its owning window (B-26a).

A ``messagebox`` / ``filedialog`` / ``simpledialog`` call without ``parent=``
is transient for ``tkinter._default_root`` — fine for the main window, wrong
for the Project Context modal or the update dialog (the prompt lands behind
the Toplevel that has the grab). This AST scan fails on any call that
neither passes a ``parent=`` keyword nor splats ``dialog_owner(...)`` (the
form ``review_run_controller`` uses because its hermetic tests drive it
with fixed-arity dialog fakes — see ``src/gui/dialog_owner.py``).

Pure source scan: never imports tkinter.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_GUI_DIR = Path(__file__).resolve().parent.parent / "src" / "gui"
_DIALOG_MODULES = {"messagebox", "filedialog", "simpledialog"}


def _dialog_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _DIALOG_MODULES
        ):
            yield node


def _has_owner(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "parent":
            return True
        if (
            keyword.arg is None  # a ``**`` splat
            and isinstance(keyword.value, ast.Call)
            and isinstance(keyword.value.func, ast.Name)
            and keyword.value.func.id == "dialog_owner"
        ):
            return True
    return False


def _scan(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{call.lineno} {ast.unparse(call.func)}"
        for call in _dialog_calls(tree)
        if not _has_owner(call)
    ]


@pytest.mark.parametrize("path", sorted(_GUI_DIR.glob("*.py")), ids=lambda p: p.name)
def test_every_dialog_call_names_its_owner(path: Path) -> None:
    offenders = _scan(path)
    assert offenders == [], "dialogs without parent=:\n" + "\n".join(offenders)


def test_scan_actually_sees_dialog_calls() -> None:
    # Guard against a silently-vacuous scan: the controllers do call dialogs.
    total = sum(
        1 for path in _GUI_DIR.glob("*.py")
        for _ in _dialog_calls(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert total >= 30


def test_scan_flags_a_missing_owner() -> None:
    tree = ast.parse("messagebox.showerror('t', 'm')\nfiledialog.askopenfilenames(title='x')")
    assert [ast.unparse(c.func) for c in _dialog_calls(tree) if not _has_owner(c)] == [
        "messagebox.showerror",
        "filedialog.askopenfilenames",
    ]
    ok = ast.parse("messagebox.showerror('t', 'm', parent=app)\nmessagebox.askyesno('t', 'm', **dialog_owner(app))")
    assert [c for c in _dialog_calls(ok) if not _has_owner(c)] == []


def test_modal_and_update_dialogs_use_their_toplevel() -> None:
    context = (_GUI_DIR / "context_controller.py").read_text(encoding="utf-8")
    # The modal's own save-time error is owned by the modal, and the shared
    # attach flow resolves its owner from the target textbox's toplevel.
    assert "parent=dialog," in context
    assert "owner = owner_window(app, target_textbox)" in context
    update = (_GUI_DIR / "update_controller.py").read_text(encoding="utf-8")
    assert "parent=_dialog_owner(app)" in update


def test_dialog_owner_helper_contract() -> None:
    pytest.importorskip("tkinter")
    from types import SimpleNamespace

    from src.gui.dialog_owner import dialog_owner, owner_window

    double = SimpleNamespace()
    assert dialog_owner(double) == {}  # a headless double gets no parent
    assert owner_window(double) is double

    class _Widget:
        def winfo_toplevel(self):
            return "toplevel"

    assert owner_window(double, _Widget()) == "toplevel"

    class _Torn:
        def winfo_toplevel(self):
            raise RuntimeError("destroyed")

    assert owner_window(double, _Torn()) is double
