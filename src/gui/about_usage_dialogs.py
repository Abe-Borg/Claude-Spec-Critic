"""Informational dialogs (workflow / usage / trust / security / about).

These windows are pure UI: long blocks of mostly-static text rendered in a
modal CTkToplevel. Model names and the code basis are rendered from config
(``api_config`` defaults via the pricing table's labels, and the selected
module's cycle) so the copy can't drift when a default model or module
changes. Keeping them out of gui.py preserves the GUI shell as a thin
layout-and-wiring file.

The trust dialog (:func:`show_trust_dialog`) is a plain-language account of
the anti-hallucination and verification machinery for engineers and
stakeholders. The copy and source ledger live in trust_content.py and
docs/TRUST_CLAIMS.md; these functions keep the existing help entry points.
"""
from __future__ import annotations

import webbrowser

import customtkinter as ctk

from .. import __version__
from ..core.api_config import (
    CROSS_CHECK_MODEL_DEFAULT,
    REVIEW_MODEL_DEFAULT,
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT,
)
from ..core.pricing import price_for
from ..modules import require_module
from ..programs import get_program
from .realtime_cost_gate import REALTIME_WORKER_TRADEOFF_TEXT
from .widgets import COLORS

_UI_FONT_SIZE = 12
_BATCH_TIMING_COPY = "provider turnaround varies, and local polling has a bounded wait (see Why Trust It)"

# Identity / licensing copy for the About dialog. Keep in sync with the
# LICENSE file and the README License section.
_AUTHOR_NAME = "Abraham Borg"
_COPYRIGHT_NOTICE = "Copyright © 2025–2026 Abraham Borg."
_LICENSE_NAME = "PolyForm Noncommercial License 1.0.0"
_LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0"
_LINKEDIN_URL = "https://www.linkedin.com/in/abrahamborg/"
_GITHUB_PROFILE_URL = "https://github.com/Abe-Borg"


def _model_label(model_id: str) -> str:
    """Human label for a model id, via the pricing table's display names.

    Rendering from config keeps the dialogs from drifting when a default
    model is bumped (the copy previously hardcoded model names and went
    stale). Unknown ids fall back to the raw id — still accurate, just
    less pretty.
    """
    price = price_for(model_id)
    return price.label if price else model_id


def _build_modal(parent, title: str, geometry: str = "620x640") -> ctk.CTkToplevel:
    dialog = ctk.CTkToplevel(parent)
    dialog.title(title)
    dialog.geometry(geometry)
    dialog.configure(fg_color=COLORS["bg_dark"])
    dialog.resizable(True, True)
    dialog.minsize(500, 500)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_force()
    return dialog


def _render_sections(scroll, sections: list[tuple[str, str]]) -> None:
    for title, body in sections:
        ctk.CTkLabel(
            scroll, text=title,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=8, pady=(10, 2))
        ctk.CTkLabel(
            scroll, text=body,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            text_color=COLORS["text_secondary"],
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 4))


def _link_label(scroll, url: str) -> None:
    """A clickable link rendered in the section-body style."""
    link = ctk.CTkLabel(
        scroll, text=url,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE, underline=True),
        text_color=COLORS["accent"], cursor="hand2",
    )
    link.pack(anchor="w", padx=8, pady=(0, 4))
    link.bind("<Button-1>", lambda _event: webbrowser.open(url))


def _action_link(parent, text: str, command) -> None:
    """Render a centered, keyboard-focusable link-style action."""
    ctk.CTkButton(
        parent, text=text, width=180, height=28,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE, underline=True),
        fg_color="transparent", hover_color=COLORS["bg_input"],
        text_color=COLORS["accent"], command=command,
    ).pack(pady=(0, 6))


def _grab_dialog(win) -> None:
    """Modal-grab a toplevel defensively.

    Grabbing before the window is viewable raises on some platforms, so
    callers schedule this via ``after(...)`` and we swallow the failure.
    """
    try:
        win.grab_set()
    except Exception:  # pragma: no cover - platform dependent
        pass


def show_realtime_cost_warning(app, *, on_keep=None, on_revert=None) -> None:
    """Warn once (dismissable) about real-time review's compounding cost.

    Shown when the operator switches Options into real-time mode, and — for
    the upgrade path where real-time is already persisted so the toggle never
    fires — again just before a live run starts. Real-time forfeits the 50%
    Batch API discount **and** runs verification live too, so the total spend
    compounds well past a simple doubling — a surprise the one-line Options
    hint under-sells. Live requests avoid the batch queue, but turnaround
    depends on the inputs, retries and provider availability.

    **Keep Real-time** runs ``on_keep``; **Use Batch instead** (and closing
    the window) runs ``on_revert``. Both default to
    ``app._apply_transport_choice`` (the Options-toggle behavior); the
    run-start gate passes callbacks that proceed with / abort the pending run
    instead. Either dismissal sets ``app._realtime_cost_warning_shown_this_session``
    so a user already warned this session isn't warned twice, and a ticked
    "Don't show this again" checkbox persists the suppression via ``ui_state``.
    """
    from ..core.ui_state import save_suppress_realtime_cost_warning

    existing = getattr(app, "_realtime_cost_dialog", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.focus_force()
        return

    win = ctk.CTkToplevel(app)
    app._realtime_cost_dialog = win
    win.title("Real-time review — cost warning")
    win.configure(fg_color=COLORS["bg_dark"])
    win.geometry("560x490")
    win.minsize(460, 420)
    win.transient(app)
    # Grab deferred: grabbing before the window is viewable raises on some
    # platforms (the update dialog learned this the hard way).
    win.after(150, lambda: _grab_dialog(win))

    suppress_var = ctk.BooleanVar(value=False)

    def _finish(realtime: bool) -> None:
        if suppress_var.get():
            save_suppress_realtime_cost_warning(True)
        app._realtime_cost_warning_shown_this_session = True
        app._realtime_cost_dialog = None
        try:
            win.grab_release()
        except Exception:  # pragma: no cover - platform dependent
            pass
        win.destroy()
        # Callbacks run after teardown so a re-entrant run-start sees a clean
        # dialog handle. Default to the Options-toggle transport commit.
        if realtime:
            (on_keep or (lambda: app._apply_transport_choice(True)))()
        else:
            (on_revert or (lambda: app._apply_transport_choice(False)))()

    # Closing the window is the same as declining: fall back to batch.
    win.protocol("WM_DELETE_WINDOW", lambda: _finish(False))

    card = ctk.CTkFrame(win, fg_color=COLORS["bg_card"], corner_radius=8)
    card.pack(fill="both", expand=True, padx=12, pady=12)

    ctk.CTkLabel(
        card, text="Real-time review costs much more",
        font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
        text_color=COLORS["text_primary"], justify="left",
    ).pack(anchor="w", padx=18, pady=(16, 2))
    ctk.CTkLabel(
        card,
        text=(
            "Real-time (streaming) review bills at standard API pricing — it "
            "forfeits the 50% Batch API discount. And because verification "
            "runs live too, the extra cost compounds across every phase, so a "
            "real-time run can cost several times what the same review costs "
            "in batch mode.\n\n"
            "Live requests avoid the batch queue. Turnaround still depends on "
            "your inputs, provider availability and retries; there is no "
            "guaranteed completion time.\n\n"
            "The worker selector controls how many spec reviews run at once. "
            f"{REALTIME_WORKER_TRADEOFF_TEXT} The planned spec reviews "
            "themselves stay the same.\n\n"
            "Very large specs (≥200k input tokens) still require batch mode."
        ),
        font=ctk.CTkFont(family="Segoe UI", size=12),
        text_color=COLORS["text_secondary"], wraplength=500, justify="left",
    ).pack(anchor="w", padx=18, pady=(0, 10))

    # Bottom button bar first so pack reserves the bottom edge.
    bottom = ctk.CTkFrame(card, fg_color="transparent")
    bottom.pack(side="bottom", fill="x", padx=18, pady=(4, 14))
    ctk.CTkButton(
        bottom, text="Keep Real-time", width=150, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=lambda: _finish(True),
    ).pack(side="right")
    ctk.CTkButton(
        bottom, text="Use Batch instead", width=150, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_secondary"],
        command=lambda: _finish(False),
    ).pack(side="right", padx=(0, 8))

    ctk.CTkCheckBox(
        card, text="Don't show this again", variable=suppress_var,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        border_color=COLORS["border"], checkmark_color=COLORS["text_primary"],
        text_color=COLORS["text_secondary"],
        checkbox_width=20, checkbox_height=20,
    ).pack(side="bottom", anchor="w", padx=18, pady=(0, 4))


def show_about_dialog(parent) -> None:
    dialog = _build_modal(parent, "How Spec Critic Works")

    outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    outer.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        outer, text="How Spec Critic Works",
        font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=20, pady=(20, 4))

    program = get_program(
        getattr(parent, "_selected_program_id", None)
        or getattr(parent, "_selected_module_id", None)
    )
    modules = [require_module(module_id) for module_id in program.implemented_module_ids]
    ctk.CTkLabel(
        outer,
        text=f"AI-assisted specification review — {program.display_name}",
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_muted"],
    ).pack(anchor="w", padx=20, pady=(0, 12))

    scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    code_basis = "; ".join(
        f"{module.display_name}: {', '.join(code.name for code in module.cycle.base_codes)}"
        for module in modules
    )
    review_label = _model_label(REVIEW_MODEL_DEFAULT)
    verifier_label = _model_label(VERIFICATION_MODEL_DEFAULT)
    escalation_label = _model_label(VERIFICATION_ESCALATION_MODEL)
    cross_check_label = _model_label(CROSS_CHECK_MODEL_DEFAULT)

    sections = [
        ("1.  Text Extraction", (
            "Your .docx files are read locally. Paragraphs, tables, text boxes, "
            "footnotes/endnotes, and headers/footers are extracted — nothing is "
            "sent to Claude yet."
        )),
        ("2.  Local Pre-Screening", (
            "Before any API calls, deterministic detectors scan each spec for "
            "LEED references inappropriate for the project, unresolved placeholders "
            "(like [SELECT] or [VERIFY]), template markers (TODO / FIXME / XXX / "
            "lorem ipsum), stale code-cycle references, invalid cycles (year/code "
            "combinations that aren’t real, like “2018 CBC”), empty sections, "
            "duplicate headings, duplicate paragraphs, and CSI-number / filename "
            "mismatches. These alerts are flagged locally and don’t cost any tokens."
        )),
        ("3.  Location & Client Research  (module-dependent)", (
            "Programs with location-sensitive reviewers (including the data-center "
            "architecture, fire suppression, electrical, and fire detection/alarm "
            "modules) ask for the project's city, state/province, "
            "and client before the run. A research pass then fans out one "
            "bounded research task per topic — governing codes, AHJ requirements, "
            "client standards, site environment — and builds a grounded "
            "requirements profile used by applicable later phases. Modules "
            "without this capability (like the California K-12 module) skip "
            "this step entirely."
        )),
        ("4.  Per-Spec Review", (
            f"Each specification is sent individually to Claude {review_label}. "
            f"Claude checks for code compliance issues against the assigned module's "
            f"code basis ({code_basis}), jurisdiction-specific requirements, "
            "outdated standards, coordination problems, and constructability "
            "concerns. Each finding is assigned a severity (Critical, High, "
            "Medium, or Gripe) and a confidence score."
        )),
        ("5.  Deduplication", (
            "When the same issue appears across multiple specs — like an outdated "
            "seismic code reference — duplicates are consolidated into a single "
            "finding that lists all affected files. Per-file edit occurrences are "
            "preserved internally so multi-file edits can target every affected spec."
        )),
        ("6.  Verification", (
            "Every finding that needs external grounding is checked in a secondary AI "
            f"pass with web search. The default verifier is Claude {verifier_label} "
            f"(faster and cheaper); {escalation_label} is used as an escalation model "
            "for Critical/High findings the first pass couldn’t ground (Unverified "
            "or no usable web evidence). Verdicts are Confirmed, Corrected, Disputed, or "
            "Unverified — a verdict cannot be marked Confirmed or Corrected unless the "
            "model’s cited URL matches retrieved search/fetch evidence, so model-"
            "invented citations are stripped and the finding is downgraded. Internal-only "
            "issues (placeholders, duplicates, internal contradictions, LEED, template "
            "markers) are resolved locally without web search and reported as Locally "
            "classified; AI triage can also assign that status. Source matches do not prove "
            "the source supports the claim. This is not a substitute for your review."
        )),
        ("7.  Cross-Spec Coordination  (optional)", (
            f"If enabled, {cross_check_label} compares spec text within each assigned "
            "module and its size-limited chunks. It looks for "
            "contradictions between specs, missing cross-references, scope gaps and "
            "overlaps, inconsistent equipment data, and division-of-work conflicts. "
            "Large projects are chunked by CSI division within each assigned module "
            "and merged. Cross-check runs after verification so it can use verified "
            "verdicts as context (Disputed review findings are filtered out of the "
            "“already identified” list it sees). Any coordination findings it produces "
            "are then put through their own verification pass before the report is "
            "exported."
        )),
        ("8.  Local-Code Compliance  (module-dependent)", (
            "When a location-aware module built a requirements profile in step 3, "
            "a compliance pass checks the whole spec package against each grounded "
            "requirement — represented, contradicted, unclear, or missing — and "
            "the report gains a Jurisdiction & Client Requirements section with a "
            "coverage matrix. A requirement is called missing only when every part "
            "of the package was assessed; one the pass did not assess everywhere is "
            "marked Not assessed, the report says the coverage is incomplete, and "
            "an addition that depends on it is shown as report only. Compliance "
            "findings also go through verification."
        )),
        ("9.  Edit Instruction Labels", (
            "Each finding is labeled in the report as Edit suggested or Report "
            "only. Edit suggested means the model proposed a concrete text "
            "change (existing text → replacement); Report only means the finding "
            "has no clean textual fix. Spec Critic emits these suggestions but "
            "never applies them — applying edits is left to a separate tool."
        )),
        ("10.  Output", (
            "Results can be viewed in-app or exported as a Word report. Alongside the "
            "report, Spec Critic writes a machine-readable JSON sidecar listing every "
            "suggested edit (existing text and proposed replacement, once for each "
            "place it applies) for ingestion by a separate editing tool. Export "
            "writes your chosen path; use a different filename from your inputs "
            "and preserve originals. There is no source-path guard."
        )),
    ]

    _render_sections(scroll, sections)

    ctk.CTkLabel(
        scroll, text="What it doesn’t do",
        font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=8, pady=(14, 2))
    ctk.CTkLabel(
        scroll,
        text=(
            "Spec Critic reads inputs for review. It produces a report and a JSON "
            "list of suggested edits; export can overwrite your chosen file, and "
            "applying them is left to a separate tool. "
            "It’s advisory only and not a substitute for AHJ review. Code "
            "citations should still be spot-checked by the engineer of record."
        ),
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_secondary"],
        wraplength=520, justify="left",
    ).pack(anchor="w", padx=8, pady=(0, 10))

    ctk.CTkButton(
        outer, text="Close", width=100, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=dialog.destroy,
    ).pack(pady=(0, 16))


def show_usage_dialog(parent) -> None:
    dialog = _build_modal(parent, "How to Use Spec Critic")

    outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    outer.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        outer, text="How to Use Spec Critic",
        font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=20, pady=(20, 4))

    ctk.CTkLabel(
        outer, text="Step-by-step guide to running a specification review",
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_muted"],
    ).pack(anchor="w", padx=20, pady=(0, 12))

    scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    sections = [
        ("1.  Enter Your API Key", (
            "Paste your Anthropic API key into the API Key field. Runs capture it "
            "in memory; the app does not save a typed key. Startup prefers the "
            "OS keyring, then optional plaintext spec_critic_api_key.txt files, "
            "then ANTHROPIC_API_KEY. Protect plaintext files and read Why Trust It "
            "before entering private material."
        )),
        ("2.  Choose a Review Program", (
            "Pick the review program in the header. The default is California "
            "K-12 DSA mechanical/plumbing. Hyperscale Data Centers is one visible "
            "choice: after extraction, it routes each specification to the "
            "Architecture, Fire Suppression, Electrical, and Fire Detection & Alarm "
            "modules, a justified combination, or an explicit unsupported "
            "coverage gap. It also asks for the project's city, state/province, "
            "country, and client so each assigned module can research "
            "location-specific requirements before the review. Double-check "
            "the location spelling — it guides requirements research and the "
            "verification cache. Confirm the echoed location before review submission; "
            "counting or separately started drawing work may already have run."
        )),
        ("3.  Select Specification Files", (
            "Click Browse and select one or more .docx specification files. "
            "The tool will extract text and analyze token usage. The token "
            "gauge shows the input size of the largest spec's review request "
            "(a local count, then Anthropic's estimate where available) against the per-spec "
            "input limit — if a spec is too large, it will be flagged, and "
            "the review refuses an oversized request. Provider counting sends the "
            "constructed text/context request before you click Submit Batch."
        )),
        ("4.  Add Project Context (Optional)", (
            "Describe your project in the Project Context field — things "
            "like building type, square footage, number of stories, or "
            "any special conditions. You can also attach files (.docx, .pdf, "
            ".md, .txt) whose text is merged into the context. This context "
            "is supplied to applicable review and follow-up requests; each stage sends "
            "its relevant fields. Drawing preflight sends a PDF chunk before cost "
            "confirmation. Review the merged context before starting. "
            "Click Expand for a larger editing area."
        )),
        ("5.  Batch Processing (Default) or Real-Time", (
            "By default, all specs are queued and processed through the Batch "
            f"API on Claude {_model_label(REVIEW_MODEL_DEFAULT)} at 50% cost "
            f"savings. Batch turnaround is slower — {_BATCH_TIMING_COPY}. "
            "Check “Real-time review (streaming)” in "
            "Options to stream reviews synchronously instead; "
            "verification runs live too (no batch queues anywhere). The trade "
            "is cost — real-time forfeits the 50% batch discount and, because "
            "verification also runs live, the spend compounds across every "
            "phase, so a run can cost several times its batch price. It also "
            "has no crash resume: if the app closes mid-run, in-flight review "
            "work is lost. Very large specs (≥200k input tokens) still require "
            "batch mode."
        )),
        ("6.  Enable Cross-Spec Coordination (Optional)", (
            "Check this option to run a separate coordination analysis that "
            "compares spec text within each module and its size-limited chunks. It looks for "
            "contradictions between specs, missing cross-references, and "
            "scope gaps that per-spec review cannot detect. Large projects are "
            "automatically chunked by module rules when requests are too large. "
            "Conflicts across modules or separate chunks can be missed."
        )),
        ("7.  Run the Review", (
            "Click Submit Batch (labeled Start Review (live) in real-time mode). "
            "The activity log shows progress. The batch runs on Anthropic's "
            "servers (up to ~24h), so you can close the app or lose your "
            "connection without losing the work — the batch is saved, and on "
            "next launch you'll be prompted to resume polling and finish the "
            "run. You can also recover a batch from a terminal with "
            "scripts/recover_batch.py. If the review repair batch (the "
            "automatic re-run of specs whose first review failed) is still "
            "running when the results come in, the report is marked "
            "provisional and the run stays saved: resume it later and "
            "verification and coordination run then, without paying for the "
            "repair again."
        )),
        ("8.  Save the Report", (
            "When the review completes, you'll be prompted to save a formatted "
            ".docx report. Spec Critic also writes a JSON sidecar next to it "
            "listing the suggested edits (existing text and proposed replacement, "
            "once for each place an edit applies) for use by a separate editing "
            "tool. Choose a separate report filename: export does not guard "
            "against overwriting an input, and same-stem sidecars are replaced."
        )),
        ("9.  Review the Results", (
            "Findings are grouped by severity (Critical, High, Medium, "
            "Gripe) and sorted by confidence within each severity tier. Each finding "
            "includes a verification verdict from a secondary AI pass with "
            "web search, and shows whether the verdict was externally grounded "
            "or escalated to Opus. The Run Diagnostics banner at the top of the "
            "report flags operational problems — specs that failed review, "
            "verification failures, budget-exhausted findings, cross-check "
            "chunks that weren't analyzed. Open the Diagnostics window to see "
            "model usage, prompt-cache hits, token counts by phase, "
            "verification evidence stats, and suggested-edit counts."
        )),
    ]

    _render_sections(scroll, sections)

    ctk.CTkLabel(
        scroll, text="Tips",
        font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=8, pady=(14, 2))
    ctk.CTkLabel(
        scroll,
        text=(
            "Use batch mode for routine reviews — same review logic at "
            "lower cost, with slower turnaround. Switch on Real-time review "
            "when you need results now — identical prompts and findings "
            "logic at standard price. Save your API key to a file so you don't "
            "have to paste it every time. Write specific project context — "
            "the more detail you provide, the more targeted the findings. "
            "Always spot-check code citations against the actual code text "
            "before acting on findings."
        ),
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_secondary"],
        wraplength=520, justify="left",
    ).pack(anchor="w", padx=8, pady=(0, 10))

    ctk.CTkButton(
        outer, text="Close", width=100, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=dialog.destroy,
    ).pack(pady=(0, 16))


def show_trust_dialog(parent):
    """Open the source-backed short topic at the existing help entry."""
    from .trust_dialogs import show_trust_dialog as show
    return show(parent)


def show_security_details_dialog(parent):
    """Open the stacked, source-backed dossier."""
    from .trust_dialogs import show_security_details_dialog as show
    return show(parent)


def show_license_dialog(parent) -> None:
    """The "About" dialog: version, copyright, license terms, and contact."""
    dialog = _build_modal(parent, "About Spec Critic", geometry="560x520")

    outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    outer.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        outer, text="About Spec Critic",
        font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=20, pady=(20, 4))

    ctk.CTkLabel(
        outer, text=f"Version {__version__}",
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_muted"],
    ).pack(anchor="w", padx=20, pady=(0, 12))

    scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    _render_sections(scroll, [
        ("Copyright", _COPYRIGHT_NOTICE),
        ("License", (
            f"Spec Critic is licensed under the {_LICENSE_NAME}. You may "
            "use, copy, modify, and share it for any noncommercial purpose — "
            "personal use, study, research, hobby projects, and use by "
            "charitable, educational, or government organizations. Commercial "
            "use requires the copyright holder’s prior written permission. "
            "The full terms ship with the software in the LICENSE file and "
            "are published at:"
        )),
    ])
    _link_label(scroll, _LICENSE_URL)

    _render_sections(scroll, [
        ("Author", (
            f"Created by {_AUTHOR_NAME}. Questions, feedback, or commercial "
            "licensing inquiries — connect on LinkedIn:"
        )),
    ])
    _link_label(scroll, _LINKEDIN_URL)

    _render_sections(scroll, [
        ("GitHub", "More projects and source code:"),
    ])
    _link_label(scroll, _GITHUB_PROFILE_URL)

    ctk.CTkButton(
        outer, text="Close", width=100, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=dialog.destroy,
    ).pack(pady=(0, 16))
