"""System prompt and user message construction for the M&P specification reviewer.

Phase 8 (plan section 12.1) splits review behavior into three explicit modes —
STRICT, COMPREHENSIVE, SAFE_EDIT — so a single prompt no longer has to act as
strict reviewer, deep AEC reviewer, and edit generator at once. The output
schema is identical across modes; only the *scope* and *editability emphasis*
shift. Comprehensive adds the broader AEC categories listed in plan section
12.2.
"""

from __future__ import annotations

from .code_cycles import CodeCycle
from .review_modes import DEFAULT_REVIEW_MODE, ReviewMode, coerce_review_mode


_STRICT_CATEGORIES = """\
1. Internal contradictions within the spec (e.g., conflicting requirements in different articles).
2. Code edition misalignment: the current cycle is CBC {cbc}, CMC {cmc}, CPC {cpc}, Energy {energy}, CALGreen {calgreen}, ASCE {asce7}. Flag references to superseded editions (e.g., ASCE {asce7_prev} instead of {asce7}).
3. References to sections, standards, or test methods that do not exist or have been withdrawn.
4. Explicit cross-references to other CSI sections, equipment tags, or coordination dependencies that the spec author should verify."""


# Phase 8 / plan section 12.2: comprehensive scope adds the broader AEC
# constructability and coordination categories that the prior single prompt
# only hinted at. The ordering mirrors how reviewers usually walk a spec set.
_COMPREHENSIVE_CATEGORIES_EXTRA = """\
5. Constructability and coordination conflicts (e.g., requirements that contradict typical means and methods, or that depend on equipment/access not provided by another section).
6. Test, adjust, and balance (TAB) and commissioning conflicts (e.g., commissioning sequences that disagree with controls or HVAC narratives).
7. Equipment schedule / spec contradictions when schedules are referenced or supplied (capacity, voltage, accessory, or basis-of-design mismatches).
8. Division 01 coordination conflicts (general requirements that the technical section duplicates or contradicts).
9. Warranty conflicts within and across sections (duration, coverage, start date).
10. Product basis-of-design / approved-equal language conflicts.
11. Controls sequence / spec conflicts (sequence of operations vs. devices and points listed).
12. DSA / HCAI / Title 24 closeout and testing requirements that are missing or under-specified.
13. Fire and smoke damper access coordination (access doors, ceiling access, labeling).
14. Seismic restraint references (OSHPD/OPM/OPA preapprovals, anchor design responsibility).
15. Sprinkler / hydraulic calculation language conflicts (occupancy hazard, density, demand area, listed components).
16. Pipe / duct material conflicts across related sections.
17. Submittal and O&M conflicts (what is required, when, in what form)."""


_STRICT_TASK = (
    "Review the submitted specifications and identify issues. For each issue found, "
    "classify severity, provide a confidence score, and provide actionable corrections.\n"
    "Stay strictly within the scope categories listed below. Do not add editorial or "
    "constructability commentary unless it is directly evidenced in the spec text.\n"
    "Review every article in every specification. Do not stop early. Return exactly "
    "as many findings as genuinely supported, including zero."
)

_COMPREHENSIVE_TASK = (
    "Review the submitted specifications and identify issues. For each issue found, "
    "classify severity, provide a confidence score, and provide actionable corrections.\n"
    "Cover the full scope listed below, including AEC constructability and coordination "
    "categories. Treat coordination, TAB/commissioning, scheduling, and closeout-quality "
    "items as in-scope when supported by spec text — do not invent issues to fill "
    "categories. Review every article in every specification. Return exactly as many "
    "findings as genuinely supported, including zero."
)

_SAFE_EDIT_TASK = (
    "Review the submitted specifications and identify issues that can be corrected with "
    "a precise, unambiguous edit. Only report a finding when:\n"
    "  - the existing language can be quoted verbatim from a single paragraph "
    "(EDIT/DELETE), or\n"
    "  - a stable nearby paragraph can serve as an unambiguous anchor (ADD), and\n"
    "  - the proposed replacement is a low-risk, deterministic change (no speculative "
    "rewrites, no whole-section rewrites, no changes to tables/headers/footers).\n"
    "Skip issues that are real but cannot be expressed as a safe, locatable edit; the "
    "comprehensive review pass will catch those separately. Review every article in "
    "every specification. Return exactly as many findings as genuinely supported, "
    "including zero."
)

_MODE_TASK_TEXT: dict[ReviewMode, str] = {
    ReviewMode.STRICT: _STRICT_TASK,
    ReviewMode.COMPREHENSIVE: _COMPREHENSIVE_TASK,
    ReviewMode.SAFE_EDIT: _SAFE_EDIT_TASK,
}


def _categories_block(cycle: CodeCycle, mode: ReviewMode) -> str:
    base = _STRICT_CATEGORIES.format(
        cbc=cycle.cbc,
        cmc=cycle.cmc,
        cpc=cycle.cpc,
        energy=cycle.energy_code,
        calgreen=cycle.calgreen,
        asce7=cycle.asce7,
        asce7_prev=cycle.asce7_previous,
    )
    if mode is ReviewMode.COMPREHENSIVE:
        return base + "\n" + _COMPREHENSIVE_CATEGORIES_EXTRA
    if mode is ReviewMode.SAFE_EDIT:
        return base + (
            "\n\nIn safe-edit mode, only report findings from these categories when "
            "they can be expressed as an unambiguous EDIT/DELETE quotation or an ADD "
            "with a verbatim anchor. Skip findings that would require multi-paragraph "
            "or table-level rewrites; those belong to the comprehensive pass."
        )
    # STRICT
    return base + (
        "\n\nA spec that passes all checks cleanly is a good outcome. Do not manufacture "
        "findings to fill categories."
    )


def _editability_clause(mode: ReviewMode) -> str:
    if mode is ReviewMode.SAFE_EDIT:
        return (
            "\nFor every finding you report, the existingText (or anchorText for ADD) "
            "MUST be copied verbatim from a single paragraph in the source spec — no "
            "paraphrasing, no merged paragraphs. If you cannot quote a single paragraph "
            "verbatim, do not emit the finding.\n"
        )
    if mode is ReviewMode.COMPREHENSIVE:
        return (
            "\nWhen a finding cannot be expressed as a clean edit (e.g., it requires "
            "spec-author judgement, a meeting between disciplines, or a multi-paragraph "
            "rewrite), still report it: set actionType to the closest match, leave "
            "existingText as the most representative quote, and explain the required "
            "follow-up in the issue field. Downstream code marks ambiguous edits for "
            "manual review automatically; do not self-censor real coordination problems "
            "just because the fix is not a one-line replacement.\n"
        )
    return ""


def get_system_prompt(cycle: CodeCycle, mode: ReviewMode | str | None = None) -> str:
    """Return the reviewer system prompt for a code cycle and review mode.

    ``mode`` defaults to :data:`DEFAULT_REVIEW_MODE` (comprehensive). Strings
    such as ``"strict"`` or ``"safe_edit"`` are accepted for convenience.
    """
    selected = coerce_review_mode(mode) if not isinstance(mode, ReviewMode) else mode
    if selected is None:
        selected = DEFAULT_REVIEW_MODE
    task_text = _MODE_TASK_TEXT[selected]
    categories = _categories_block(cycle, selected)
    editability = _editability_clause(selected)
    return f"""You are a specification reviewer for mechanical and plumbing disciplines. The project context is California K-12 education facilities under DSA jurisdiction.

<review_mode>
Active review mode: {selected.value.upper()}
</review_mode>

<task>
{task_text}
</task>

<severity_definitions>
CRITICAL — showstoppers for DSA approval, safety, or code compliance.
HIGH — major technical issues requiring correction.
MEDIUM — meaningful issues with moderate impact.
GRIPES — quality/editorial issues that should still be fixed.
</severity_definitions>

<output_format>
First provide ANALYSIS SUMMARY (1-2 paragraphs), then wrap findings JSON in <findings_json>...</findings_json>.
Each finding fields:
- severity: "CRITICAL" | "HIGH" | "MEDIUM" | "GRIPES"
- fileName
- section
- issue
- actionType: "ADD" | "EDIT" | "DELETE"
- existingText (verbatim text to edit/delete; null for ADD)
- replacementText (only the new text to write; for ADD, the inserted block alone)
- codeReference
- confidence (0.0-1.0)

For actionType "ADD" only, also include:
- anchorText: verbatim text of an existing nearby paragraph to anchor the insertion (omit or use null if no reliable anchor exists; the edit will be flagged for manual review)
- insertPosition: "before" or "after" relative to anchorText

If none, return [] inside tags.
</output_format>
{editability}
<review_scope>
These are the categories of issues you are qualified to identify. Only report a finding if you have concrete evidence from the spec text that a genuine problem exists. If a category has no issues, that is a normal and expected outcome — do not force findings into any category.

Categories:
{categories}
</review_scope>"""


def get_single_spec_user_message(
    spec_content: str,
    filename: str,
    project_context: str = "",
    *,
    cycle: CodeCycle,
    mode: ReviewMode | str | None = None,
) -> str:
    """Build user message for reviewing a single spec in isolation."""
    selected = coerce_review_mode(mode) if not isinstance(mode, ReviewMode) else mode
    if selected is None:
        selected = DEFAULT_REVIEW_MODE
    context_block = ""
    if project_context.strip():
        context_block = f"""
<project_context>
{project_context.strip()}
</project_context>

"""

    mode_reminder = {
        ReviewMode.STRICT: (
            "Mode reminder: STRICT — report only evidence-backed contradictions, "
            "code-cycle issues, and invalid references."
        ),
        ReviewMode.COMPREHENSIVE: (
            "Mode reminder: COMPREHENSIVE — strict scope plus AEC constructability, "
            "coordination, TAB/commissioning, schedule/spec, controls, closeout, "
            "and material-coordination categories."
        ),
        ReviewMode.SAFE_EDIT: (
            "Mode reminder: SAFE_EDIT — only emit findings whose fix is a precise, "
            "unambiguous, low-risk edit. Skip real-but-unsafe-to-edit issues."
        ),
    }[selected]

    return f"""Review the following specification document for a California K-12 project under DSA jurisdiction.

Current code cycle: CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc}, Energy Code {cycle.energy_code}, CALGreen {cycle.calgreen}, ASCE {cycle.asce7}.

{mode_reminder}

Reminders:
- Review every section in the file.
- Analysis summary first, then JSON findings in <findings_json> tags.
- Include confidence (0.0-1.0) with each finding.

{context_block}===== FILE: {filename} =====
{spec_content}"""
