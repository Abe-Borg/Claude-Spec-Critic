"""Informational dialogs ("How It Works" / "How to Use").

These two windows are pure UI: long blocks of mostly-static text rendered
in a modal CTkToplevel. Model names and the code basis are rendered from
config (``api_config`` defaults via the pricing table's labels, and the
selected module's cycle) so the copy can't drift when a default model or
module changes. Keeping them out of gui.py preserves the GUI shell as a
thin layout-and-wiring file.
"""
from __future__ import annotations

import customtkinter as ctk

from ..core.api_config import (
    CROSS_CHECK_MODEL_DEFAULT,
    REVIEW_MODEL_DEFAULT,
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT,
)
from ..core.pricing import price_for
from ..modules import get_module
from .widgets import COLORS

_UI_FONT_SIZE = 12
_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"


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


def show_about_dialog(parent) -> None:
    dialog = _build_modal(parent, "How Spec Critic Works")

    outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    outer.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        outer, text="How Spec Critic Works",
        font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=20, pady=(20, 4))

    module = get_module(getattr(parent, "_selected_module_id", None))
    ctk.CTkLabel(
        outer,
        text=f"AI-assisted specification review — {module.display_name}",
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_muted"],
    ).pack(anchor="w", padx=20, pady=(0, 12))

    scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    code_basis = ", ".join(bc.name for bc in module.cycle.base_codes)
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
            "Modules that review location-sensitive work (like the data-center "
            "fire-suppression module) ask for the project's city, state/province, "
            "and client before the run. A research pass then fans out one "
            "web-search call per topic — governing codes, AHJ requirements, "
            "client standards, site environment — and builds a grounded "
            "requirements profile that every later phase can see. Modules "
            "without this capability (like the California K-12 module) skip "
            "this step entirely."
        )),
        ("4.  Per-Spec Review", (
            f"Each specification is sent individually to Claude {review_label}. "
            f"Claude checks for code compliance issues against the module's "
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
            "model’s cited URL actually appears in the web_search results, so model-"
            "invented citations are stripped and the finding is downgraded. Internal-only "
            "issues (placeholders, duplicates, internal contradictions, LEED, template "
            "markers) are resolved locally without web search and reported as Locally "
            "classified. This is an AI-assisted check, not a substitute for engineer review."
        )),
        ("7.  Cross-Spec Coordination  (optional)", (
            f"If enabled, a separate {cross_check_label} call analyzes the full text of "
            "all your specs together using the 1M token context window. It catches "
            "contradictions between specs, missing cross-references, scope gaps and "
            "overlaps, inconsistent equipment data, and division-of-work conflicts. "
            "Large projects are chunked by CSI division (per the module's chunk map) "
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
            "coverage matrix. Compliance findings also go through verification."
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
            "suggested edit (existing text and proposed replacement per finding) for "
            "ingestion by a separate editing tool. Spec Critic never modifies your "
            "source files."
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
            "Spec Critic is a review assistant — it never modifies your source "
            "documents. It produces a report and a JSON list of suggested edits; "
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
            "Paste your Anthropic API key (starts with sk-ant-...) into the "
            "API Key field. The key is used for all Claude API calls during "
            "the review. You can also save it to a file named "
            "'spec_critic_api_key.txt' next to the application — it will "
            "be loaded automatically on startup."
        )),
        ("2.  Choose a Review Module", (
            "Pick the review module in the header — one validated domain "
            "configuration (jurisdiction, code basis, prompts, detectors). "
            "The default is California K-12 DSA mechanical/plumbing; the "
            "data-center fire-suppression module additionally asks for the "
            "project's city, state/province, and client so it can research "
            "location-specific requirements before the review. Double-check "
            "the location spelling — it steers every web search and the "
            "verification cache, and the run echoes the parsed location back "
            "before anything is billed."
        )),
        ("3.  Select Specification Files", (
            "Click Browse and select one or more .docx specification files. "
            "The tool will extract text and analyze token usage. The token "
            "gauge shows the largest single spec's estimated API call size "
            "against the per-call limit — if a spec is too large, it will "
            "be flagged."
        )),
        ("4.  Add Project Context (Optional)", (
            "Describe your project in the Project Context field — things "
            "like building type, square footage, number of stories, or "
            "any special conditions. You can also attach files (.docx, .pdf, "
            ".md, .txt) whose text is merged into the context. This context "
            "is included with every API call — review, cross-check, and "
            "verification — and helps Claude produce more relevant findings. "
            "Click Expand for a larger editing area."
        )),
        ("5.  Batch Processing", (
            "All specs are queued and processed through the Batch API on Claude "
            f"{_model_label(REVIEW_MODEL_DEFAULT)} at 50% cost savings, with "
            f"results {_BATCH_TIMING_COPY}."
        )),
        ("6.  Enable Cross-Spec Coordination (Optional)", (
            "Check this option to run a separate coordination analysis that "
            "sends all spec content to Claude in a single call. This catches "
            "contradictions between specs, missing cross-references, and "
            "scope gaps that per-spec review cannot detect. Large projects are "
            "automatically chunked by CSI division when the combined input "
            "exceeds the recommended token ceiling."
        )),
        ("7.  Run the Review", (
            "Click Submit Batch. "
            "The activity log shows progress. The batch runs on Anthropic's "
            "servers (up to ~24h), so you can close the app or lose your "
            "connection without losing the work — the batch is saved, and on "
            "next launch you'll be prompted to resume polling and finish the "
            "run. You can also recover a batch from a terminal with "
            "scripts/recover_batch.py."
        )),
        ("8.  Save the Report", (
            "When the review completes, you'll be prompted to save a formatted "
            ".docx report. Spec Critic also writes a JSON sidecar next to it "
            "listing the suggested edits (existing text and proposed replacement "
            "per finding) for use by a separate editing tool. Your source files "
            "are never modified."
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
            "lower cost, with slower turnaround. Save your API key to a file so you don't "
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
