"""EX-02: which output mechanism each consumer uses, which it could use, and
how often its output failed to parse.

The decision record is ``plans/experiments/EX-02-schema-constrained-outputs.md``.

Three mechanisms are kept apart throughout, because one does not imply another:

* **strict tool arguments** (``strict: true`` on a tool): the payload of a tool
  call matches its schema. It does not make the call happen.
* **forced tool invocation** (``tool_choice`` ``{"type": "tool", ...}``): the
  model must call that tool. It does not constrain anything the model writes
  instead of a call, because there is no "instead".
* **constrained final response** (``output_config.format`` with a
  ``json_schema``): the response's text is JSON matching the schema. There is
  no tool call at all.

**This module makes no API call.** It reads the app's own request builders and
a diagnostics summary:

* :func:`consumer_inventory` — every consumer that parses model output, its
  current request shape (model, thinking, custom tool, strict, tool_choice,
  server tools, citations), and each mechanism's state with the reason.
* :func:`review_arm_requests` — the per-spec review's request under each arm
  of the default-off switch ``SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT``, and what
  changes between arms.
* :func:`measure_parse_outcomes` — from a diagnostics summary (the dict
  ``DiagnosticsReport.summary()`` returns, which ``scripts/recover_batch.py
  --diagnostics-json`` saves), how many review attempts parsed, through which
  channel, and how many did not; plus the verifier's classified failures.

Offline use::

    python -m evals.structured_outputs [--module california_k12_mep]
        [--diagnostics summary.json]

prints the inventory, the review arms, the protocol, and (with a summary) the
measured outcomes, as JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from src.core import api_config
from src.review import structured_schemas

#: The switch value each review arm sets (``None`` unsets it: the baseline).
ARM_SWITCH_VALUES: dict[str, str | None] = {
    structured_schemas.REVIEW_OUTPUT_TOOL_AUTO: None,
    structured_schemas.REVIEW_OUTPUT_FORCED_TOOL: structured_schemas.REVIEW_OUTPUT_FORCED_TOOL,
    structured_schemas.REVIEW_OUTPUT_JSON_SCHEMA: structured_schemas.REVIEW_OUTPUT_JSON_SCHEMA,
}

MECHANISM_STRICT = "strict_tool_arguments"
MECHANISM_FORCED = "forced_tool_invocation"
MECHANISM_FORMAT = "constrained_final_response"
MECHANISMS: tuple[str, ...] = (MECHANISM_STRICT, MECHANISM_FORCED, MECHANISM_FORMAT)

STATE_ON = "on"
STATE_OFF = "off"
STATE_EXPERIMENT = "experiment arm, off by default"
STATE_ELIGIBLE = "eligible, not tried"
STATE_EXCLUDED = "excluded"
STATE_NOT_NEEDED = "not needed"

#: The consumer the experiment starts with (see the decision record for why).
FIRST_CONSUMER = "review"


# --------------------------------------------------------------------------
# Switch handling
# --------------------------------------------------------------------------


@contextmanager
def review_output_constraint_switch(value: str | None) -> Iterator[None]:
    """Set ``SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT`` for a block, then restore it.

    ``None`` unsets it (the baseline). The builders read the switch at call
    time, so a block is enough to build one arm's requests.
    """
    name = structured_schemas.ENV_REVIEW_OUTPUT_CONSTRAINT
    previous = os.environ.get(name)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


# --------------------------------------------------------------------------
# Request shapes
# --------------------------------------------------------------------------


def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tool_type(tool: Mapping[str, Any]) -> str:
    return str(tool.get("type") or "custom")


def describe_shape(params: Mapping[str, Any]) -> dict[str, Any]:
    """The output-relevant parts of one request, as sent."""
    tools = list(params.get("tools") or [])
    server_tools = [_tool_type(t) for t in tools if t.get("type")]
    custom_tools = [t for t in tools if not t.get("type")]
    citations = any(
        isinstance(t.get("citations"), Mapping) and t["citations"].get("enabled")
        for t in tools
    )
    output_config = params.get("output_config") or {}
    output_format = output_config.get("format") if isinstance(output_config, Mapping) else None
    return {
        "model": params.get("model"),
        "thinking": params.get("thinking"),
        "effort": (output_config or {}).get("effort") if isinstance(output_config, Mapping) else None,
        "custom_tools": [str(t.get("name")) for t in custom_tools],
        "strict": {str(t.get("name")): bool(t.get("strict")) for t in custom_tools},
        "tool_choice": params.get("tool_choice"),
        "server_tools": server_tools,
        "citations_enabled": citations,
        "output_format": (
            {"type": output_format.get("type"), "schema_sha256": _digest(output_format.get("schema"))}
            if isinstance(output_format, Mapping)
            else None
        ),
    }


def _review_params(*, module_id: str | None = None) -> dict[str, Any]:
    from src.modules.registry import get_module
    from src.review.review_request_builder import ReviewRequestSpec, build_review_request

    module = get_module(module_id)
    spec = ReviewRequestSpec(
        spec_content=(
            "PART 1 GENERAL\n1.01 SUMMARY\nA. Provide the specified piping system."
        ),
        filename="230500.docx",
        model=api_config.REVIEW_MODEL_DEFAULT,
        cycle=module.cycle,
    )
    return dict(build_review_request(spec).params)


def _verification_params() -> dict[str, Any]:
    from src.core.code_cycles import DEFAULT_CYCLE
    from src.review.reviewer import Finding
    from src.verification.verification_routing import build_verification_request, select_routing
    from src.verification.verifier import (
        _build_verification_prompt,
        _get_verification_system_prompt,
    )

    finding = Finding(
        severity="HIGH",
        fileName="230500.docx",
        section="1.01",
        issue="The pipe test pressure is stated as 50 psi; the plumbing code requires 100 psi.",
        actionType="EDIT",
        existingText="Test at 50 psi.",
        replacementText="Test at 100 psi.",
        codeReference="CPC 609.4",
        confidence=0.8,
    )
    decision = select_routing(finding, cycle=DEFAULT_CYCLE)
    prompt = _build_verification_prompt(
        finding, cycle=DEFAULT_CYCLE, include_verdict_tool=decision.include_verdict_tool
    )
    system_prompt = _get_verification_system_prompt(
        DEFAULT_CYCLE, include_verdict_tool=decision.include_verdict_tool
    )
    return dict(build_verification_request(decision, prompt=prompt, system_prompt=system_prompt).params)


def _phase_params(*, model: str, phase: str, tools: list[dict], tool_choice: dict | None) -> dict[str, Any]:
    """A phase that assembles its request inline: the same parts it uses."""
    params: dict[str, Any] = {"model": model, "tools": tools}
    if tool_choice is not None:
        params["tool_choice"] = tool_choice
    api_config.apply_thinking_config(params, model=model, phase=phase)
    api_config.apply_effort_config(params, model=model, phase=phase)
    return params


def _consumer_params() -> dict[str, tuple[str, dict[str, Any]]]:
    """``{consumer: (source, params)}`` for every consumer, switch unset."""
    s = structured_schemas
    cross_model = api_config.CROSS_CHECK_MODEL_DEFAULT
    compliance_model = api_config.COMPLIANCE_MODEL_DEFAULT
    impact_model = api_config.DRAWING_IMPACT_MODEL_DEFAULT
    research_model = api_config.RESEARCH_MODEL_DEFAULT
    triage_model = api_config.TRIAGE_MODEL_DEFAULT
    with review_output_constraint_switch(None):
        return {
            "review": ("review_request_builder.build_review_request", _review_params()),
            "cross_check": (
                "cross_check_findings_tool + cross_check_tool_choice (the parts cross_checker sends)",
                _phase_params(
                    model=cross_model,
                    phase=api_config.PHASE_CROSS_CHECK,
                    tools=[s.cross_check_findings_tool(model=cross_model)],
                    tool_choice=s.cross_check_tool_choice(),
                ),
            ),
            "compliance": (
                "compliance_findings_tool + compliance_tool_choice (the parts compliance_checker sends)",
                _phase_params(
                    model=compliance_model,
                    phase=api_config.PHASE_COMPLIANCE,
                    tools=[s.compliance_findings_tool(model=compliance_model)],
                    tool_choice=s.compliance_tool_choice(),
                ),
            ),
            "drawing_impact": (
                "drawing_impact_tool + drawing_impact_tool_choice (the parts impact_synthesizer sends)",
                _phase_params(
                    model=impact_model,
                    phase=api_config.PHASE_DRAWING_IMPACT,
                    tools=[s.drawing_impact_tool(model=impact_model)],
                    tool_choice=s.drawing_impact_tool_choice(),
                ),
            ),
            "research": (
                "web_search + web_fetch + requirements_research_tool, no tool_choice (requirements_research)",
                _phase_params(
                    model=research_model,
                    phase=api_config.PHASE_RESEARCH,
                    tools=[
                        api_config.build_web_search_tool(max_uses=5),
                        api_config.build_web_fetch_tool(max_uses=3),
                        s.requirements_research_tool(model=research_model),
                    ],
                    tool_choice=None,
                ),
            ),
            "verification": (
                "verification_routing.build_verification_request (a HIGH finding with a code reference)",
                _verification_params(),
            ),
            "triage": (
                "triage_classifications_tool + triage_tool_choice (the parts triage sends)",
                _phase_params(
                    model=triage_model,
                    phase=api_config.PHASE_TRIAGE,
                    tools=[s.triage_classifications_tool(model=triage_model)],
                    tool_choice=s.triage_tool_choice(model=triage_model),
                ),
            ),
        }


_FAILURE_COST = {
    "review": (
        "A parse failure leaves a spec with zero findings and pays for a full repair "
        "request (128k output cap; a second batch cycle on the batch transport); while "
        "the repair is pending, every dependent stage waits. One request per spec."
    ),
    "cross_check": (
        "One paid parse-error re-request, then a failed pass for the module (or chunk). "
        "One request per module or chunk."
    ),
    "compliance": (
        "The tagged-JSON fallback, then a failed pass; coverage completeness reports the "
        "gap. One request per module or chunk."
    ),
    "drawing_impact": "The tagged-JSON fallback, then an amber note in the report. One request per run.",
    "research": (
        "The tagged-JSON fallback, then a failed dimension; a run continues while one "
        "dimension succeeds. One request per dimension."
    ),
    "verification": (
        "Classified as an operational failure (plan WP-10: no_verdict / malformed_verdict), "
        "never cached or shared; the finding reads VERIFICATION_FAILED."
    ),
    "triage": (
        "Every finding in the chunk falls back to web verification (the cost triage exists "
        "to avoid). Closed on the default model by the forced tool."
    ),
}


def _mechanism_states(consumer: str, shape: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    strict_values = list(shape["strict"].values())
    strict = (
        {"state": STATE_ON, "reason": "strict: true on the consumer's submit tool."}
        if strict_values and all(strict_values)
        else {"state": STATE_OFF, "reason": "The submit tool is sent without strict: true."}
    )
    choice = shape.get("tool_choice") or {}
    server = bool(shape["server_tools"])
    if choice.get("type") == "tool":
        forced = {"state": STATE_ON, "reason": "tool_choice forces the submit tool (no thinking on this request)."}
    elif server:
        forced = {
            "state": STATE_EXCLUDED,
            "reason": (
                "The model must search before it submits, so forcing the submit tool would "
                "skip the search; and the web tools' dynamic filtering rejects any tool_choice "
                "option beyond auto (a 400 on disable_parallel_tool_use)."
            ),
        }
    elif consumer == FIRST_CONSUMER:
        forced = {
            "state": STATE_EXPERIMENT,
            "reason": (
                "SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT=forced_tool. Documented as accepted with "
                "adaptive thinking on the review models; never sent from this repository."
            ),
        }
    else:
        forced = {
            "state": STATE_ELIGIBLE,
            "reason": "Same shape as the review (one custom tool, no server tools); not the first consumer.",
        }
    if server:
        constrained = {
            "state": STATE_EXCLUDED,
            "reason": (
                "Structured outputs are documented as incompatible with citations (a 400), "
                "and this request carries web tools"
                + (" with citations enabled on web_fetch" if shape["citations_enabled"] else "")
                + "; the verifier's native citations (plan WP-16) would also be lost."
            ),
        }
    elif choice.get("type") == "tool":
        constrained = {
            "state": STATE_NOT_NEEDED,
            "reason": "The forced tool already closes the text detour; not tried.",
        }
    elif consumer == FIRST_CONSUMER:
        constrained = {
            "state": STATE_EXPERIMENT,
            "reason": (
                "SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT=json_schema. The provider's pages disagree "
                "on JSON outputs with thinking; unverified until a live probe sends it."
            ),
        }
    else:
        constrained = {
            "state": STATE_ELIGIBLE,
            "reason": "Same shape as the review (one custom tool, no server tools); not the first consumer.",
        }
    return {MECHANISM_STRICT: strict, MECHANISM_FORCED: forced, MECHANISM_FORMAT: constrained}


def consumer_inventory() -> list[dict[str, Any]]:
    """One row per consumer that parses model output (see the module docstring)."""
    rows = []
    for consumer, (source, params) in _consumer_params().items():
        shape = describe_shape(params)
        rows.append(
            {
                "consumer": consumer,
                "source": source,
                "first_consumer": consumer == FIRST_CONSUMER,
                "shape": shape,
                "mechanisms": _mechanism_states(consumer, shape),
                "failure_cost": _FAILURE_COST[consumer],
            }
        )
    return rows


def review_arm_requests(
    *, module_id: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """The review request under each arm, and what differs from the baseline.

    ``model`` defaults to the review model. On a model whose capability record
    does not vouch for an arm, that arm is built as the default shape (and
    says so in ``built_as``): Sonnet 5.5 (the default since 2026-10-08) and
    Opus 5.5 (the default before it) reject forced tool use, so the
    ``forced_tool`` arm changes nothing on either, and measuring that arm
    needs a model that accepts it.
    """
    from src.review.review_request_builder import ReviewRequestSpec, build_review_request
    from src.modules.registry import get_module

    module = get_module(module_id)
    model = model or api_config.REVIEW_MODEL_DEFAULT
    arms: dict[str, Any] = {}
    for arm, value in ARM_SWITCH_VALUES.items():
        with review_output_constraint_switch(value):
            built = build_review_request(
                ReviewRequestSpec(
                    spec_content="PART 1 GENERAL\n1.01 SUMMARY\nA. Provide the specified piping system.",
                    filename="230500.docx",
                    model=model,
                    cycle=module.cycle,
                )
            )
        params = built.params
        arms[arm] = {
            "built_as": built.output_mode,
            "shape": describe_shape(params),
            "system_prompt_sha256": _digest(built.system_prompt),
            "user_message_sha256": _digest(built.user_message),
            "_params": params,
        }
    base = arms[structured_schemas.REVIEW_OUTPUT_TOOL_AUTO]["_params"]
    for arm, entry in arms.items():
        params = entry.pop("_params")
        entry["fields_changed"] = sorted(
            key for key in set(params) | set(base) if params.get(key) != base.get(key)
        )
    return {
        "module_id": module.module_id,
        "model": model,
        "arms": arms,
        "configuration_sha256": {arm: _digest(entry) for arm, entry in arms.items()},
    }


# --------------------------------------------------------------------------
# Measurement (from a diagnostics summary)
# --------------------------------------------------------------------------

#: Review outcome tags that mean the attempt's response was read but not parsed.
REVIEW_UNPARSED_OUTCOMES = ("parse_error", "incomplete", "refusal")

#: Verifier terminal reasons that mean the reply could not be read as a verdict.
VERIFICATION_PARSE_FAILURES = ("no_verdict", "malformed_verdict")


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def measure_parse_outcomes(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Parse outcomes recorded in one diagnostics summary.

    ``review`` reads ``review_parse_outcomes`` (every review attempt, primary
    and repair, deduplicated): the attempts whose response was read, the ones
    that parsed, the ones that did not (a parse error, a truncation, a
    refusal — each rate is over the attempts that returned a response), and,
    among the parsed ones, how many the tagged-JSON fallback carried. A
    summary written before the rollup existed says so instead of reading as
    zero failures. ``verification`` reads the verifier's classified terminal
    reasons. Nothing else is measurable from a summary today: cross-check,
    compliance, drawing impact, research, and triage record their parse path
    only in the trace or the log.
    """
    out: dict[str, Any] = {}
    review = summary.get("review_parse_outcomes")
    if not isinstance(review, Mapping):
        out["review"] = {"recorded": False, "reason": "summary has no review_parse_outcomes (written before plan EX-02)"}
    else:
        by_outcome = {str(k): int(v) for k, v in (review.get("by_outcome") or {}).items()}
        by_channel = {str(k): int(v) for k, v in (review.get("by_output_channel") or {}).items()}
        parsed = by_outcome.get("ok", 0)
        unparsed = {tag: by_outcome.get(tag, 0) for tag in REVIEW_UNPARSED_OUTCOMES}
        responded = parsed + sum(unparsed.values())
        out["review"] = {
            "recorded": True,
            "attempts": int(review.get("attempts") or 0),
            "responses": responded,
            "parsed": parsed,
            "by_outcome": by_outcome,
            "by_output_channel": by_channel,
            "unparsed_rate": _rate(sum(unparsed.values()), responded),
            "parse_error_rate": _rate(unparsed["parse_error"], responded),
            "truncation_rate": _rate(unparsed["incomplete"], responded),
            "refusal_rate": _rate(unparsed["refusal"], responded),
            "text_fallback_rate": _rate(by_channel.get("text", 0), parsed),
            "unrecorded_channel": by_channel.get("unrecorded", 0),
        }
    retry = summary.get("retry_stats") or {}
    terminal = {str(k): int(v) for k, v in (retry.get("by_terminal_reason") or {}).items()}
    verdicts = summary.get("verification_verdicts") or {}
    verified = sum(int(v) for v in verdicts.values()) if isinstance(verdicts, Mapping) else 0
    failures = {reason: terminal.get(reason, 0) for reason in VERIFICATION_PARSE_FAILURES}
    out["verification"] = {
        "results": verified,
        "unreadable_verdicts": failures,
        "unreadable_rate": _rate(sum(failures.values()), verified),
    }
    return out


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------

EVALUATION_PROTOCOL: dict[str, str] = {
    "status": (
        "NOT RUN. No live request has been made for this experiment. The owner chose an "
        "offline-only session for S21 (2026-09-29). No measured parse-failure rate exists "
        "for any consumer, and nothing in this module or its decision record is a measured "
        "improvement."
    ),
    "authorization": (
        "A live comparison needs its own authorization: a maximum spend, an API key in the "
        "environment, a fixed corpus, and a stopping rule agreed in advance. The presence of "
        "a key is not authorization to spend."
    ),
    "step_0_capability_probe": (
        "Before any comparison, send each arm's exact production shape once, small: "
        "pytest -m network tests/test_network_smoke.py -k review_output_constraint. A 400 on "
        "an arm ends that arm; record the error. The docs disagree on JSON outputs with "
        "thinking, and forced tool use with adaptive thinking has never been sent from here."
    ),
    "step_1_baseline_rate": (
        "Measure the baseline's parse-failure rate first, from ordinary runs at no extra "
        "spend: every run's diagnostics summary now carries review_parse_outcomes "
        "(measure_parse_outcomes reads it). If the review's unparsed and text-fallback rates "
        "are both zero over a corpus large enough to matter, stop: there is nothing for a "
        "constraint to fix, and the decision is reject."
    ),
    "arms": (
        "tool_auto (switch unset), forced_tool, json_schema. One change at a time: same "
        "corpus, module, review model, effort, transport, worker count, and Project Context "
        "across arms. forced_tool changes only tool_choice; json_schema changes the output "
        "channel and the four prompt lines that name it."
    ),
    "corpus": (
        "The labeled specs in evals/labeled_specs.py plus at least one module's real "
        "package, recorded by SHA-256 of each spec's extracted content. Include specs that "
        "are large enough to approach the output cap, since truncation is one of the "
        "failures being counted."
    ),
    "both_transports": (
        "Message Batches and real time. Batch: submit under one arm and collect under "
        "another once, to show a pending batch stays collectable across a switch change."
    ),
    "metrics": (
        "Per arm: unparsed rate (parse_error, incomplete, refusal) and text-fallback rate "
        "from review_parse_outcomes; repair rate and total attempts; latency; estimated cost "
        "by category (plan WP-15). Quality: finding count, severity mix, the share of "
        "findings demoted to REPORT_ONLY, anchor-validation demotions, unsupported findings "
        "(adjudicated on the labeled specs), and a sample read side by side. A constrained "
        "response that parses but omits findings is a regression, not a success."
    ),
    "promotion": (
        "Enable an arm by default only if it shows fewer unparseable outputs without more "
        "omission, more unsupported findings, or less useful evidence, and without higher "
        "retry, repair, latency, or cost. Promotion is per consumer; the next consumer "
        "(compliance, then cross-check) needs its own measurement."
    ),
}


def capture(*, module_id: str | None = None, diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything the CLI prints, as one JSON-friendly dict."""
    result: dict[str, Any] = {
        "inventory": consumer_inventory(),
        "review_arms": review_arm_requests(module_id=module_id),
        "evaluation_protocol": EVALUATION_PROTOCOL,
    }
    if diagnostics is not None:
        result["measured"] = measure_parse_outcomes(diagnostics)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--module", default=None, help="module id for the review arms")
    parser.add_argument(
        "--diagnostics",
        default=None,
        help="a diagnostics summary JSON (scripts/recover_batch.py --diagnostics-json)",
    )
    ns = parser.parse_args(argv)
    diagnostics = None
    if ns.diagnostics:
        diagnostics = json.loads(Path(ns.diagnostics).read_text(encoding="utf-8"))
    json.dump(capture(module_id=ns.module, diagnostics=diagnostics), sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
