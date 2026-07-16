"""Informational dialogs ("How It Works" / "How to Use" / "Why Trust It?").

These windows are pure UI: long blocks of mostly-static text rendered in a
modal CTkToplevel. Model names and the code basis are rendered from config
(``api_config`` defaults via the pricing table's labels, and the selected
module's cycle) so the copy can't drift when a default model or module
changes. Keeping them out of gui.py preserves the GUI shell as a thin
layout-and-wiring file.

The trust dialog (:func:`show_trust_dialog`) is a plain-language account of
the anti-hallucination and verification machinery for engineers and
stakeholders. Every claim it makes maps to an enforced mechanism in the
codebase (the grounding invariant, anchor validation, the diagnostics
banner, ...) — when one of those mechanisms changes, update the copy here.
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
        ("5.  Batch Processing (Default) or Real-Time", (
            "By default, all specs are queued and processed through the Batch "
            f"API on Claude {_model_label(REVIEW_MODEL_DEFAULT)} at 50% cost "
            f"savings, with results {_BATCH_TIMING_COPY}. Check “Real-time "
            "review (streaming)” in Options to stream the reviews "
            "synchronously instead: results arrive in minutes for small runs, "
            "verification runs live too (no batch queues anywhere), but the "
            "run bills at standard API pricing and has no crash resume — if "
            "the app closes mid-run, in-flight review work is lost. Very "
            "large specs (≥200k input tokens) still require batch mode."
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
            "Click Submit Batch (labeled Start Review (live) in real-time mode). "
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


def show_trust_dialog(parent) -> None:
    """Plain-language explanation of the verification and anti-hallucination
    safeguards, for engineers and stakeholders deciding whether to rely on
    the tool's output. Non-programming audience; assumes familiarity with
    specs and AEC review workflows."""
    dialog = _build_modal(parent, "Why You Can Trust the Results", "660x700")

    outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    outer.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        outer, text="Why You Can Trust the Results",
        font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=20, pady=(20, 4))

    ctk.CTkLabel(
        outer,
        text="How Spec Critic guards against AI errors — and shows its work",
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_muted"],
    ).pack(anchor="w", padx=20, pady=(0, 12))

    scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    verifier_label = _model_label(VERIFICATION_MODEL_DEFAULT)
    escalation_label = _model_label(VERIFICATION_ESCALATION_MODEL)

    sections = [
        ("The ground rule: trust is earned per finding, never assumed", (
            "Spec Critic is built on one assumption: an AI's claim is just a "
            "claim until it survives checking. The tool's job is not to sound "
            "confident — it is to show, for every individual finding, how much "
            "checking stands behind it, and to say plainly when a check could "
            "not be completed. Every finding in the report carries a status "
            "(Verified, Disputed, Insufficient evidence, Locally classified, "
            "Verification failed, and so on), and nothing is ever silently "
            "promoted to a stronger status than the evidence supports. Think "
            "of the statuses the way you would read stamps in a plan-review "
            "set: they tell you what has been checked, by whom, and what "
            "still needs a human decision."
        )),
        ("Two independent AIs: one proposes, another cross-examines", (
            "The AI that reads your specs and drafts findings never gets the "
            "final word on whether it was right. Every substantive finding is "
            "handed to a second, separate AI — the verifier — whose only job "
            "is to check the claim against the outside world using live web "
            "search of code texts, standards bodies, and authority-having-"
            "jurisdiction publications. This is the same principle as plan "
            "review: the author does not approve their own work. For the "
            f"highest-stakes findings the first verifier ({verifier_label}) "
            f"cannot settle, a stronger model ({escalation_label}) re-runs "
            "the check from scratch. And when two verifiers reach different, "
            "well-supported conclusions, the report marks the finding "
            "Contested and recommends human review — the disagreement itself "
            "is treated as information, not something to be papered over."
        )),
        ("The hallucination guard: no real source, no “Verified”", (
            "Language models can invent plausible-looking citations — a code "
            "section that doesn't exist, a standards document that was never "
            "published. Spec Critic's most important rule is aimed squarely "
            "at this: a finding can only be marked Verified (confirmed or "
            "corrected) if the verifier cited at least one source that was "
            "actually retrieved during its live web search. The software "
            "compares every source the AI claims to have used against the "
            "list of pages the search really returned. A citation that "
            "doesn't match is stripped, and the verdict is automatically "
            "downgraded to Insufficient evidence. This rule is enforced in "
            "three independent places in the software — including the saved-"
            "results store, which refuses to remember a verdict that lacks a "
            "real source — so a fabricated citation cannot reach the report "
            "through any path. When you see “Verified,” it always means you "
            "can follow the listed source and read the same evidence the "
            "verifier read."
        )),
        ("Anchored to your code cycle, not the AI's memory", (
            "An AI's built-in knowledge is frozen at its training date and "
            "fuzzy about editions — exactly the wrong properties for code "
            "review. So Spec Critic never asks the AI to remember which code "
            "applies. Each review module pins the precise code basis: the "
            "adopted cycle and the specific standard editions (for example, "
            "which edition of NFPA 13 the jurisdiction actually adopted, "
            "including state amendments). That pinned list is written into "
            "every review and verification request, and the reviewer is "
            "instructed to flag departures from those editions specifically. "
            "Saved verification results are keyed to the exact cycle and "
            "edition list (and, for location-aware modules, the project's "
            "jurisdiction) — change any of them and prior verdicts are not "
            "reused; everything re-verifies against the new basis."
        )),
        ("Some checks never touch an AI at all", (
            "The mechanical problems — unresolved placeholders like [VERIFY] "
            "or TBD, leftover TODO markers, paragraphs duplicated verbatim, "
            "code years that don't exist (a “2018 CBC” was never published), "
            "references to superseded cycles, empty sections, CSI-number and "
            "filename mismatches — are found by plain deterministic pattern "
            "matching, the same technology as find-and-replace. These "
            "detectors run before any AI is involved, produce the same "
            "answer every time, and cannot hallucinate. Findings of this "
            "kind are labeled “Locally classified” so you can tell at a "
            "glance that they rest on mechanical detection, not AI judgment."
        )),
        ("How suggested edits are decided — and why they are never applied", (
            "When the AI proposes a text change, it must quote the exact "
            "existing spec language and the exact replacement — no "
            "paraphrasing. Before that suggestion reaches the report, the "
            "software mechanically confirms the quoted text really does "
            "appear, word for word, in the named spec file; a suggestion "
            "anchored to text that isn't there is demoted to a report-only "
            "observation. Suggestions that would change nothing are rejected "
            "outright. Each surviving suggestion carries two signals side by "
            "side: the reviewing model's own confidence, and the independent "
            "verification verdict — and once a real verdict exists, the "
            "report visibly favors the verdict over the model's self-rating. "
            "Most importantly, Spec Critic never edits your documents. It "
            "writes suggestions into the report and a machine-readable "
            "sidecar file; applying any of them remains a deliberate human "
            "decision, with the engineer of record in control."
        )),
        ("When something goes wrong, the report says so", (
            "A review tool earns trust by admitting what it could not do. "
            "Every exported report opens with a Run Diagnostics banner that "
            "names any spec whose review failed outright — because a spec "
            "with zero findings from a failed review is not a clean bill of "
            "health — and flags verification calls that hit technical "
            "failures, findings whose search budget ran out before grounding, "
            "and any cross-spec coordination chunks that were not analyzed. "
            "Reused verdicts from earlier runs are labeled with their age. "
            "And every verified finding includes an evidence panel listing "
            "the sources consulted, which citations were accepted or "
            "rejected, and which models did the work — so an engineer can "
            "retrace the entire chain of reasoning without taking anything "
            "on faith."
        )),
        ("The honest limits", (
            "Spec Critic is an assistant, not an authority. It is advisory "
            "only and is not a substitute for the engineer of record, peer "
            "review, or AHJ review. An AI review can miss issues — a clean "
            "report does not certify a compliant spec — and verification is "
            "only as good as what is publicly retrievable online; some "
            "authority requirements live in documents no search can reach. "
            "Code citations should be spot-checked against the published "
            "text before acting on them. The design goal has never been "
            "“the AI is always right.” It is narrower and more useful: for "
            "every claim in the report, you can see exactly how much "
            "checking stands behind it, and what kind."
        )),
    ]

    _render_sections(scroll, sections)

    ctk.CTkButton(
        outer, text="Close", width=100, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=dialog.destroy,
    ).pack(pady=(0, 16))
