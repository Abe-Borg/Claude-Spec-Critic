"""Self-update UI: footer, daily auto-check, update dialog, download flow.

Bridges the pure updater module (``src/core/updates.py``) and the GUI shell,
following the controller pattern: every function takes ``app`` (the
``SpecReviewApp``) as its first argument, network/disk work runs on daemon
threads, and every tkinter mutation is marshaled back with ``app.after(0,
lambda ...)`` using default-argument capture.

Lifecycle guards (learned on the sibling Drawing Analyzer app):

- Only one update *check* and one *download* run at a time
  (``app._update_checking`` / ``app._update_downloading``).
- Closing the dialog mid-download cancels the download's completion handling
  (``app._update_download_cancelled``) so a dismissed dialog can never pop a
  stray "install & quit" prompt later. The daemon worker itself can't be
  killed; its result is simply ignored and the verified file stays cached in
  ``~/.spec_critic/updates`` for the next offer.
- "Download & Install" is refused while a review run is in flight
  (``app.is_processing``) — the installer would close the app mid-run.
"""
from __future__ import annotations

import threading
import webbrowser
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from .. import __version__
from ..core import updates
from .widgets import COLORS


def init_update_state(app) -> None:
    """Initialise the updater's app-level state. Called from ``__init__``."""
    app._update_state_path = updates.default_state_path()
    app._update_checking = False
    app._update_downloading = False
    app._update_download_cancelled = False
    app._update_dialog = None


def build_footer(app, parent) -> None:
    """A slim footer: version at left, an update-status note, and a button.

    Packed ``side="bottom"`` into the container *before* any content so the
    version + "Check for Updates" strip reserves the bottom edge before the
    log claims the remaining space with ``expand=True``.
    """
    footer = ctk.CTkFrame(parent, fg_color="transparent")
    footer.pack(side="bottom", fill="x", pady=(8, 0))
    ctk.CTkLabel(
        footer, text=f"v{__version__}",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color=COLORS["text_muted"],
    ).pack(side="left")
    app.update_status_label = ctk.CTkLabel(
        footer, text="",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color=COLORS["text_muted"],
    )
    app.update_status_label.pack(side="left", padx=(10, 0))
    app.check_update_btn = ctk.CTkButton(
        footer, text="Check for Updates", width=150, height=28,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_secondary"],
        command=app._on_check_for_updates_clicked,
    )
    app.check_update_btn.pack(side="right")


def set_update_status(app, text: str, *, color: str | None = None) -> None:
    label = getattr(app, "update_status_label", None)
    if label is not None:
        try:
            label.configure(text=text, text_color=color or COLORS["text_muted"])
        except Exception:  # pragma: no cover - defensive UI update
            pass


def maybe_auto_check_for_updates(app) -> None:
    """Launch a silent, throttled update check (once/day). Never nags.

    Only surfaces a dialog if an update is available and the user has not
    chosen to skip that version; a clean or failed check stays silent.
    Windows-only: the release asset is a Windows installer, so a source run
    on macOS / Linux is never nagged about an update it cannot apply (the
    manual footer button still works there and points at the releases page).
    """
    try:
        if not updates.installer_platform_supported():
            return
        if updates.update_check_disabled():
            return
        state = updates.load_state(app._update_state_path)
        if not updates.should_auto_check(state, now=datetime.now()):
            return
    except Exception:  # noqa: BLE001 - startup convenience, never fatal
        return
    start_update_check(app, manual=False)


def on_check_for_updates_clicked(app) -> None:
    """The footer button: an explicit check that always reports its result."""
    start_update_check(app, manual=True)


def start_update_check(app, *, manual: bool) -> None:
    if app._update_checking:
        return
    app._update_checking = True
    if manual:
        try:
            app.check_update_btn.configure(state="disabled", text="Checking…")
        except Exception:  # pragma: no cover - defensive UI update
            pass
        set_update_status(app, "Checking for updates…")
    threading.Thread(
        target=_update_check_worker, args=(app, manual), daemon=True
    ).start()


def _update_check_worker(app, manual: bool) -> None:
    """Fetch + compare off the UI thread; marshal the result back via after()."""
    result = updates.check_for_update(__version__)
    # Record the check time regardless of outcome so the daily throttle holds.
    try:
        state = updates.load_state(app._update_state_path)
        updates.record_check(state, now=datetime.now())
        updates.save_state(app._update_state_path, state)
    except Exception:  # noqa: BLE001 - best-effort state write
        pass
    app.after(0, lambda: on_update_check_done(app, result, manual))


def on_update_check_done(app, result, manual: bool) -> None:
    app._update_checking = False
    try:
        app.check_update_btn.configure(state="normal", text="Check for Updates")
    except Exception:  # pragma: no cover - defensive UI update
        pass

    if result.status == updates.STATUS_UPDATE_AVAILABLE and result.info is not None:
        info = result.info
        set_update_status(
            app, f"Update available: v{info.version}", color=COLORS["accent_glow"]
        )
        if not updates.installer_platform_supported():
            # The release asset is a Windows installer — don't offer a
            # download this platform can't run. A manual check still reports
            # the news and points at the releases page.
            if manual:
                messagebox.showinfo(
                    "Update available",
                    f"Version {info.version} is available, but the packaged "
                    "update is a Windows installer and can't be applied to "
                    "this install.\n\n"
                    f"See {updates.releases_page_url()} for the release.",
                )
            return
        skipped = False
        if not manual:
            try:
                state = updates.load_state(app._update_state_path)
                skipped = updates.version_is_skipped(state, info.version)
            except Exception:  # noqa: BLE001
                skipped = False
        if manual or not skipped:
            show_update_dialog(app, info)
        return

    if result.status == updates.STATUS_UP_TO_DATE:
        set_update_status(app, "You're up to date.")
        if manual:
            messagebox.showinfo(
                "Up to date",
                f"You're running the latest version (v{__version__}).",
            )
        return

    if result.status == updates.STATUS_DISABLED:
        if manual:
            messagebox.showinfo(
                "Update checks are off",
                "Automatic update checks are disabled by the "
                "SPEC_CRITIC_DISABLE_UPDATE_CHECK environment variable.",
            )
        return

    # STATUS_ERROR
    set_update_status(app, "Update check failed.")
    if manual:
        messagebox.showwarning(
            "Couldn't check for updates",
            "Could not reach the update service.\n\n"
            f"{result.error or 'Unknown error.'}\n\n"
            "You can download the latest version manually from the "
            "releases page.",
        )


def show_update_dialog(app, info) -> None:
    """A styled dialog offering to download + install ``info`` (or skip / defer)."""
    existing = getattr(app, "_update_dialog", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.focus_force()
        return

    win = ctk.CTkToplevel(app)
    app._update_dialog = win
    win.title("Update available")
    win.configure(fg_color=COLORS["bg_dark"])
    win.geometry("560x480")
    win.minsize(460, 380)
    win.transient(app)
    win.protocol("WM_DELETE_WINDOW", app._close_update_dialog)
    # Grab deferred: grabbing before the window is viewable raises on some
    # platforms (the sibling app learned this the hard way).
    win.after(150, lambda: _grab_dialog(win))

    card = ctk.CTkFrame(win, fg_color=COLORS["bg_card"], corner_radius=8)
    card.pack(fill="both", expand=True, padx=12, pady=12)

    ctk.CTkLabel(
        card, text=f"Version {info.version} is available",
        font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
        text_color=COLORS["text_primary"], justify="left",
    ).pack(anchor="w", padx=18, pady=(16, 2))
    ctk.CTkLabel(
        card,
        text=(
            f"You have v{__version__}. Update to get the latest fixes and "
            "improvements. The app will close so the installer can replace it."
        ),
        font=ctk.CTkFont(family="Segoe UI", size=12),
        text_color=COLORS["text_secondary"], wraplength=490, justify="left",
    ).pack(anchor="w", padx=18, pady=(0, 8))

    # Bottom button bar first so pack reserves the bottom edge.
    bottom = ctk.CTkFrame(card, fg_color="transparent")
    bottom.pack(side="bottom", fill="x", padx=18, pady=(4, 14))
    app._update_download_btn = ctk.CTkButton(
        bottom, text="Download & Install", width=170, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=lambda: start_update_download(app, info),
    )
    app._update_download_btn.pack(side="right")
    app._update_later_btn = ctk.CTkButton(
        bottom, text="Later", width=80, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_secondary"],
        command=app._close_update_dialog,
    )
    app._update_later_btn.pack(side="right", padx=(0, 8))
    app._update_skip_btn = ctk.CTkButton(
        bottom, text="Skip this Version", width=140, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_muted"],
        command=lambda: skip_update_version(app, info),
    )
    app._update_skip_btn.pack(side="left")

    # Progress row — created now, packed only once a download starts.
    app._update_progress = ctk.CTkProgressBar(card, height=10)
    app._update_progress.set(0)
    app._update_progress_status = ctk.CTkLabel(
        card, text="",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color=COLORS["text_muted"],
    )

    ctk.CTkLabel(
        card, text="What's new",
        font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        text_color=COLORS["accent_glow"],
    ).pack(anchor="w", padx=18, pady=(6, 2))
    notes = (info.notes or "").strip() or "No release notes were provided."
    notes_box = ctk.CTkTextbox(
        card, fg_color=COLORS["bg_dark"], text_color=COLORS["text_secondary"],
        wrap="word", height=150,
        font=ctk.CTkFont(family="Segoe UI", size=12),
    )
    notes_box.pack(fill="both", expand=True, padx=18, pady=(0, 6))
    notes_box.insert("1.0", notes)
    notes_box.configure(state="disabled")

    link = ctk.CTkLabel(
        card, text="View this release on GitHub",
        font=ctk.CTkFont(family="Segoe UI", size=11, underline=True),
        text_color=COLORS["accent_glow"], cursor="hand2",
    )
    link.pack(anchor="w", padx=18, pady=(0, 4))
    link.bind("<Button-1>", lambda _e: open_releases_page(app))


def _grab_dialog(win) -> None:
    try:
        win.grab_set()
    except Exception:  # pragma: no cover - platform dependent
        pass


def start_update_download(app, info) -> None:
    if app._update_downloading:
        # A download is already running (e.g. the dialog was closed and
        # reopened). Don't start a second writer to the same file.
        set_update_status(app, "A download is already in progress…")
        return
    if getattr(app, "is_processing", False):
        messagebox.showinfo(
            "Review in progress",
            "Please wait for the current review to finish before updating.",
        )
        return
    app._update_downloading = True
    app._update_download_cancelled = False
    for name in ("_update_download_btn", "_update_skip_btn", "_update_later_btn"):
        widget = getattr(app, name, None)
        if widget is not None:
            try:
                widget.configure(state="disabled")
            except Exception:  # pragma: no cover - defensive UI update
                pass
    try:
        app._update_progress_status.pack(side="bottom", fill="x", padx=18, pady=(0, 2))
        app._update_progress.pack(side="bottom", fill="x", padx=18, pady=(2, 4))
        app._update_progress.set(0)
        app._update_progress_status.configure(text="Starting download…")
    except Exception:  # pragma: no cover - defensive UI update
        pass
    threading.Thread(
        target=_update_download_worker, args=(app, info), daemon=True
    ).start()


def _update_download_worker(app, info) -> None:
    try:
        dest_dir = updates.default_download_dir()

        def _progress(done: int, total: int) -> None:
            app.after(0, lambda d=done, t=total: on_update_download_progress(app, d, t))

        path = updates.download_installer(info, dest_dir, progress=_progress)
    except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
        app.after(0, lambda e=str(exc): on_update_download_error(app, e))
        return
    app.after(0, lambda p=path: on_update_download_done(app, p))


def on_update_download_progress(app, done: int, total: int) -> None:
    mb = 1024 * 1024
    try:
        if total > 0:
            app._update_progress.set(min(1.0, done / total))
            app._update_progress_status.configure(
                text=f"Downloading… {done // mb} / {total // mb} MB"
            )
        else:
            app._update_progress_status.configure(
                text=f"Downloading… {done // mb} MB"
            )
    except Exception:  # pragma: no cover - defensive UI update
        pass


def on_update_download_done(app, path) -> None:
    app._update_downloading = False
    if app._update_download_cancelled or app._update_dialog is None:
        # The user dismissed the update dialog while the download was in
        # flight — respect that and don't pop a surprise install prompt. The
        # verified file stays cached; the next check will offer it again.
        set_update_status(app, "Update downloaded — install it later.")
        return
    try:
        app._update_progress.set(1.0)
        app._update_progress_status.configure(text="Download verified.")
    except Exception:  # pragma: no cover - defensive UI update
        pass
    proceed = messagebox.askyesno(
        "Install update",
        "The update downloaded and passed its integrity check.\n\n"
        "Spec Critic will now close so the installer can replace it. "
        "Continue?",
    )
    if not proceed:
        reset_update_dialog_buttons(app)
        set_update_status(app, "Update downloaded (not installed).")
        return
    try:
        updates.spawn_installer(path)
    except Exception as exc:  # noqa: BLE001
        messagebox.showerror(
            "Couldn't start the installer",
            f"The installer was saved to:\n\n{path}\n\n"
            f"but could not be launched automatically ({exc}).\n"
            "You can run it manually.",
        )
        reset_update_dialog_buttons(app)
        return
    # Release the modal grab / tear down the dialog, then exit so the
    # installer can replace the running files (installer.iss sets
    # CloseApplications=yes to close any lingering handle on them).
    close_update_dialog(app)
    app.quit()


def on_update_download_error(app, message: str) -> None:
    app._update_downloading = False
    if app._update_download_cancelled or app._update_dialog is None:
        # Dialog was dismissed mid-download; fail quietly.
        set_update_status(app, "Update download cancelled.")
        return
    reset_update_dialog_buttons(app)
    set_update_status(app, "Update download failed.")
    messagebox.showerror(
        "Download failed",
        f"The update could not be downloaded or verified:\n\n{message}\n\n"
        "You can download it manually from the releases page.",
    )


def reset_update_dialog_buttons(app) -> None:
    for name in ("_update_download_btn", "_update_skip_btn", "_update_later_btn"):
        widget = getattr(app, name, None)
        if widget is not None:
            try:
                widget.configure(state="normal")
            except Exception:  # pragma: no cover - defensive UI update
                pass
    for name in ("_update_progress", "_update_progress_status"):
        widget = getattr(app, name, None)
        if widget is not None:
            try:
                widget.pack_forget()
            except Exception:  # pragma: no cover - defensive UI update
                pass


def skip_update_version(app, info) -> None:
    try:
        state = updates.load_state(app._update_state_path)
        updates.mark_skipped(state, info.version)
        updates.save_state(app._update_state_path, state)
    except Exception:  # noqa: BLE001 - best-effort state write
        pass
    set_update_status(app, f"Skipped v{info.version}.")
    close_update_dialog(app)


def open_releases_page(app) -> None:
    try:
        webbrowser.open(updates.releases_page_url())
    except Exception:  # pragma: no cover - best-effort external opener
        pass


def close_update_dialog(app) -> None:
    if app._update_downloading:
        # Cancel the in-flight download's completion handling so it can't
        # pop a surprise install prompt after the dialog is dismissed.
        app._update_download_cancelled = True
    win = getattr(app, "_update_dialog", None)
    app._update_dialog = None
    if win is None:
        return
    try:
        win.grab_release()
    except Exception:  # pragma: no cover - platform dependent
        pass
    try:
        win.destroy()
    except Exception:  # pragma: no cover - platform dependent
        pass
