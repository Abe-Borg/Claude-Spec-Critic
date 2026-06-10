"""Tool-output schemas for review, cross-check, verification, and triage.

* Every review / cross-check / verification call exposes a single custom
  tool whose ``input_schema`` matches the desired payload shape.
* ``tool_choice`` is ``{"type": "auto"}`` because the API rejects forcing
  tool_choice when adaptive thinking is enabled.
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

The schemas stay inside the strict-mode supported subset: every property
required, optionals nullable, ``additionalProperties: false``, no
``oneOf``/``anyOf``, no numerical or string-length constraints.
"""
from __future__ import annotations

import os
from typing import Any


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
                "0..1 confidence in the finding. >=0.85: directly evidenced by "
                "quoted spec text and unambiguous. 0.60-0.84: well-supported but "
                "contextual or interpretive. <0.60: weak or indirect evidence."
            ),
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
        # the text fallback parser. Nullable so UNVERIFIED/DISPUTED
        # verdicts (which have no supporting quote) still satisfy
        # strict-mode constrained sampling.
        "source_quote": {
            "type": ["string", "null"],
            "description": (
                "Verbatim text from a web_search result snippet that supports "
                "this verdict — the evidence you actually read, not a "
                "paraphrase. REQUIRED non-empty for CONFIRMED and CORRECTED "
                "verdicts; optional/null for UNVERIFIED and DISPUTED. If no "
                "snippet supports the verdict, you do not have grounded "
                "evidence — return UNVERIFIED."
            ),
        },
    },
}


# ---------------------------------------------------------------------------
# Tool builders. Each returns a single tool dict that callers pass via
# ``tools=[...]`` together with ``tool_choice={"type": "auto"}`` (forcing
# tool_choice is incompatible with adaptive thinking; see module docstring).
# ---------------------------------------------------------------------------

_REVIEW_TOOL_NAME = "submit_review_findings"
_CROSS_CHECK_TOOL_NAME = "submit_cross_check_findings"
_VERIFICATION_TOOL_NAME = "submit_verification_verdict"
_TRIAGE_TOOL_NAME = "submit_triage_classifications"


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


def triage_tool_choice() -> dict[str, Any]:
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
    # Any forcing tool_choice ({"type": "tool", "name": ...} or {"type": "any"})
    # is rejected by the API when ``thinking`` is enabled. Use {"type": "auto"}
    # so adaptive thinking is preserved; with only one tool exposed and the
    # system prompt instructing the model to call it, the tool is reliably —
    # but not contractually — invoked. The tagged-JSON text parser is the
    # documented fallback for the path where the model returns text instead.
    return {"type": "auto", "disable_parallel_tool_use": True}

def cross_check_tool_choice() -> dict[str, Any]:
    return {"type": "auto", "disable_parallel_tool_use": True}

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
TRIAGE_TOOL_NAME = _TRIAGE_TOOL_NAME
