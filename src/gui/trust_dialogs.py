# Purpose: a source-backed trust dossier for the professional signing the output.
# If the implementation changes, this changes with it. A trust document that has
# drifted from the code is worse than none.
"""Native, stacked trust dialogs; no browser, server or external assets.

Tk supplies native window titles/transient dialog semantics instead of DOM ARIA
roles. The inline SVG retains its role/aria-label in the saved dossier; this
renderer draws the same primitives and displays the text alternative in Tk.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from xml.etree import ElementTree

import customtkinter as ctk

from .widgets import COLORS
from . import trust_content as content


def token(name):
    """Use the existing dark tokens and CustomTkinter's light theme tokens."""
    theme = ctk.ThemeManager.theme
    light = {
        "bg_dark": theme["CTk"]["fg_color"][0],
        "bg_card": theme["CTkFrame"]["fg_color"][0],
        "bg_input": theme["CTkFrame"]["top_fg_color"][0],
        "text_primary": theme["CTkLabel"]["text_color"][0],
        "text_secondary": theme["CTkLabel"]["text_color"][0],
        "text_muted": theme["CTkLabel"]["text_color"][0],
        "border": theme["CTkFrame"]["border_color"][0],
    }
    return (light[name], COLORS[name]) if name in light else COLORS[name]


def make_button_focusable(button):
    """Give the app's canvas-based buttons keyboard activation and a focus ring."""
    node = button._canvas
    border = button.cget("border_width"), button.cget("border_color")
    node.configure(takefocus=1)
    def invoke(event):
        button.invoke()
        return "break"
    node.bind("<Return>", invoke)
    node.bind("<space>", invoke)
    node.bind("<FocusIn>", lambda event: button.configure(border_width=2, border_color=COLORS["accent"]))
    node.bind("<FocusOut>", lambda event: button.configure(border_width=border[0], border_color=border[1]))
    return node


def label(parent, text, *, size=14, bold=False, muted=False):
    widget = ctk.CTkLabel(
        parent, text=text, anchor="w", justify="left",
        font=ctk.CTkFont(family="Segoe UI", size=size, weight="bold" if bold else "normal"),
        text_color=token("text_secondary" if muted else "text_primary" if bold else "text_secondary"),
        wraplength=400,
    )
    widget.pack(fill="x", padx=12, pady=(4, 8))
    def wrap(event):
        width = max(100, int((event.width - 16) / widget._get_widget_scaling()))
        if widget.cget("wraplength") != width:
            widget.configure(wraplength=width)
    # CTkLabel.bind also binds its inner text label: using that text's width
    # to resize itself creates a shrink/grow loop. Observe the outer widget.
    tk.Misc.bind(widget, "<Configure>", wrap, add="+")
    return widget


class Note(ctk.CTkFrame):
    def __init__(self, parent, title, body):
        super().__init__(parent, fg_color=token("bg_input"), corner_radius=8)
        self.pack(fill="x", pady=8)
        label(self, title, bold=True)
        label(self, body)


class Table(ctk.CTkFrame):
    """Rows use shared columns when wide and labeled cells when narrow."""
    def __init__(self, parent, headers, rows):
        super().__init__(parent, fg_color="transparent")
        self.pack(fill="x", pady=8)
        self.rows = []
        self.headers = headers
        self._wide = None
        self._header = ctk.CTkFrame(self, fg_color=token("bg_input"))
        self._header.pack(fill="x")
        for index, header in enumerate(headers):
            cell = ctk.CTkFrame(self._header, fg_color="transparent")
            cell.grid(row=0, column=index, sticky="nsew", padx=4)
            self._header.grid_columnconfigure(index, weight=1, uniform="trust_headers")
            label(cell, header, size=12, bold=True)
        for values in rows:
            row = ctk.CTkFrame(self, fg_color=token("bg_card"), corner_radius=0)
            row.pack(fill="x", pady=(2, 0))
            cells = []
            for header, value in zip(headers, values):
                cell = ctk.CTkFrame(row, fg_color="transparent")
                heading = label(cell, header, size=12, bold=True)
                body = label(cell, value)
                cells.append((cell, heading, body))
            self.rows.append((row, cells))
        self.bind("<Configure>", self._layout)

    def _layout(self, event):
        wide = event.width / self._get_widget_scaling() >= 640
        if wide == self._wide:
            return
        self._wide = wide
        if wide:
            self._header.pack(fill="x", before=self.rows[0][0])
        else:
            self._header.pack_forget()
        for row, cells in self.rows:
            for index in range(len(cells)):
                row.grid_columnconfigure(index, weight=1 if wide else 0, uniform="trust" if wide else "")
            row.grid_columnconfigure(0, weight=1)
            for index, (cell, heading, body) in enumerate(cells):
                cell.grid_forget()
                cell.grid(row=0 if wide else index, column=index if wide else 0, sticky="nsew", padx=4)
                if wide:
                    heading.pack_forget()
                else:
                    heading.pack(fill="x", padx=12, pady=(4, 8), before=body)


class Contrast(Table):
    def __init__(self, parent, headers, values):
        super().__init__(parent, headers, (values,))


class RuntimeCard(ctk.CTkFrame):
    def __init__(self, parent, number, action, facts):
        super().__init__(parent, fg_color=token("bg_card"), corner_radius=8,
                         border_width=1, border_color=token("border"))
        self.pack(fill="x", pady=8)
        self.action_id = action.identity
        self.rows = action.rows(facts)
        label(self, f"{number}. {action.title}", size=16, bold=True)
        for heading, body in self.rows:
            label(self, heading + ":", size=12, bold=True)
            label(self, body)


class Diagram(ctk.CTkFrame):
    """Draw the inline SVG's rect/text/line subset without an SVG dependency."""
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self.pack(fill="x", pady=8)
        self.svg = ElementTree.fromstring(content.FLOW_SVG)
        self.aria_label = self.svg.attrib["aria-label"]
        self.canvas = tk.Canvas(self, highlightthickness=0, height=300, takefocus=0)
        self.canvas.pack(fill="x", padx=12)
        label(self, self.aria_label, size=12)
        self.canvas.bind("<Configure>", self._draw_svg)

    def _set_appearance_mode(self, mode_string):
        super()._set_appearance_mode(mode_string)
        if hasattr(self, "canvas"):
            self._draw_svg()

    def _draw_svg(self, event=None):
        width = self.canvas.winfo_width()
        scale = max(0.1, width / 760)
        color = self._apply_appearance_mode(token("text_primary"))
        background = self._apply_appearance_mode(token("bg_card"))
        self.canvas.configure(background=background, height=300 * scale)
        self.canvas.delete("all")
        for item in self.svg:
            kind = item.tag.rsplit("}", 1)[-1]
            a = item.attrib
            if kind == "rect":
                x, y, w, h = (float(a[key]) * scale for key in ("x", "y", "width", "height"))
                self.canvas.create_rectangle(x, y, x + w, y + h, outline=color)
            elif kind == "line":
                coords = [float(a[key]) * scale for key in ("x1", "y1", "x2", "y2")]
                self.canvas.create_line(*coords, fill=color, width=2, arrow="last",
                                        dash=(6, 5) if "stroke-dasharray" in a else ())
            elif kind == "text":
                self.canvas.create_text(float(a["x"]) * scale, float(a["y"]) * scale,
                                        text=item.text, anchor="w", fill=color,
                                        font=("Segoe UI", max(8, int(15 * scale))))


class DocumentScroll(tk.Text):
    """One native scroll with embedded blocks, avoiding a document-sized pixmap."""
    def __init__(self, parent, owner):
        self.owner = owner
        self.blocks = []
        self.block_marks = {}
        color = token("bg_dark")[1 if ctk.get_appearance_mode() == "Dark" else 0]
        super().__init__(parent, bg=color, borderwidth=0, highlightthickness=0,
                         wrap="word", cursor="arrow", takefocus=0, state="disabled")
        self.bind("<Configure>", self._resize_blocks)

    def append(self, widget, mark=None):
        self.configure(state="normal")
        index = self.index("end-1c")
        if mark:
            self.mark_set(mark, index)
            self.mark_gravity(mark, "left")
        self.window_create(index, window=widget, stretch=True, align="top")
        self.insert("end-1c", "\n")
        self.configure(state="disabled")
        self.blocks.append(widget)
        self.block_marks[widget] = index
        widget.configure(width=max(100, self.winfo_width() - 12))

    def _resize_blocks(self, event):
        for widget in self.blocks:
            width = max(100, int((event.width - 12) / widget._get_widget_scaling()))
            if widget.cget("width") != width:
                widget.configure(width=width)

    def reveal_focus(self, node):
        for widget, index in self.block_marks.items():
            if str(node).startswith(str(widget) + "."):
                self.see(index)
                return


class BlockFrame(ctk.CTkFrame):
    """Fix block width to the viewport and derive height from wrapped children."""
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.pack_propagate(False)
        self._fit_pending = False

    def track_layout(self):
        def visit(widget):
            tk.Misc.bind(widget, "<Configure>", self._schedule_fit, add="+")
            for child in widget.winfo_children():
                visit(child)
        visit(self)
        self._schedule_fit()

    def _schedule_fit(self, event=None):
        if not self._fit_pending:
            self._fit_pending = True
            self.after_idle(self._fit)

    def _fit(self):
        self._fit_pending = False
        height = 0
        for child in self.pack_slaves():
            padding = child.pack_info()["pady"]
            height += child.winfo_reqheight() + (sum(padding) if isinstance(padding, tuple) else 2 * padding)
        height = max(1, int(height / self._get_widget_scaling()))
        if self.cget("height") != height:
            self.configure(height=height)


class Section(BlockFrame):
    def __init__(self, parent, topic):
        super().__init__(parent)
        self.anchor = topic.anchor
        label(self, topic.kicker.upper(), size=12, bold=True, muted=True)
        label(self, topic.title, size=22, bold=True)


class TrustDialog(ctk.CTkToplevel):
    def __init__(self, parent, *, detailed=False, opener=None):
        super().__init__(parent)
        self.parent_dialog = parent
        self.opener = opener or parent.focus_get()
        self.detailed = detailed
        self.sections = {}
        self.rail_buttons = {}
        self.focus_nodes = []
        self._closed = False
        self._highlight_pending = False
        self._rail_visible = False
        self._child_dialog = None
        self._activate_id = None
        self._highlight_id = None
        self.facts = content.fact_values()
        self.title("Spec Critic — Exactly what runs" if detailed else "Spec Critic — Why trust it?")
        self.configure(fg_color=token("bg_dark"))
        self.transient(parent)
        scale = self._get_window_scaling()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = int(min(1120 if detailed else 700, screen_w * 0.94) / scale)
        height = int(screen_h * 0.86 / scale)
        self.geometry(f"{width}x{height}")
        self.minsize(min(360, width), min(320, height))
        self.maxsize(int(screen_w / scale), int(screen_h * 0.88 / scale))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", self._escape)
        self.bind("<Tab>", lambda event: self._tab(event, 1))
        self.bind("<Shift-Tab>", lambda event: self._tab(event, -1))
        self.bind("<ISO_Left_Tab>", lambda event: self._tab(event, -1))
        self.bind("<Next>", lambda event: self._page(1))
        self.bind("<Prior>", lambda event: self._page(-1))
        self.bind("<Home>", lambda event: self._edge(0))
        self.bind("<End>", lambda event: self._edge(1))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda event: self._wheel(event, -1))
        self.bind("<Button-5>", lambda event: self._wheel(event, 1))

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 0))
        self.close_button = self.button(header, "Back to Why trust it?" if detailed else "Close", self.close)
        self.close_button.pack(side="right", padx=8)
        label(header, "Exactly what runs" if detailed else "Why trust it?", size=24, bold=True)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=16, pady=12)
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(1, weight=1)
        self.rail = ctk.CTkFrame(self.body, width=210, fg_color=token("bg_card"))
        self.scroll = DocumentScroll(self.body, self)
        self.scroll.grid(row=0, column=1, sticky="nsew")
        self.scrollbar = ctk.CTkScrollbar(self.body, command=self.scroll.yview)
        self.scrollbar.grid(row=0, column=2, sticky="ns")
        self.canvas = self.scroll
        self.canvas.configure(yscrollcommand=self._on_scroll)
        tk.Misc.bind(self.body, "<Configure>", self._resize, add="+")
        if detailed:
            label(self.rail, "Contents", bold=True)
            for topic in content.TOPICS:
                button = self.button(self.rail, topic.title, lambda anchor=topic.anchor: self.navigate(anchor), width=200)
                button.configure(height=36, font=ctk.CTkFont(family="Segoe UI", size=12))
                button._text_label.configure(wraplength=180, justify="left")
                button.pack(fill="x", padx=6, pady=2)
                self.rail_buttons[topic.anchor] = button
                section = Section(self.scroll, topic)
                self.sections[topic.anchor] = section
                self.scroll.append(section, topic.anchor)
                for block in topic.blocks:
                    if block.kind == "runtime":
                        for index, action in enumerate(content.ACTIONS, 1):
                            frame = self.block_frame()
                            RuntimeCard(frame, index, action, self.facts)
                    else:
                        self.render_block(self.block_frame(), block)
        else:
            label(self.block_frame(), content.LEAD, size=16)
            for block in content.SHORT_POINTS:
                frame = self.block_frame()
                label(frame, block.title, size=16, bold=True)
                label(frame, str(block.body).format_map(self.facts))
            note = Note(self.block_frame(), "Not convinced?", content.CLOSING)
            self.detail_button = self.button(note, content.DETAIL_BUTTON, self.open_details)
            self.detail_button.pack(fill="x", padx=12, pady=12)
            tk.Misc.bind(self.detail_button, "<Configure>", self._wrap_detail_button, add="+")
        for frame in self.scroll.blocks:
            frame.track_layout()
        self._activate_id = self.after_idle(self._activate)

    def block_frame(self):
        frame = BlockFrame(self.scroll)
        self.scroll.append(frame)
        return frame

    def _set_appearance_mode(self, mode_string):
        super()._set_appearance_mode(mode_string)
        if hasattr(self, "scroll"):
            self.scroll.configure(bg=self._apply_appearance_mode(token("bg_dark")))

    def button(self, parent, text, command, width=180):
        button = ctk.CTkButton(parent, text=text, command=command, width=width, height=34,
                               font=ctk.CTkFont(family="Segoe UI", size=13),
                               fg_color=token("bg_input"), hover_color=token("border"),
                               text_color=token("text_primary"))
        node = make_button_focusable(button)
        self.focus_nodes.append(node)
        return button

    def _wrap_detail_button(self, event):
        text = self.detail_button._text_label
        width = max(100, event.width - 28)
        if int(text.cget("wraplength")) != width:
            text.configure(wraplength=width)

    def render_block(self, parent, block):
        def fmt(value):
            return str(value).format_map(self.facts)
        if block.kind == "text":
            if block.title:
                label(parent, block.title, bold=True)
            label(parent, fmt(block.body))
        elif block.kind == "note":
            Note(parent, block.title, fmt(block.body))
        elif block.kind in ("table", "engine", "prices"):
            rows = content.engine_rows(self.facts) if block.kind == "engine" else content.price_rows(self.facts) if block.kind == "prices" else block.body
            Table(parent, block.title.split(" | "), tuple(tuple(fmt(cell) for cell in row) for row in rows))
        elif block.kind == "contrast":
            Contrast(parent, block.title.split(" | "), tuple(fmt(cell) for cell in block.body))
        elif block.kind == "diagram":
            Diagram(parent)
        elif block.kind == "runtime":
            for index, action in enumerate(content.ACTIONS, 1):
                RuntimeCard(parent, index, action, self.facts)
        elif block.kind in ("bullets", "steps", "commitments"):
            for index, item in enumerate(block.body, 1):
                if block.kind == "commitments":
                    label(parent, item[0], size=16, bold=True)
                    label(parent, fmt(item[1]))
                else:
                    label(parent, (f"{index}. " if block.kind == "steps" else "• ") + fmt(item))
        elif block.kind == "links":
            box = Note(parent, block.title, "Open these links deliberately; provider policies can change independently of this application.")
            for title, url in content.FURTHER_READING:
                link = self.button(box, title, lambda target=url: webbrowser.open(target))
                link.pack(fill="x", padx=12, pady=4)

    def _activate(self):
        if self._closed:
            return
        self.lift()
        self.grab_set()
        self.focus_nodes[0].focus_force()
        self._resize()

    def open_details(self):
        if self._child_dialog is None or not self._child_dialog.winfo_exists():
            self.scroll.reveal_focus(self.detail_button._canvas)
            self._child_dialog = TrustDialog(self, detailed=True, opener=self.detail_button._canvas)
        else:
            self._child_dialog._activate()
        return self._child_dialog

    def _resize(self, event=None):
        width = event.width if event is not None else self.body.winfo_width()
        show = self.detailed and width / self.body._get_widget_scaling() >= 980
        if show != self._rail_visible:
            self._rail_visible = show
            if show:
                self.rail.grid(row=0, column=0, sticky="ns", padx=(0, 12))
            else:
                self.rail.grid_remove()

    def _on_scroll(self, first, last):
        self.scrollbar.set(first, last)
        if not self._highlight_pending and not self._closed:
            self._highlight_pending = True
            self._highlight_id = self.after_idle(self._highlight)

    def _highlight(self):
        self._highlight_pending = False
        if self._closed or not self.sections:
            return
        top = self.scroll.index("@0,24")
        current = next(iter(self.sections))
        for anchor, section in self.sections.items():
            if self.scroll.compare(anchor, "<=", top):
                current = anchor
        if getattr(self, "current_anchor", None) == current:
            return
        self.current_anchor = current
        for anchor, button in self.rail_buttons.items():
            button.configure(fg_color=COLORS["accent_hover"] if anchor == current else token("bg_input"),
                             text_color=COLORS["text_primary"] if anchor == current else token("text_primary"))

    def navigate(self, anchor):
        self.update_idletasks()
        self.scroll.yview(anchor)
        self._highlight()

    def _tab(self, event, direction):
        if self.grab_current() is not self:
            return "break"
        nodes = [node for node in self.focus_nodes if node.winfo_viewable()]
        focused = self.focus_get()
        index = nodes.index(focused) if focused in nodes else -1 if direction > 0 else 0
        node = nodes[(index + direction) % len(nodes)]
        self.scroll.reveal_focus(node)
        node.focus_set()
        return "break"

    def _page(self, direction):
        if self.grab_current() is self:
            self.canvas.yview_scroll(direction, "pages")
        return "break"

    def _edge(self, position):
        if self.grab_current() is self:
            self.canvas.yview_moveto(position)
        return "break"

    def _wheel(self, event, direction=None):
        if self.grab_current() is self:
            self.scroll.yview_scroll(direction if direction is not None else (-1 if event.delta > 0 else 1), "units")
        return "break"

    def _escape(self, event=None):
        if self.grab_current() is self:
            self.close()
        return "break"

    def close(self):
        if self._closed:
            return
        # Only the top modal is dismissible. A window-manager event to an
        # obscured parent must not destroy its still-open child.
        if self._child_dialog is not None and self._child_dialog.winfo_exists():
            self._child_dialog._activate()
            return
        self._closed = True
        for callback in (self._activate_id, self._highlight_id):
            if callback is not None:
                self.after_cancel(callback)
        if self.grab_current() is self:
            self.grab_release()
        self.destroy()
        if self.parent_dialog.winfo_exists():
            if isinstance(self.parent_dialog, TrustDialog):
                self.parent_dialog.grab_set()
                self.parent_dialog.scroll.reveal_focus(self.opener)
                self.parent_dialog.update_idletasks()
            if self.opener is not None and self.opener.winfo_exists():
                self._restore_focus()

    def _restore_focus(self):
        if self.opener is not None and self.opener.winfo_exists():
            self.opener.focus_force()


def show_trust_dialog(parent):
    opener = getattr(parent, "trust_button", None)
    if opener is not None:
        opener = opener._canvas
    dialog = getattr(parent, "_trust_dialog", None)
    if dialog is None or not dialog.winfo_exists():
        dialog = TrustDialog(parent, opener=opener)
        parent._trust_dialog = dialog
    else:
        child = dialog._child_dialog
        (child if child is not None and child.winfo_exists() else dialog)._activate()
    return dialog


def show_security_details_dialog(parent):
    return parent.open_details() if isinstance(parent, TrustDialog) else TrustDialog(parent, detailed=True)
