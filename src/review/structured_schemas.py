"""Tool-output schemas for review, cross-check, verification, and triage.

* Every review / cross-check / verification call exposes a single custom
  tool whose ``input_schema`` matches the desired payload shape.
* ``tool_choice`` is ``{"type": "auto"}`` on every phase that sends
  ``thinking``. The code was written when forcing a tool was believed to be
  rejected under any thinking; Anthropic's thinking page (rechecked
  2026-09-29, plan EX-02) now limits that to manual ``budget_tokens``
  thinking and to the models that reject forced tool use outright (Opus
  5.5, Sonnet 5.5, Fable 5.1, Mythos 5.1). No forced request with adaptive
  thinking has been sent from this repository, so ``auto`` stays the
  default, and forcing is one arm of the default-off review experiment
  (:func:`review_output_mode`). Triage (:func:`triage_tool_choice`), the
  only phase that never sends ``thinking``, forces its single tool on
  models whose capability record carries ``supports_forced_tool_choice``
  (Haiku 4.5) and keeps ``auto`` everywhere else.
* The model is *instructed* to call the tool, but with ``auto`` it MAY
  return a plain-text response instead. Callers must therefore keep the
  tagged-JSON text fallback parsers reachable.
* ``strict: true`` is attached by default for models the capability
  whitelist marks as supporting it (see :func:`_strict_for_model` — env
  flag AND ``supports_strict_tools``), grammar-constraining the payload to
  the schema *when the model calls the tool*. Strict mode makes the
  payload shape contractual; it does not make the tool call itself
  contractual — the fallback above still applies. Unknown-model overrides
  degrade to the lenient shape, never a 400.
* Three mechanisms, kept apart (plan EX-02): strict tool arguments (on),
  forced tool invocation (triage only; a review experiment arm), and a
  constrained final response (``output_config.format``; the other review
  experiment arm). One does not imply another: strict arguments do not
  make the call happen, forcing the call does not constrain a text reply,
  and a constrained final response carries no tool call at all.

The schemas stay inside the strict-mode supported subset: every property
required, optionals nullable, ``additionalProperties: false``, no
``oneOf``/``anyOf``, no numerical or string-length constraints.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any

_log = logging.getLogger(__name__)


def structured_tool_output_enabled() -> bool:
    """Whether review/cross-check/verification expose their custom tool schemas.

    Always True. Every request includes the appropriate custom tool
    (``submit_review_findings`` / ``submit_cross_check_findings`` /
    ``submit_verification_verdict``) and ``tool_choice={"type": "auto"}``.
    The model is expected but not required to call the tool; the
    tagged-JSON text-fallback parsers stay reachable for the rare case
    where the model emits plain text instead.
    """
    return True


# ---------------------------------------------------------------------------
# Shared finding object schema (review + cross-check)
# ---------------------------------------------------------------------------

# Confidence bands the review rubric names, the report renders, and the
# finding schema's ``confidence`` description restates. One definition so a
# threshold moved here cannot leave another surface describing the old bands;
# the qualitative wording of each band lives only in ``prompts.py``'s rubric.
CONFIDENCE_HIGH_MIN = 0.85
CONFIDENCE_MODERATE_MIN = 0.60


_FINDING_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # All properties are required so strict-mode constrained sampling has a
    # deterministic shape to fill. Optional values use nullable types.
    "required": [
        "severity",
        "fileName",
        "section",
        "issue",
        "actionType",
        "existingText",
        "replacementText",
        "codeReference",
        "confidence",
        "anchorText",
        "insertPosition",
        # Optional evidence pointer. Required-but-nullable so
        # strict-mode constrained sampling still has a deterministic shape.
        "evidenceElementId",
    ],
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["CRITICAL", "HIGH", "MEDIUM", "GRIPES"],
            "description": "Severity classification.",
        },
        "fileName": {
            "type": "string",
            "description": "Spec file the finding applies to (or the primary file for cross-spec issues).",
        },
        "section": {
            "type": "string",
            "description": "CSI section reference (e.g. '230523', 'Part 2.3.A').",
        },
        "issue": {
            "type": "string",
            "description": "Plain-language description of the problem.",
        },
        "actionType": {
            "type": "string",
            # ``REPORT_ONLY`` is the explicit "this finding has no clean
            # textual fix" choice. Models no longer have to manufacture a
            # replacement quote for coordination / interpretation findings
            # — they emit REPORT_ONLY and leave the edit-shaped slots null.
            "enum": ["ADD", "EDIT", "DELETE", "REPORT_ONLY"],
            "description": (
                "Whether the fix is to add, edit, or delete text, or "
                "REPORT_ONLY when no clean textual fix exists (coordination "
                "or interpretation finding)."
            ),
        },
        "existingText": {
            "type": ["string", "null"],
            "description": "For EDIT/DELETE: the exact verbatim text in the spec. For ADD/REPORT_ONLY: nullable.",
        },
        "replacementText": {
            "type": ["string", "null"],
            "description": "Suggested replacement / new text. For DELETE/REPORT_ONLY: nullable.",
        },
        "codeReference": {
            "type": ["string", "null"],
            "description": "Applicable code clause or standard, e.g. 'CBC §1705.13'.",
        },
        "confidence": {
            # No JSON-Schema ``minimum``/``maximum``: numerical constraints
            # are outside the strict-mode supported subset, and the parser
            # already clamps confidence to 0..1 at parse time.
            "type": "number",
            "description": (
                # Thresholds only — the band definitions are the review system
                # prompt's rubric, not restated here, so the two cannot drift.
                f"0..1 confidence in the finding, using the bands the report "
                f"renders: >={CONFIDENCE_HIGH_MIN:.2f} high, "
                f"{CONFIDENCE_MODERATE_MIN:.2f}-{CONFIDENCE_HIGH_MIN - 0.01:.2f} "
                f"moderate, <{CONFIDENCE_MODERATE_MIN:.2f} low. Low confidence is "
                "a label for the downstream filter, never a reason to withhold "
                "the finding."
            ),
        },
        "anchorText": {
            "type": ["string", "null"],
            "description": "ADD only: verbatim nearby paragraph used to locate the insertion point.",
        },
        "insertPosition": {
            # No ``enum`` here: the live API's strict-mode schema validator
            # rejects an enum on a union type with a null member — observed
            # as a hard 400 at submit on every review/cross-check request
            # ("Enum value 'before' does not match declared type
            # '['string', 'null']'"). The value set lives in the description
            # instead, and ``validate_edit_shape`` already demotes an ADD
            # whose insertPosition is not "before"/"after" at parse time, so
            # nothing is lost contractually — same pattern as the removed
            # numeric constraints.
            "type": ["string", "null"],
            "description": (
                'ADD only: "before" or "after" the anchor. Null for every '
                "other action."
            ),
        },
        # When the prompt renders spec elements with id attributes, the model should
        # cite the element id of the paragraph / row / heading the finding
        # quotes. The id is a stable per-run identifier (e.g. ``p17``,
        # ``t2r3``) emitted by the extractor — see ``ParagraphMapping.element_id``.
        # A downstream applier can use the id to disambiguate identical text
        # in different sections and to revalidate the target before mutating.
        # Nullable so existing behavior remains the fallback when the model
        # cannot identify a unique element with confidence.
        "evidenceElementId": {
            "type": ["string", "null"],
            "description": (
                "Stable id of the paragraph / row / heading the finding "
                "quotes (e.g. 'p17', 't2r3'). Use the exact id from the "
                "<para>/<row>/<heading> wrapper in the spec body. Leave "
                "null when no single element clearly owns the issue."
            ),
        },
    },
}


REVIEW_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["analysis_summary", "findings"],
    "properties": {
        "analysis_summary": {
            "type": "string",
            "description": "Short narrative covering the review thinking. Empty string is acceptable.",
        },
        "findings": {
            "type": "array",
            "items": _FINDING_OBJECT_SCHEMA,
            "description": "Zero or more findings.",
        },
    },
}


# Cross-check findings use the same finding object schema as the per-spec
# review — coordination claims have the same shape (severity, issue,
# action, evidence) as any other finding.
_CROSS_CHECK_FINDING_OBJECT_SCHEMA: dict[str, Any] = _FINDING_OBJECT_SCHEMA


CROSS_CHECK_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["coordination_summary", "findings"],
    "properties": {
        "coordination_summary": {
            "type": "string",
            "description": (
                "Plain-text summary organized by coordination theme. No markdown. "
                "Empty string is acceptable when no issues are found."
            ),
        },
        "findings": {
            "type": "array",
            "items": _CROSS_CHECK_FINDING_OBJECT_SCHEMA,
            "description": "Zero or more cross-spec coordination findings.",
        },
    },
}


TRIAGE_CLASSIFICATIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classifications"],
    "properties": {
        "classifications": {
            "type": "array",
            "description": (
                "One entry per finding in the input batch, in the same order. "
                "Use the integer index supplied in the prompt to reference each "
                "finding."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "classification", "reason"],
                "properties": {
                    "index": {
                        # No ``minimum``: numerical constraints are outside
                        # the strict-mode supported subset; the triage call
                        # site only accepts indices it actually sent, so an
                        # out-of-range index is dropped there.
                        "type": "integer",
                        "description": "Zero-based index of the finding being classified.",
                    },
                    "classification": {
                        "type": "string",
                        "enum": ["web_required", "local_skip"],
                        "description": (
                            "web_required: the finding asserts a code/standard/external "
                            "fact and must be verified with web evidence. "
                            "local_skip: the finding is verifiable from spec text alone "
                            "(internal contradiction with quoted text, formatting, typo, "
                            "duplicate, placeholder) and does not need web search."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief justification (one sentence).",
                    },
                },
            },
        },
    },
}


# Closed category set for research items. Drives the rendered profile's
# section grouping (``research.requirements_research``) and — later — the
# compliance pass's controlling-requirement classes. Closed enum on a
# non-nullable string is inside the strict-mode supported subset.
RESEARCH_ITEM_CATEGORIES: tuple[str, ...] = (
    "governing_code",
    "local_amendment",
    "ahj_requirement",
    "referenced_standard",
    "client_standard",
    "insurer_requirement",
    "site_environment",
)

# Actionability routing (D-7 [FT]): ``spec_requirement`` is content the
# specifications must contain or match; ``process_advisory`` is a
# permit/schedule/process fact (fees, notice periods, seasonal windows) the
# project team must act on but which is not spec text — advisories must
# never generate "missing from the spec" coverage rows downstream. Unknown
# values coerce to ``spec_requirement`` at parse: the safe default, since it
# can only over-check, never silently skip.
RESEARCH_ACTIONABILITY_VALUES: tuple[str, ...] = (
    "spec_requirement",
    "process_advisory",
)


REQUIREMENTS_RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "items"],
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Short narrative of what was researched and how well it "
                "grounded. Empty string is acceptable."
            ),
        },
        "items": {
            "type": "array",
            "description": "Zero or more discrete requirements or facts.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                # All properties required; optionals are nullable — the
                # strict-mode subset discipline used by every other tool.
                "required": [
                    "topic",
                    "category",
                    "requirement",
                    "actionability",
                    "authority",
                    "code_reference",
                    "source_urls",
                    "confidence",
                    "notes",
                ],
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Short label for the requirement (a few words).",
                    },
                    "category": {
                        "type": "string",
                        "enum": list(RESEARCH_ITEM_CATEGORIES),
                        "description": "Requirement class.",
                    },
                    "requirement": {
                        "type": "string",
                        "description": (
                            "ONE discrete requirement or fact, stated so a "
                            "specification reviewer can act on it."
                        ),
                    },
                    "actionability": {
                        "type": "string",
                        "enum": list(RESEARCH_ACTIONABILITY_VALUES),
                        "description": (
                            "spec_requirement: content the specifications must "
                            "contain or match. process_advisory: a "
                            "permit/schedule/process fact (fees, notice periods, "
                            "seasonal windows, allocation reviews) the project "
                            "team must act on but which is not spec text."
                        ),
                    },
                    "authority": {
                        "type": ["string", "null"],
                        "description": "Who imposes the requirement (agency, insurer, client).",
                    },
                    "code_reference": {
                        "type": ["string", "null"],
                        "description": "Code/standard section citation when one exists.",
                    },
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "URLs of sources retrieved in this conversation that "
                            "support the requirement. Never cite a URL you did "
                            "not actually retrieve."
                        ),
                    },
                    "confidence": {
                        # No ``minimum``/``maximum``: numerical constraints are
                        # outside the strict-mode supported subset; the parser
                        # clamps to [0, 1].
                        "type": "number",
                        "description": (
                            "0..1 confidence. Use 0 for a requirement you could "
                            "not ground in retrieved sources (and explain in "
                            "notes) — never guess."
                        ),
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": (
                            "Caveats: paywalled primary source, official summary "
                            "used instead, pending amendments, etc."
                        ),
                    },
                },
            },
        },
    },
}


# Closed coverage-status set for the compliance pass (WS-4, D-7). Drives the
# coverage matrix in the report and the chunked-merge precedence
# (contradicted > represented > unclear > unanimous-missing). Unknown values
# coerce to "unclear" at parse — the honest default for a status the model
# invented.
COMPLIANCE_COVERAGE_STATUSES: tuple[str, ...] = (
    "represented",
    "missing",
    "contradicted",
    "unclear",
)


COMPLIANCE_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["compliance_summary", "coverage", "findings"],
    "properties": {
        "compliance_summary": {
            "type": "string",
            "description": (
                "Plain-text summary of how well the package represents the "
                "profile requirements. No markdown. Empty string is acceptable."
            ),
        },
        "coverage": {
            "type": "array",
            "description": (
                "One entry per controlling profile requirement id, none left "
                "out, classifying how the package represents it. [UNVERIFIED] "
                "items and process advisories ([PROCESS]) never get coverage "
                "entries."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requirement_id", "status", "evidence", "fileName"],
                "properties": {
                    "requirement_id": {
                        "type": "string",
                        "description": (
                            "The profile requirement id being classified "
                            "(e.g. 'r-1a2b3c4d5e6f')."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": list(COMPLIANCE_COVERAGE_STATUSES),
                        "description": (
                            "represented: the package correctly reflects the "
                            "requirement. missing: no spec content addresses it. "
                            "contradicted: spec content conflicts with it. "
                            "unclear: cannot be determined from the package."
                        ),
                    },
                    "evidence": {
                        "type": ["string", "null"],
                        "description": (
                            "Strongest supporting quote from the package (verbatim), "
                            "or null when none exists (e.g. missing)."
                        ),
                    },
                    "fileName": {
                        "type": ["string", "null"],
                        "description": "Spec file the evidence came from, or null.",
                    },
                },
            },
        },
        "findings": {
            "type": "array",
            "items": _FINDING_OBJECT_SCHEMA,
            "description": (
                "Zero or more compliance findings (missing/contradicted "
                "requirements, or spec text conflicting with a requirement)."
            ),
        },
    },
}


# Closed relationship set for a drawing-impact finding link (WS-5). Describes
# how the drawings bear on a finding. Unknown values coerce to
# "contextualized" at parse — the weakest, safe default (it neither confirms
# nor refutes the finding, so it can't overstate the drawings' contribution).
DRAWING_IMPACT_RELATIONSHIPS: tuple[str, ...] = (
    "corroborated",
    "contradicted",
    "contextualized",
)

# Closed overall-impact set. Drives the report badge; the model is instructed
# to pick "none"/"minimal" honestly when the drawings added little. Unknown
# values coerce to "minimal" at parse.
DRAWING_IMPACT_LEVELS: tuple[str, ...] = (
    "substantial",
    "moderate",
    "minimal",
    "none",
)


DRAWING_IMPACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["impact_level", "narrative", "finding_links"],
    "properties": {
        "impact_level": {
            "type": "string",
            "enum": list(DRAWING_IMPACT_LEVELS),
            "description": (
                "Overall degree to which having the drawings available changed "
                "or supported this review. Choose 'none' or 'minimal' honestly "
                "when the drawings only restated the specs or no finding turned "
                "on drawing content."
            ),
        },
        "narrative": {
            "type": "string",
            "description": (
                "Plain-text explanation (no markdown) of how the drawings "
                "informed the review: what they made checkable that the spec "
                "text alone did not, and where drawings and specs agree or "
                "conflict. State plainly when the drawings added little."
            ),
        },
        "finding_links": {
            "type": "array",
            "description": (
                "Only the findings the drawings genuinely bear on. Omit a "
                "finding entirely rather than inventing a connection."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "finding_id",
                    "relationship",
                    "explanation",
                    "sheet_references",
                ],
                "properties": {
                    "finding_id": {
                        "type": "string",
                        "description": (
                            "The exact id of a finding from the input list "
                            "(e.g. 'rf-1a2b3c4d5e6f'). Never invent an id."
                        ),
                    },
                    "relationship": {
                        "type": "string",
                        "enum": list(DRAWING_IMPACT_RELATIONSHIPS),
                        "description": (
                            "corroborated: the drawings independently support "
                            "the finding. contradicted: the drawings conflict "
                            "with it (reconcile the two). contextualized: the "
                            "drawings supply interpreting context without "
                            "confirming or refuting it."
                        ),
                    },
                    "explanation": {
                        "type": "string",
                        "description": (
                            "One or two plain-text sentences on how the drawing "
                            "content relates to this finding."
                        ),
                    },
                    "sheet_references": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The digest page references that support this link, "
                            "each in the digest's own '[<file> p.N]' form. Never "
                            "cite a page not present in the digest."
                        ),
                    },
                },
            },
        },
    },
}


# Coordination experiment (plan EX-06, default off). One observation per
# candidate pair of passages from two specifications. Closed assessment set;
# an unknown value is read as "cannot_tell" at parse, and a "conflict" whose
# quotes are not found in their passages is demoted to "cannot_tell".
COORDINATION_ASSESSMENTS: tuple[str, ...] = (
    "conflict",
    "not_conflict",
    "cannot_tell",
)


COORDINATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {
        "observations": {
            "type": "array",
            "description": (
                "One entry per candidate id in the input, and only those ids."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "candidate_id",
                    "assessment",
                    "same_scope_reason",
                    "side_a_quote",
                    "side_b_quote",
                    "explanation",
                ],
                "properties": {
                    "candidate_id": {
                        "type": "string",
                        "description": (
                            "The exact id of a candidate from the input "
                            "(e.g. 'co-1a2b3c4d5e6f'). Never invent an id."
                        ),
                    },
                    "assessment": {
                        "type": "string",
                        "enum": list(COORDINATION_ASSESSMENTS),
                        "description": (
                            "conflict: both passages refer to the same item in "
                            "the same scope and their requirements cannot both "
                            "be met. not_conflict: different items or scopes, or "
                            "compatible requirements. cannot_tell: the passages "
                            "do not establish whether they refer to the same "
                            "item in the same scope."
                        ),
                    },
                    "same_scope_reason": {
                        "type": "string",
                        "description": (
                            "For conflict: why both passages refer to the same "
                            "item and scope, from their own words. Otherwise: "
                            "what differs or what is missing."
                        ),
                    },
                    "side_a_quote": {
                        "type": "string",
                        "description": (
                            "Words copied exactly from passage A that state its "
                            "requirement."
                        ),
                    },
                    "side_b_quote": {
                        "type": "string",
                        "description": (
                            "Words copied exactly from passage B that state its "
                            "requirement."
                        ),
                    },
                    "explanation": {
                        "type": "string",
                        "description": (
                            "One or two plain-text sentences: what each side "
                            "requires and why that is or is not a conflict."
                        ),
                    },
                },
            },
        },
    },
}


VERIFICATION_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "explanation", "sources", "correction", "source_quote"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["CONFIRMED", "DISPUTED", "CORRECTED", "UNVERIFIED"],
            "description": "Verification outcome.",
        },
        "explanation": {
            "type": "string",
            "description": "1-3 sentences explaining the verdict and citing sources by domain.",
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "URLs or source identifiers used as evidence. May be empty.",
        },
        "correction": {
            "type": ["string", "null"],
            "description": "For CORRECTED verdicts only: the corrected reference text.",
        },
        # Every grounded verdict must carry a
        # verbatim snippet from the search result that the model actually
        # read. CONFIRMED/CORRECTED with an empty source_quote is demoted
        # to UNVERIFIED at parse time — see ``_verdict_from_tool_use`` and
        # the text fallback parser. DISPUTED is asked for one too (the
        # contradicting passage): the parser tolerates its absence, but the
        # verification cache never persists a DISPUTED without a quote
        # (``verification_cache._CITATION_GATED_VERDICTS``), so the prompt,
        # the schema, and the cache now ask for the same shape. Nullable so
        # UNVERIFIED (no supporting quote) still satisfies strict-mode
        # constrained sampling.
        "source_quote": {
            "type": ["string", "null"],
            "description": (
                "Verbatim text from a web_search result snippet that supports "
                "this verdict — the evidence you actually read, not a "
                "paraphrase. REQUIRED non-empty for CONFIRMED and CORRECTED; "
                "for DISPUTED, the retrieved passage that contradicts the "
                "claim; null for UNVERIFIED. If no snippet supports the "
                "verdict, you do not have grounded evidence — return UNVERIFIED."
            ),
        },
    },
}


# ---------------------------------------------------------------------------
# Tool builders. Each returns a single tool dict that callers pass via
# ``tools=[...]`` together with ``tool_choice={"type": "auto"}`` (see the
# module docstring for why ``auto``, and for the forced arm of the review
# experiment).
# ---------------------------------------------------------------------------

_REVIEW_TOOL_NAME = "submit_review_findings"
_CROSS_CHECK_TOOL_NAME = "submit_cross_check_findings"
_VERIFICATION_TOOL_NAME = "submit_verification_verdict"
_TRIAGE_TOOL_NAME = "submit_triage_classifications"
_RESEARCH_TOOL_NAME = "submit_requirements_research"
_COMPLIANCE_TOOL_NAME = "submit_compliance_findings"
_DRAWING_IMPACT_TOOL_NAME = "submit_drawing_impact"
_COORDINATION_TOOL_NAME = "submit_coordination_observations"


ENV_STRICT_TOOL_USE = "SPEC_CRITIC_STRICT_TOOL_USE"
_STRICT_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def _strict_enabled() -> bool:
    """Operator env gate for ``strict: true`` on tool definitions.

    This is one of two gates — the other is the per-model capability check
    in :func:`_strict_for_model`, which the tool builders actually consult
    (env flag AND model support).

    Strict tool use grammar-constrains the model's tool input to the declared
    JSON Schema, eliminating the malformed-/truncated-payload failure mode the
    tagged-JSON text fallback parsers exist to absorb — and which, on the
    review path, otherwise surfaces as a "failed review" spec that emits zero
    findings. The review / cross-check / verification / triage schemas all
    stay inside the strict-mode supported subset (every property required,
    optionals nullable, no ``oneOf``/``anyOf``, no numerical/string
    constraints), so the flag needs no schema rework.

    Default ON; disable with ``SPEC_CRITIC_STRICT_TOOL_USE=0`` (or ``false``
    / ``no`` / ``off``) to restore the legacy lenient tool shape — the escape
    hatch if an account / SDK / model combination ever rejects the strict
    shape at submit. The flag originally defaulted off because the
    strict-mode × adaptive-thinking interaction was unverified from the
    hermetic harness; Anthropic's structured-outputs docs now list strict
    tool use as compatible with extended thinking, streaming, and the
    Message Batches API, and
    ``tests/test_network_smoke.py::test_strict_tool_use_smoke`` sends the
    exact production strict shape against the live API — re-run it (with a
    real key) after an SDK or model-id bump.

    Strict mode guarantees a schema-valid payload only when the model *does*
    call the tool. Under ``tool_choice: auto`` a refusal or plain-text detour
    is still possible, and the rollback path runs lenient — so the tagged-JSON
    text fallback parsers stay reachable either way as defense-in-depth.
    """
    raw = os.environ.get(ENV_STRICT_TOOL_USE)
    if raw is None:
        return True
    return raw.strip().lower() not in _STRICT_DISABLE_TOKENS


def _strict_for_model(model: str | None) -> bool:
    """Whether to attach ``strict: true`` for a request bound to ``model``.

    Two gates AND together: the operator env flag (:func:`_strict_enabled`)
    and the model capability whitelist (``supports_strict_tools``). Strict
    tool use is part of structured outputs, which Anthropic documents for
    specific models — sending it to an unlisted-but-valid override (e.g.
    ``SPEC_CRITIC_VERIFICATION_MODEL`` pinned to an older Claude) risks a
    400 at submit. Routing through ``model_capabilities`` keeps the
    standing rule intact: a misconfigured model env var produces a smaller
    safe request, never an API rejection. ``model=None`` (a call site with
    no model in scope) degrades the same conservative way.
    """
    if not _strict_enabled():
        return False
    if model is None:
        return False
    from ..core.api_config import model_capabilities

    return model_capabilities(model).supports_strict_tools


def review_findings_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _REVIEW_TOOL_NAME,
        "description": (
            "Submit the structured per-spec review output. Use this tool exactly "
            "once. Return all findings (zero or more) in the ``findings`` array."
        ),
        "input_schema": REVIEW_FINDINGS_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def cross_check_findings_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _CROSS_CHECK_TOOL_NAME,
        "description": (
            "Submit the structured cross-spec coordination output. Use this "
            "tool exactly once. ``findings`` may be empty when coordination is "
            "adequate."
        ),
        "input_schema": CROSS_CHECK_FINDINGS_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def triage_classifications_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _TRIAGE_TOOL_NAME,
        "description": (
            "Submit triage classifications for a batch of findings. Use this "
            "tool exactly once with one entry per finding (matched by the "
            "integer index supplied in the prompt)."
        ),
        "input_schema": TRIAGE_CLASSIFICATIONS_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def triage_tool_choice(*, model: str | None = None) -> dict[str, Any]:
    """Tool choice for the Haiku triage classifier, forced when the model allows.

    Every other phase stays on ``auto`` (see the module docstring). Triage is
    the one phase that never sends ``thinking``
    (``api_config._PHASES_NO_THINKING``), so on Haiku 4.5, where an omitted
    key means no thinking, forcing the single exposed tool is a plain request.
    It removes the plain-text detour that ``_classify_batch`` logs as "no
    usable tool payload" — a detour that sends every finding in the chunk
    down the full ``web_required`` verification path, exactly the cost this
    pass exists to avoid.

    The gate is the model, not the phase: ``SPEC_CRITIC_TRIAGE_MODEL`` can
    name Opus 5 or Sonnet 5, where omitting ``thinking`` runs adaptive
    thinking (forcing there is documented as accepted but was never sent
    from this repository), or a model that rejects forced tool use outright
    (Opus 5.5, Sonnet 5.5).
    ``model_capabilities(model).supports_forced_tool_choice`` decides;
    ``model=None`` and unlisted ids keep ``auto`` — the request the API
    always accepts — like every other optional capability here.
    """
    if model is not None:
        from ..core.api_config import model_capabilities

        if model_capabilities(model).supports_forced_tool_choice:
            return {
                "type": "tool",
                "name": _TRIAGE_TOOL_NAME,
                "disable_parallel_tool_use": True,
            }
    return {"type": "auto", "disable_parallel_tool_use": True}


def requirements_research_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _RESEARCH_TOOL_NAME,
        "description": (
            "After researching with web search/fetch, submit the structured "
            "requirements-research output for this dimension. Use this tool "
            "exactly once as the final step of your turn."
        ),
        "input_schema": REQUIREMENTS_RESEARCH_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


# Research sends NO tool_choice — the same convention as verification (see
# the note below verification_verdict_tool). The ``_20260209`` web server
# tools run dynamic filtering (programmatic tool calling under the hood),
# and the API rejects ``disable_parallel_tool_use`` combined with it:
# HTTP 400 ``tool_choice.disable_parallel_tool_use: true cannot be used
# with programmatic tool calling``. The system prompt instructs the model
# to end the turn with the research tool; the tagged-JSON fallback
# (``<research_json>``) stays reachable for the text detour.


def compliance_findings_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _COMPLIANCE_TOOL_NAME,
        "description": (
            "Submit the structured local-code compliance output. Use this tool "
            "exactly once: one coverage entry per controlling profile "
            "requirement, plus findings for missing/contradicted requirements."
        ),
        "input_schema": COMPLIANCE_FINDINGS_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def compliance_tool_choice() -> dict[str, Any]:
    return {"type": "auto", "disable_parallel_tool_use": True}


def drawing_impact_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _DRAWING_IMPACT_TOOL_NAME,
        "description": (
            "Submit the structured explanation of how the construction "
            "drawings informed this specification review. Use this tool "
            "exactly once. ``finding_links`` may be empty when no finding "
            "turns on drawing content."
        ),
        "input_schema": DRAWING_IMPACT_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def drawing_impact_tool_choice() -> dict[str, Any]:
    return {"type": "auto", "disable_parallel_tool_use": True}


def coordination_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _COORDINATION_TOOL_NAME,
        "description": (
            "Submit one observation per candidate pair of specification "
            "passages. Use this tool exactly once."
        ),
        "input_schema": COORDINATION_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def coordination_tool_choice() -> dict[str, Any]:
    return {"type": "auto", "disable_parallel_tool_use": True}


def verification_verdict_tool(*, model: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _VERIFICATION_TOOL_NAME,
        "description": (
            "After consulting web search, submit the structured verification "
            "verdict for the finding under review. Use this tool exactly once "
            "as the final step of your turn."
        ),
        "input_schema": VERIFICATION_VERDICT_SCHEMA,
    }
    if _strict_for_model(model):
        tool["strict"] = True
    return tool


def review_tool_choice() -> dict[str, Any]:
    # The default review shape: {"type": "auto"}. With only one tool exposed
    # and the system prompt instructing the model to call it, the tool is
    # reliably — but not contractually — invoked; the tagged-JSON text parser
    # is the documented fallback for the path where the model returns text.
    # Forcing the tool is the ``forced_tool`` arm of the default-off EX-02
    # experiment (:func:`review_forced_tool_choice`), not the default: it is
    # documented as accepted with adaptive thinking on the review models but
    # has not been sent from this repository.
    return {"type": "auto", "disable_parallel_tool_use": True}

def cross_check_tool_choice() -> dict[str, Any]:
    return {"type": "auto", "disable_parallel_tool_use": True}

# Verification cannot use a forcing tool_choice because the model needs to
# call ``web_search`` first; instead the prompt instructs the model to emit
# the verdict tool as the final step. ``any`` lets it pick web_search early.


# ---------------------------------------------------------------------------
# Review output constraint (plan EX-02): an experiment, off by default
# ---------------------------------------------------------------------------
#
# The per-spec review is the first consumer of the schema-constrained output
# experiment. No measured parse-failure rate exists for any consumer (plans/
# experiments/EX-02-schema-constrained-outputs.md), so the review was chosen
# for what a failure there costs: an unparseable review is a zero-finding spec
# that pays for a full repair request (a second batch cycle on the batch
# transport), and while a repair is pending every dependent stage waits.
#
# Three request shapes, one per value of ``SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT``:
#
# * unset, empty, ``0`` / ``false`` / ``no`` / ``off`` — ``tool_auto``, the
#   default: the ``submit_review_findings`` tool under ``tool_choice: auto``.
#   Every request is byte-identical to a build without the switch.
# * ``forced_tool`` — the same tool and prompts, with ``tool_choice`` forcing
#   that tool. One change: the tool call becomes contractual. The response
#   shape is the default's.
# * ``json_schema`` — no tool; ``output_config.format`` constrains the final
#   response to :data:`REVIEW_FINDINGS_SCHEMA`, and the prompts say to return
#   the JSON object instead of calling the tool. The response is a text block.
#
# Anything else is ``tool_auto`` with one warning (an experiment switch fails
# closed). A value the review model's capability record does not vouch for —
# ``supports_forced_tool_with_thinking`` (or, on a request without thinking,
# ``supports_forced_tool_choice``) for ``forced_tool``;
# ``supports_json_output_format`` for ``json_schema`` — is also ``tool_auto``,
# with one warning per value and model. Both flags record what Anthropic
# documents, not what has been sent: neither shape has met the live API.
#
# Parsing never reads this switch. ``reviewer.review_result_from_message``
# reads whatever the response contains, so a batch submitted under one value
# is collected correctly under any other, including a batch submitted before
# the switch existed.

ENV_REVIEW_OUTPUT_CONSTRAINT = "SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT"

REVIEW_OUTPUT_TOOL_AUTO = "tool_auto"
REVIEW_OUTPUT_FORCED_TOOL = "forced_tool"
REVIEW_OUTPUT_JSON_SCHEMA = "json_schema"
REVIEW_OUTPUT_MODES: tuple[str, ...] = (
    REVIEW_OUTPUT_TOOL_AUTO,
    REVIEW_OUTPUT_FORCED_TOOL,
    REVIEW_OUTPUT_JSON_SCHEMA,
)

_REVIEW_OUTPUT_CONSTRAINT_VALUES = frozenset(
    {REVIEW_OUTPUT_FORCED_TOOL, REVIEW_OUTPUT_JSON_SCHEMA}
)
_WARNED_REVIEW_OUTPUT_VALUES: set[str] = set()
_WARNED_REVIEW_OUTPUT_UNSUPPORTED: set[tuple[str, str, bool]] = set()


def requested_review_output_constraint() -> str:
    """The review output shape the environment asks for (see the section above).

    Read at call time, so an evaluation arm switches with the environment
    alone. This is the request, not the decision: :func:`review_output_mode`
    checks it against the review model.
    """
    raw = os.environ.get(ENV_REVIEW_OUTPUT_CONSTRAINT)
    if raw is None:
        return REVIEW_OUTPUT_TOOL_AUTO
    val = raw.strip().lower()
    if val == "" or val in _STRICT_DISABLE_TOKENS:
        return REVIEW_OUTPUT_TOOL_AUTO
    if val in _REVIEW_OUTPUT_CONSTRAINT_VALUES:
        return val
    if val not in _WARNED_REVIEW_OUTPUT_VALUES:
        _WARNED_REVIEW_OUTPUT_VALUES.add(val)
        _log.warning(
            "%s=%r is not a recognized value (use forced_tool or json_schema); "
            "the review keeps its default tool_auto shape.",
            ENV_REVIEW_OUTPUT_CONSTRAINT,
            raw,
        )
    return REVIEW_OUTPUT_TOOL_AUTO


def review_output_mode(*, model: str | None, thinking: bool) -> str:
    """The review output shape for one request: one of :data:`REVIEW_OUTPUT_MODES`.

    ``thinking`` says whether the request carries a ``thinking`` config;
    forcing a tool is documented per model separately with and without it.
    A requested shape the model's capability record does not vouch for falls
    back to ``tool_auto`` — the request the API has always accepted — with
    one warning, so a model override never turns the experiment into a 400.
    """
    requested = requested_review_output_constraint()
    if requested == REVIEW_OUTPUT_TOOL_AUTO:
        return REVIEW_OUTPUT_TOOL_AUTO
    supported = False
    if model is not None:
        from ..core.api_config import model_capabilities

        caps = model_capabilities(model)
        if requested == REVIEW_OUTPUT_FORCED_TOOL:
            supported = (
                caps.supports_forced_tool_with_thinking
                if thinking
                else caps.supports_forced_tool_choice
            )
        else:
            supported = caps.supports_json_output_format
    if supported:
        return requested
    key = (requested, str(model), bool(thinking))
    if key not in _WARNED_REVIEW_OUTPUT_UNSUPPORTED:
        _WARNED_REVIEW_OUTPUT_UNSUPPORTED.add(key)
        _log.warning(
            "%s=%s is not documented for model %r (%s thinking); the review "
            "keeps its default tool_auto shape.",
            ENV_REVIEW_OUTPUT_CONSTRAINT,
            requested,
            model,
            "with" if thinking else "without",
        )
    return REVIEW_OUTPUT_TOOL_AUTO


def review_forced_tool_choice() -> dict[str, Any]:
    """``tool_choice`` for the ``forced_tool`` arm: the review tool, forced."""
    return {
        "type": "tool",
        "name": _REVIEW_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }


def review_json_output_format() -> dict[str, Any]:
    """``output_config.format`` for the ``json_schema`` arm.

    The schema is :data:`REVIEW_FINDINGS_SCHEMA`, the one the review tool
    already sends under strict tool use, so both arms constrain the payload
    to one shape and the parser reads it the same way. A copy, so a request
    can never alias (and mutate) the module's schema.
    """
    return {"type": "json_schema", "schema": copy.deepcopy(REVIEW_FINDINGS_SCHEMA)}


# ---------------------------------------------------------------------------
# Response unpacking
# ---------------------------------------------------------------------------

def _coerce_to_dict(value: Any) -> dict[str, Any] | None:
    """Best-effort conversion of an SDK value to a plain dict.

    Tool ``input`` payloads come back as plain dicts on the streaming path,
    but the batch-results path sometimes returns a Pydantic model instead.
    Without this coercion the caller silently falls back to text parsing,
    which then mis-parses (or fails on) perfectly valid structured output.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            data = dumper(mode="python", exclude_none=False)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    legacy_dumper = getattr(value, "dict", None)
    if callable(legacy_dumper):
        try:
            data = legacy_dumper()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def tool_name_matches(name: object, tool_name: str) -> bool:
    """True when a model's ``tool_use`` name is ``tool_name``, up to letter case.

    Anthropic's Sonnet 5.5 prompting guide ("Tolerant tool-call handling",
    checked 2026-09-29) notes that the model occasionally calls a declared
    tool by a name that differs only in letter case, and advises accepting
    the call when the match is unambiguous rather than treating it as fatal.
    Every request here declares one custom tool per name, and no two of this
    app's tool names differ only in case, so a case-only match is
    unambiguous. Anything else (a different word, a non-string) is not a
    match.
    """
    if not isinstance(name, str):
        return False
    return name == tool_name or name.casefold() == tool_name.casefold()


def extract_tool_use_block(response: object, tool_name: str) -> dict[str, Any] | None:
    """Pull the matching ``tool_use`` block's ``input`` off a response.

    Returns the input dict if found, otherwise None. Tolerates SDK
    Pydantic objects, plain dicts, and Pydantic-model ``input`` payloads
    (the batch retrieval path can return any of the three). A call whose
    name differs from ``tool_name`` only in letter case counts
    (:func:`tool_name_matches`).
    """
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if not content:
        return None
    for block in content:
        # SDK objects expose ``type``/``name`` as attrs; plain dicts use keys.
        btype = getattr(block, "type", None)
        if btype is None and isinstance(block, dict):
            btype = block.get("type")
        if btype != "tool_use":
            continue
        bname = getattr(block, "name", None)
        if bname is None and isinstance(block, dict):
            bname = block.get("name")
        if not tool_name_matches(bname, tool_name):
            continue
        binput = getattr(block, "input", None)
        if binput is None and isinstance(block, dict):
            binput = block.get("input")
        coerced = _coerce_to_dict(binput)
        if coerced is not None:
            return coerced
    return None


# A ``{`` or ``[`` that can open a JSON value: an object's next non-space
# character is a key's quote or its closing brace; an array's is a value's
# first character or its closing bracket. Permissive on purpose (it never
# rejects a real value); it only skips brackets in prose.
_JSON_VALUE_START = re.compile(r'\{(?=\s*["}])|\[(?=\s*[\[\]{"\-0-9tfn])')


def json_values_in_text(text: str) -> list[tuple[int, int, Any]]:
    """Every top-level JSON object or array in ``text``: ``(start, end, value)``.

    The method Anthropic's Sonnet 5.5 prompting guide gives for reading JSON
    a model wrote after working a problem out in prose ("Reasoning tasks with
    JSON output", checked 2026-09-29): starting at each ``{`` or ``[``, try
    to parse one JSON value; when one parses, continue from its end, so the
    values nested inside it are not counted on their own. Callers keep the
    last value of the shape they expect. The guide warns against taking
    everything from the first ``{`` to the last ``}``: the model occasionally
    writes a draft before its final JSON, and that range holds both. A
    bracket inside prose (a quoted ``[SELECT]`` placeholder, say) simply
    fails to parse and is skipped.

    Values are in text order; ``text[start:end]`` is each value's source.

    Only a bracket whose next non-space character can continue a JSON value
    is tried (:data:`_JSON_VALUE_START`): a failed parse builds a
    ``JSONDecodeError`` that counts the lines before it, so trying every
    bracket in a long reply full of ``[SELECT]``-style placeholders cost time
    quadratic in its length.
    """
    decoder = json.JSONDecoder()
    values: list[tuple[int, int, Any]] = []
    text = text or ""
    pos = 0
    for match in _JSON_VALUE_START.finditer(text):
        start = match.start()
        if start < pos:
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        values.append((start, end, value))
        pos = end
    return values


def last_tagged_json_object(text: str, tag: str) -> dict[str, Any] | None:
    """The last ``<tag>...</tag>`` block in ``text`` whose body is a JSON object.

    The tagged-JSON text fallbacks ask for one block, but the model
    occasionally writes a draft before its final JSON (Anthropic's Sonnet 5.5
    prompting guide, "Reasoning tasks with JSON output"), so the last block
    that parses is the answer. A greedy pattern spanning the first opening tag
    to the last closing tag read the two blocks as one invalid body.
    """
    pattern = re.compile(
        rf"<\s*{re.escape(tag)}\s*>(.*?)<\s*/\s*{re.escape(tag)}\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in reversed(list(pattern.finditer(text or ""))):
        try:
            data = json.loads(match.group(1).strip())
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_json_output_object(text: str) -> dict[str, Any] | None:
    """The response's text as one JSON object, or ``None``.

    What a constrained final response (``output_config.format``) returns: a
    text block holding exactly the JSON the schema describes. The whole text
    must parse as one object — no prose around it, no array, no substring
    search — because the schema guarantees that shape, and a lenient scan
    could read an object out of text that was never a constrained response.
    Anything else is ``None``, and the caller falls back to its older paths.
    Schema validity is not semantic validity: the caller still validates
    every field it reads.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


REVIEW_TOOL_NAME = _REVIEW_TOOL_NAME
CROSS_CHECK_TOOL_NAME = _CROSS_CHECK_TOOL_NAME
VERIFICATION_TOOL_NAME = _VERIFICATION_TOOL_NAME
TRIAGE_TOOL_NAME = _TRIAGE_TOOL_NAME
RESEARCH_TOOL_NAME = _RESEARCH_TOOL_NAME
COMPLIANCE_TOOL_NAME = _COMPLIANCE_TOOL_NAME
DRAWING_IMPACT_TOOL_NAME = _DRAWING_IMPACT_TOOL_NAME
COORDINATION_TOOL_NAME = _COORDINATION_TOOL_NAME
