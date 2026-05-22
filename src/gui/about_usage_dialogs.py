"""Static informational dialogs ("How It Works" / "How to Use").

These two windows are pure UI: long blocks of static text rendered in a
modal CTkToplevel. Keeping them out of gui.py preserves the GUI shell as
a thin layout-and-wiring file.
"""
from __future__ import annotations

import customtkinter as ctk

from .widgets import COLORS

_UI_FONT_SIZE = 12
_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"


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

    ctk.CTkLabel(
        outer, text="AI-assisted M&P specification review for California K-12 DSA projects",
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_muted"],
    ).pack(anchor="w", padx=20, pady=(0, 12))

    scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    sections = [
        ("1.  Text Extraction", (
            "Your .docx files are read locally. Paragraphs and tables are "
            "extracted — nothing is sent to Claude yet."
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
        ("3.  Per-Spec Review", (
            "Each specification is sent individually to Claude Opus 4.7. "
            "Claude checks for code compliance issues (CBC, CMC, CPC, "
            "Energy Code, CALGreen), DSA-specific requirements, outdated standards, "
            "coordination problems, and constructability concerns. Each finding is "
            "assigned a severity (Critical, High, Medium, or Gripe) and a confidence score. "
            "The Review Mode selector controls scope: Strict (evidence-backed contradictions "
            "and code-cycle issues only), Comprehensive (adds AEC constructability and "
            "coordination), or Safe edit (only findings that can be expressed as a precise "
            "auto-applicable edit). Real-time and batch use identical prompts, models, "
            "and output caps — findings should be equivalent across modes."
        )),
        ("4.  Deduplication", (
            "When the same issue appears across multiple specs — like an outdated "
            "seismic code reference — duplicates are consolidated into a single "
            "finding that lists all affected files. Per-file edit occurrences are "
            "preserved internally so multi-file edits can target every affected spec."
        )),
        ("5.  Cross-Spec Coordination  (optional)", (
            "If enabled, a separate Sonnet 4.6 call analyzes the full text of all your "
            "specs together using the 1M token context window. It catches contradictions "
            "between specs, missing cross-references, scope gaps and overlaps, "
            "inconsistent equipment data, and division-of-work conflicts. Large projects "
            "are chunked by CSI division (21 / 22 / 23 / Controls) and merged. Cross-check "
            "runs in parallel with verification to reduce wall-clock time."
        )),
        ("6.  Verification", (
            "Every finding that needs external grounding is checked in a secondary AI "
            "pass with web search. The default verifier is Claude Sonnet 4.6 (faster and "
            "cheaper); Opus 4.7 is used as an escalation model for Critical/High "
            "findings the first pass couldn’t ground (Unverified or no usable "
            "web evidence). Verdicts are Confirmed, Corrected, Disputed, or "
            "Unverified — a verdict cannot be marked Confirmed or Corrected unless the "
            "model’s cited URL actually appears in the web_search results, so model-"
            "invented citations are stripped and the finding is downgraded. Internal-only "
            "issues (placeholders, duplicates, internal contradictions, LEED, template "
            "markers) are resolved locally without web search and reported as Locally "
            "classified. This is an AI-assisted check, not a substitute for engineer review."
        )),
        ("7.  Edit Safety Classification", (
            "Each finding is labeled in the report as Auto-edit candidate, "
            "Manual edit candidate, Report only, or Suppressed. Auto-edit candidate "
            "requires a supportive verification status (Verified-supported, "
            "Verified-contradicted, or Locally classified), an edit-confidence of at "
            "least 0.7, and no cross-check suppression. Findings that propose an edit "
            "but don’t clear that bar fall to Manual edit candidate. Ambiguous "
            "matches, missing ADD anchors, and table/header/footer/rich-format edits "
            "are never auto-applied. When an id-anchored quote no longer matches the "
            "cited paragraph, the locator routes the edit to manual review rather than "
            "matching the quote elsewhere in the document."
        )),
        ("8.  Output", (
            "Results can be viewed in-app, exported as a Word report, or used to produce "
            "an edited copy of each spec. Auto-edit mode applies surgical changes in a "
            "safe order with revalidation immediately before each mutation. The source "
            "files are never overwritten."
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
            "documents. Auto-edit always writes to a copy. "
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
        ("2.  Select Specification Files", (
            "Click Browse and select one or more .docx specification files. "
            "The tool will extract text and analyze token usage. The token "
            "gauge shows the largest single spec's estimated API call size "
            "against the per-call limit — if a spec is too large, it will "
            "be flagged."
        )),
        ("3.  Add Project Context (Optional)", (
            "Describe your project in the Project Context field — things "
            "like building type, square footage, number of stories, or "
            "any special conditions. This context is included with every "
            "API call and helps Claude produce more relevant findings. "
            "Click Expand for a larger editing area."
        )),
        ("4.  Choose Your Mode", (
            "Real-time mode processes the review immediately in the current "
            "session — faster turnaround but higher cost. Batch mode queues all specs for processing at 50% "
            f"cost savings, with results {_BATCH_TIMING_COPY}. "
            "Both modes use identical prompts, models, output caps, and review "
            "logic — findings should be equivalent; only the API dispatch path "
            "differs. For more than a few specs, batch mode is strongly recommended."
        )),
        ("5.  Choose Review Mode", (
            "Strict reports only evidence-backed contradictions, code-cycle "
            "issues, and invalid references — fewer findings, higher precision. "
            "Comprehensive (the default) adds AEC constructability, coordination, "
            "TAB/commissioning, schedules, controls, closeout, and material "
            "coordination issues. Safe edit only emits findings whose fix is a "
            "precise, unambiguous, low-risk edit — useful when you intend to use "
            "the auto-edit output."
        )),
        ("6.  Enable Cross-Spec Coordination (Optional)", (
            "Check this option to run a separate coordination analysis that "
            "sends all spec content to Claude in a single call. This catches "
            "contradictions between specs, missing cross-references, and "
            "scope gaps that per-spec review cannot detect. Large projects are "
            "automatically chunked by CSI division when the combined input "
            "exceeds the recommended token ceiling."
        )),
        ("7.  Save the Report", (
            "When the review completes, you'll be prompted to save a formatted "
            ".docx report. After saving, you can choose to apply edits: "
            "auto-edit writes an edited copy of each spec — only Auto-safe "
            "findings are applied; ambiguous, table, header/footer, and "
            "richly formatted edits are downgraded to manual review. "
            "Source files are never overwritten."
        )),
        ("8.  Run the Review", (
            "Click Run Review (real-time) or Submit Batch (batch mode). "
            "The activity log shows progress. In batch mode, you can close "
            "the app and reopen it later — the pending batch state is saved "
            "and you will be prompted to resume."
        )),
        ("9.  Review the Results", (
            "Findings are grouped by severity (Critical, High, Medium, "
            "Gripe) and sorted by confidence within each severity tier. Each finding "
            "includes a verification verdict from a secondary AI pass with "
            "web search, and shows whether the verdict was externally grounded "
            "or escalated to Opus. Open the Diagnostics window to see "
            "model usage, prompt-cache hits, token counts by phase, "
            "verification evidence stats, and edit-skip reasons."
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
