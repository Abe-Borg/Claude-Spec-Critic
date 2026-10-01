"""Native integration checks; run with DISPLAY or xvfb-run, no live API."""
from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
ctk = pytest.importorskip("customtkinter")

from src.gui import about_usage_dialogs
from src.gui import trust_content as content
from src.gui.trust_dialogs import Diagram, RuntimeCard, Table, TrustDialog, show_trust_dialog, make_button_focusable


@pytest.fixture
def root(monkeypatch):
    import tkinter as tk
    import urllib.request
    from anthropic import Anthropic
    def blocked(*args, **kwargs):
        pytest.fail("Trust UI attempted a network request")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(Anthropic, "request", blocked)
    try:
        window = ctk.CTk()
    except tk.TclError as exc:
        pytest.skip(f"Native trust-dialog test needs a display: {exc}")
    errors = []
    window.report_callback_exception = lambda *args: errors.append(args)
    window.geometry("1100x900")
    window.trust_button = ctk.CTkButton(window, text="Why Trust It?", command=lambda: about_usage_dialogs.show_trust_dialog(window))
    window.trust_button.pack()
    make_button_focusable(window.trust_button)
    window.update()
    yield window
    window.destroy()
    assert errors == []


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def test_help_button_opens_short_then_stacked_dossier(root):
    root.trust_button.invoke()
    short = root._trust_dialog
    root.update()
    assert short.detail_button.cget("text") == content.DETAIL_BUTTON
    assert root.grab_current() is short
    short.detail_button.invoke()
    root.update()
    dossier = short._child_dialog
    assert isinstance(dossier, TrustDialog)
    assert dossier.parent_dialog is short and short.winfo_exists()
    assert root.grab_current() is dossier
    assert root.focus_get().winfo_toplevel() is dossier
    assert dossier.rail.winfo_ismapped()
    assert set(dossier.sections) == {t.anchor for t in content.TOPICS}
    cards = [w for w in descendants(dossier) if isinstance(w, RuntimeCard)]
    assert [card.action_id for card in cards] == [a.identity for a in content.ACTIONS]
    assert all(tuple(label for label, _ in card.rows) == content.RUNTIME_LABELS for card in cards)
    assert dossier.winfo_height() <= root.winfo_screenheight() * .88
    # A keyboard event closes exactly one window and restores the opener.
    dossier.event_generate("<Escape>")
    root.update()
    assert not dossier.winfo_exists() and short.winfo_exists()
    assert root.grab_current() is short
    assert root.focus_get() is short.detail_button._canvas
    short.event_generate("<Escape>")
    root.update()
    assert not short.winfo_exists()
    assert root.focus_get() is root.trust_button._canvas
    root.trust_button._canvas.event_generate("<Return>")
    root.update()
    assert root._trust_dialog.winfo_exists()
    root._trust_dialog.close()


def test_focus_containment_parent_close_and_contents(root):
    short = show_trust_dialog(root)
    root.update()
    dossier = short.open_details()
    root.update()
    short._escape()  # obscured parent cannot close its child or itself
    short.close()
    assert short.winfo_exists() and dossier.winfo_exists()
    visible = [node for node in dossier.focus_nodes if node.winfo_viewable()]
    visible[-1].focus_force()
    dossier.event_generate("<Tab>")
    root.update()
    assert root.focus_get() is visible[0]
    dossier.event_generate("<Shift-Tab>")
    root.update()
    assert root.focus_get() is visible[-1]
    dossier.navigate("security")
    root.update()
    assert dossier.current_anchor == "security"
    # Contents never remove/hide another section.
    assert all(section.winfo_manager() == "text" for section in dossier.sections.values())
    assert all(dossier.scroll.mark_names().count(anchor) == 1 for anchor in dossier.sections)
    dossier.close(); short.close()


def test_narrow_layout_and_theme_changes(root):
    short = show_trust_dialog(root)
    root.update()
    dossier = short.open_details()
    root.update()
    dossier.geometry("540x760")
    root.update()
    assert not dossier.rail.winfo_ismapped()
    tables = [w for w in descendants(dossier) if isinstance(w, Table)]
    assert tables
    # Text embeds off-screen blocks lazily. Check each table after showing it,
    # including ones below the long runtime inventory.
    for table in tables:
        dossier.scroll.see(dossier.scroll.block_marks[table.master])
        root.update()
        assert table._wide is False
        assert table.winfo_width() < 640
    for mode in ("light", "dark"):
        ctk.set_appearance_mode(mode)
        root.update()
        diagrams = [w for w in descendants(dossier) if isinstance(w, Diagram)]
        assert len(diagrams) == 1
        diagram = diagrams[0]
        assert diagram.canvas.cget("background")
        assert diagram.aria_label == content.FLOW_DESCRIPTION
    dossier.geometry("1120x760")
    root.update()
    assert dossier.rail.winfo_ismapped()
    dossier.close(); short.close()
