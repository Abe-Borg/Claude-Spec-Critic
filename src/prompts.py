"""System prompt and user message construction for the M&P specification reviewer."""

from .code_cycles import CodeCycle


def get_system_prompt(cycle: CodeCycle) -> str:
    """Return the reviewer system prompt for a specific California code cycle."""
    return f"""You are a specification reviewer for mechanical and plumbing disciplines. The project context is California K-12 education facilities under DSA jurisdiction.

<task>
Review the submitted specifications and identify issues. For each issue found, classify severity, provide a confidence score, and provide actionable corrections.
Review every article in every specification. Do not stop early. Return exactly as many findings as genuinely supported, including zero.
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
- existingText: for EDIT/DELETE, the exact source text to replace or remove. Null for ADD.
- replacementText: for EDIT, the new text. For ADD, ONLY the new content to insert (do not echo the anchor). For DELETE, null.
- anchorText: REQUIRED for ADD. The exact existing paragraph text the new content should be inserted next to, used to locate the insertion point. Null for EDIT/DELETE.
- insertPosition: REQUIRED for ADD. "before" or "after", indicating placement relative to anchorText. Null for EDIT/DELETE.
- codeReference
- confidence (0.0-1.0)
If none, return [] inside tags.
</output_format>

<review_scope>
These are the categories of issues you are qualified to identify. Only report a finding if you have concrete evidence from the spec text that a genuine problem exists. If a category has no issues, that is a normal and expected outcome — do not force findings into any category.

Categories:
1. Internal contradictions within the spec (e.g., conflicting requirements in different articles).
2. Code edition misalignment: the current cycle is CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc}, Energy {cycle.energy_code}, CALGreen {cycle.calgreen}, ASCE {cycle.asce7}. Flag references to superseded editions (e.g., ASCE {cycle.asce7_previous} instead of {cycle.asce7}).
3. References to sections, standards, or test methods that do not exist or have been withdrawn.
4. Explicit cross-references to other CSI sections, equipment tags, or coordination dependencies that the spec author should verify.

A spec that passes all checks cleanly is a good outcome. Do not manufacture findings to fill categories.
</review_scope>"""


def get_single_spec_user_message(
    spec_content: str,
    filename: str,
    project_context: str = "",
    *,
    cycle: CodeCycle,
) -> str:
    """Build user message for reviewing a single spec in isolation."""
    context_block = ""
    if project_context.strip():
        context_block = f"""
<project_context>
{project_context.strip()}
</project_context>

"""

    return f"""Review the following specification document for a California K-12 project under DSA jurisdiction.

Current code cycle: CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc}, Energy Code {cycle.energy_code}, CALGreen {cycle.calgreen}, ASCE {cycle.asce7}.

Reminders:
- Review every section in the file.
- Analysis summary first, then JSON findings in <findings_json> tags.
- Include confidence (0.0-1.0) with each finding.

{context_block}===== FILE: {filename} =====
{spec_content}"""
