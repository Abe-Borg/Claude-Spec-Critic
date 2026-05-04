"""Structured-output JSON schemas for review, cross-check, and verification.

Phase 2.4 / 2.5 (audit Sections 6.4–6.5): replace fragile ``<findings_json>``
tag-and-regex parsing with Anthropic tool-use schemas. The model is given a
single tool whose ``input_schema`` matches the existing finding shape, and
``tool_choice`` forces it to call that tool — eliminating the parse-failure
class entirely. The text-parsing path stays as a fallback for the rare
case where the model returns no tool_use block (e.g., refusal).

Toggle:
    SPEC_CRITIC_STRUCTURED_OUTPUTS — "0" disables; default on.
"""
from __future__ import annotations

import os
from typing import Any


def structured_outputs_enabled() -> bool:
    """Whether to force structured tool-use outputs on review/cross-check/verification.

    Default on. Set ``SPEC_CRITIC_STRUCTURED_OUTPUTS=0`` to revert to the
    previous tagged-JSON-in-text path (kept as a fallback inside the
    parsers).
    """
    return os.environ.get("SPEC_CRITIC_STRUCTURED_OUTPUTS", "1") != "0"


# ---------------------------------------------------------------------------
# Shared finding object schema (review + cross-check)
# ---------------------------------------------------------------------------

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
            "enum": ["ADD", "EDIT", "DELETE"],
            "description": "Whether the fix is to add, edit, or delete text.",
        },
        "existingText": {
            "type": ["string", "null"],
            "description": "For EDIT/DELETE: the exact verbatim text in the spec. For ADD: nullable.",
        },
        "replacementText": {
            "type": ["string", "null"],
            "description": "Suggested replacement / new text. For DELETE: nullable.",
        },
        "codeReference": {
            "type": ["string", "null"],
            "description": "Applicable code clause or standard, e.g. 'CBC §1705.13'.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "0..1 confidence in the finding.",
        },
        "anchorText": {
            "type": ["string", "null"],
            "description": "ADD only: verbatim nearby paragraph used to locate the insertion point.",
        },
        "insertPosition": {
            "type": ["string", "null"],
            "enum": ["before", "after", None],
            "description": "ADD only: insert before or after the anchor.",
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
            "items": _FINDING_OBJECT_SCHEMA,
            "description": "Zero or more cross-spec coordination findings.",
        },
    },
}


VERIFICATION_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "explanation", "sources", "correction"],
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
    },
}


# ---------------------------------------------------------------------------
# Tool builders. Each returns a single tool dict that callers pass via
# ``tools=[...]`` together with ``tool_choice={"type": "tool", "name": ...}``
# to force the model to emit a structured object.
# ---------------------------------------------------------------------------

_REVIEW_TOOL_NAME = "submit_review_findings"
_CROSS_CHECK_TOOL_NAME = "submit_cross_check_findings"
_VERIFICATION_TOOL_NAME = "submit_verification_verdict"


def _strict_enabled() -> bool:
    """Whether to attach ``"strict": true`` to tool definitions.

    Strict mode uses grammar-constrained sampling to guarantee tool inputs
    match the schema, eliminating the parse-failure tail. On by default;
    set ``SPEC_CRITIC_STRICT_TOOLS=0`` to disable (e.g. if a future schema
    construct is unsupported by the strict validator).
    """
    return os.environ.get("SPEC_CRITIC_STRICT_TOOLS", "1") != "0"


def review_findings_tool() -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _REVIEW_TOOL_NAME,
        "description": (
            "Submit the structured per-spec review output. Use this tool exactly "
            "once. Return all findings (zero or more) in the ``findings`` array."
        ),
        "input_schema": REVIEW_FINDINGS_SCHEMA,
    }
    if _strict_enabled():
        tool["strict"] = True
    return tool


def cross_check_findings_tool() -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _CROSS_CHECK_TOOL_NAME,
        "description": (
            "Submit the structured cross-spec coordination output. Use this "
            "tool exactly once. ``findings`` may be empty when coordination is "
            "adequate."
        ),
        "input_schema": CROSS_CHECK_FINDINGS_SCHEMA,
    }
    if _strict_enabled():
        tool["strict"] = True
    return tool


def verification_verdict_tool() -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": _VERIFICATION_TOOL_NAME,
        "description": (
            "After consulting web search, submit the structured verification "
            "verdict for the finding under review. Use this tool exactly once "
            "as the final step of your turn."
        ),
        "input_schema": VERIFICATION_VERDICT_SCHEMA,
    }
    if _strict_enabled():
        tool["strict"] = True
    return tool


def review_tool_choice() -> dict[str, Any]:
    # Force the model to call submit_review_findings. Modern Anthropic models
    # accept {"type": "tool", "name": ...} together with adaptive thinking;
    # the prior {"type": "any"} workaround is no longer required.
    return {
        "type": "tool",
        "name": _REVIEW_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }


def cross_check_tool_choice() -> dict[str, Any]:
    return {
        "type": "tool",
        "name": _CROSS_CHECK_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }


# Verification cannot use a forcing tool_choice because the model needs to
# call ``web_search`` first; instead the prompt instructs the model to emit
# the verdict tool as the final step. ``any`` lets it pick web_search early.


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


def extract_tool_use_block(response: object, tool_name: str) -> dict[str, Any] | None:
    """Pull the matching ``tool_use`` block's ``input`` off a response.

    Returns the input dict if found, otherwise None. Tolerates SDK
    Pydantic objects, plain dicts, and Pydantic-model ``input`` payloads
    (the batch retrieval path can return any of the three).
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
        if bname != tool_name:
            continue
        binput = getattr(block, "input", None)
        if binput is None and isinstance(block, dict):
            binput = block.get("input")
        coerced = _coerce_to_dict(binput)
        if coerced is not None:
            return coerced
    return None


REVIEW_TOOL_NAME = _REVIEW_TOOL_NAME
CROSS_CHECK_TOOL_NAME = _CROSS_CHECK_TOOL_NAME
VERIFICATION_TOOL_NAME = _VERIFICATION_TOOL_NAME
