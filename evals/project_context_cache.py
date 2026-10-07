"""EX-01: the request layout Project Context caching would change, and the
protocol for measuring it.

The decision record is ``plans/experiments/EX-01-project-context-caching.md``.

**This module measures request shape, not money.** It builds requests with the
same builders the app sends them through and reports what the provider's cache
would see: the order of tools, system blocks, and message blocks, where each
``cache_control`` breakpoint sits and with which TTL, how many of the four
breakpoint slots each phase uses (a request-level automatic breakpoint on a
``pause_turn`` resume included), and how far a set of review requests stays
byte-identical. It makes no API call. Whether the candidate saves money is a
question only cache reads and writes observed on real requests can answer; see
:data:`EVALUATION_PROTOCOL` for what such a run must record.

The candidate is the default-off switch ``SPEC_CRITIC_PROJECT_CONTEXT_CACHE``
(``api_config.project_context_cache_control``): with it on, a review that
carries a Project Context is sent as two text blocks, the first ending with the
``<project_context>`` block and carrying a breakpoint.

Offline use::

    python -m evals.project_context_cache --spec a.docx --spec b.docx \\
        --context-file context.txt [--module california_k12_mep] [--ttl 1h]

prints the layout of each review request under the baseline and the candidate,
their shared prefix, and the breakpoint budget of every phase, as JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from src.core import api_config
from src.core.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    price_for,
)

#: The provider's limit on cache breakpoints in one request (prompt-caching
#: docs, rechecked 2026-09-29). A request-level automatic ``cache_control``
#: takes one of these slots.
MAX_BREAKPOINTS = 4

#: The switch values an evaluation arm sets (see ``api_config``).
ARM_SWITCH_VALUES: dict[str, str | None] = {
    "baseline": None,
    "candidate_1h": "1h",
    "candidate_5m": "5m",
}

_TTL_RANK = {"1h": 2, "5m": 1}


# --------------------------------------------------------------------------
# Switch handling
# --------------------------------------------------------------------------


@contextmanager
def project_context_cache_switch(value: str | None) -> Iterator[None]:
    """Set ``SPEC_CRITIC_PROJECT_CONTEXT_CACHE`` for a block, then restore it.

    ``None`` unsets it (the baseline). The builders read the switch at call
    time, so a block is enough to build one arm's requests.
    """
    name = api_config.ENV_PROJECT_CONTEXT_CACHE
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
# Layout
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Breakpoint:
    """One cache breakpoint, in the provider's prefix order."""

    position: str  # "tools[0]", "system[0]", "messages[0].content[0]", "request"
    ttl: str  # "1h" or "5m" (an absent ttl is the five-minute default)
    automatic: bool = False  # the request-level field, not a block marker


def _ttl(cache_control: Mapping[str, Any]) -> str:
    return str(cache_control.get("ttl") or "5m")


def _as_blocks(content: Any) -> list[Any]:
    if isinstance(content, list):
        return list(content)
    return [content]


def breakpoints(params: Mapping[str, Any]) -> list[Breakpoint]:
    """Every breakpoint ``params`` sets, in prefix order (tools, system, messages).

    A top-level ``cache_control`` is the provider's automatic breakpoint: it
    lands on the last eligible block and takes a slot, so it is listed last.
    """
    found: list[Breakpoint] = []
    for i, tool in enumerate(params.get("tools") or []):
        if isinstance(tool, Mapping) and isinstance(tool.get("cache_control"), Mapping):
            found.append(Breakpoint(f"tools[{i}]", _ttl(tool["cache_control"])))
    system = params.get("system")
    for i, block in enumerate(_as_blocks(system) if system is not None else []):
        if isinstance(block, Mapping) and isinstance(block.get("cache_control"), Mapping):
            found.append(Breakpoint(f"system[{i}]", _ttl(block["cache_control"])))
    for m, message in enumerate(params.get("messages") or []):
        content = message.get("content") if isinstance(message, Mapping) else None
        for i, block in enumerate(_as_blocks(content)):
            if isinstance(block, Mapping) and isinstance(block.get("cache_control"), Mapping):
                found.append(
                    Breakpoint(f"messages[{m}].content[{i}]", _ttl(block["cache_control"]))
                )
    top = params.get("cache_control")
    if isinstance(top, Mapping):
        found.append(Breakpoint("request", _ttl(top), automatic=True))
    return found


def ttl_order_problems(points: Sequence[Breakpoint]) -> list[str]:
    """Explicit breakpoints whose TTL outlives one before it.

    The provider requires longer TTLs first: a one-hour entry may not follow a
    five-minute one. The automatic breakpoint lands at the end with its own
    TTL, so only block markers are checked against each other and against it.
    """
    problems: list[str] = []
    shortest = None
    for point in points:
        rank = _TTL_RANK.get(point.ttl, 1)
        if shortest is not None and rank > _TTL_RANK[shortest.ttl]:
            problems.append(
                f"{point.position} ({point.ttl}) follows {shortest.position} ({shortest.ttl})"
            )
        if shortest is None or rank < _TTL_RANK[shortest.ttl]:
            shortest = point
    return problems


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _local_tokens(text: str) -> int | None:
    """Unpadded local cl100k count, or ``None`` when the tokenizer is absent."""
    try:
        from src.core.tokenizer import count_tokens

        return int(count_tokens(text))
    except Exception:  # noqa: BLE001 — sizes are optional; the rank file may be missing
        return None


def _text_of(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, Mapping):
        if block.get("type") == "text":
            return str(block.get("text") or "")
        stripped = {k: v for k, v in block.items() if k != "cache_control"}
        return json.dumps(stripped, sort_keys=True, ensure_ascii=False, default=str)
    return str(block)


def _segment(label: str, block: Any, *, with_tokens: bool) -> dict[str, Any]:
    text = _text_of(block)
    entry: dict[str, Any] = {
        "position": label,
        "chars": len(text),
        "utf8_bytes": len(text.encode("utf-8")),
        "sha256_12": _digest(text),
    }
    if isinstance(block, Mapping):
        if block.get("name"):
            entry["name"] = block["name"]
        if isinstance(block.get("cache_control"), Mapping):
            entry["cache_control_ttl"] = _ttl(block["cache_control"])
    if with_tokens:
        entry["local_cl100k_tokens"] = _local_tokens(text)
    return entry


def prefix_segments(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(position, text)`` in the provider's prefix order, markers dropped.

    Tools (as JSON), then system blocks, then each message's content blocks —
    the order the cache hierarchy reads a request in. Two requests share a
    cached prefix only as far as these agree, and only where the settings
    that sit ahead of the messages (model, thinking, effort, ``tool_choice``)
    agree too; :func:`shared_prefix` checks those separately.
    """
    segments: list[tuple[str, str]] = []
    for i, tool in enumerate(params.get("tools") or []):
        segments.append((f"tools[{i}]", _text_of(tool)))
    system = params.get("system")
    for i, block in enumerate(_as_blocks(system) if system is not None else []):
        segments.append((f"system[{i}]", _text_of(block)))
    for m, message in enumerate(params.get("messages") or []):
        content = message.get("content") if isinstance(message, Mapping) else None
        for i, block in enumerate(_as_blocks(content)):
            segments.append((f"messages[{m}].content[{i}]", _text_of(block)))
    return segments


_SETTINGS_AHEAD_OF_MESSAGES = ("model", "thinking", "output_config", "tool_choice")


def shared_prefix(requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How far a set of requests stays identical, in prefix order.

    Returns the segments every request shares, the first segment where they
    part and the length of the common text inside it, and whether the
    settings that invalidate a messages cache when they change agree.
    """
    if not requests:
        return {"requests": 0}
    settings_agree = all(
        all(r.get(key) == requests[0].get(key) for r in requests)
        for key in _SETTINGS_AHEAD_OF_MESSAGES
    )
    per_request = [prefix_segments(r) for r in requests]
    shared: list[str] = []
    shared_chars = 0
    divergence: dict[str, Any] | None = None
    for index in range(min(len(s) for s in per_request)):
        labels = {s[index][0] for s in per_request}
        texts = [s[index][1] for s in per_request]
        if len(labels) == 1 and all(t == texts[0] for t in texts):
            shared.append(per_request[0][index][0])
            shared_chars += len(texts[0])
            continue
        common = os.path.commonprefix(texts)
        divergence = {
            "position": per_request[0][index][0] if len(labels) == 1 else sorted(labels),
            "common_chars_in_segment": len(common),
        }
        break
    if divergence is None and len({len(s) for s in per_request}) > 1:
        divergence = {"position": "segment count differs", "common_chars_in_segment": 0}
    return {
        "requests": len(requests),
        "settings_ahead_of_messages_agree": settings_agree,
        "identical_segments": shared,
        "identical_segment_chars": shared_chars,
        "divergence": divergence,
    }


def describe_request(params: Mapping[str, Any], *, with_tokens: bool = False) -> dict[str, Any]:
    """The layout of one request: settings, segments, and breakpoints."""
    points = breakpoints(params)
    output_config = params.get("output_config") or {}
    return {
        "model": params.get("model"),
        "max_tokens": params.get("max_tokens"),
        "thinking": params.get("thinking"),
        "effort": output_config.get("effort") if isinstance(output_config, Mapping) else None,
        "tool_choice": params.get("tool_choice"),
        "service_tier": params.get("service_tier"),
        "segments": [
            _segment(label, block, with_tokens=with_tokens)
            for label, block in _labelled_blocks(params)
        ],
        "breakpoints": [
            {"position": p.position, "ttl": p.ttl, "automatic": p.automatic} for p in points
        ],
        "breakpoint_count": len(points),
        "within_breakpoint_limit": len(points) <= MAX_BREAKPOINTS,
        "ttl_order_problems": ttl_order_problems(points),
    }


def _labelled_blocks(params: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    for i, tool in enumerate(params.get("tools") or []):
        yield f"tools[{i}]", tool
    system = params.get("system")
    for i, block in enumerate(_as_blocks(system) if system is not None else []):
        yield f"system[{i}]", block
    for m, message in enumerate(params.get("messages") or []):
        content = message.get("content") if isinstance(message, Mapping) else None
        for i, block in enumerate(_as_blocks(content)):
            yield f"messages[{m}].content[{i}]", block


# --------------------------------------------------------------------------
# Review requests under each arm
# --------------------------------------------------------------------------


def review_requests(
    specs: Sequence[Any],
    *,
    project_context: str,
    module_id: str | None = None,
    model: str | None = None,
    switch: str | None = None,
    retry_instruction: str | None = None,
    realtime: bool = False,
) -> list[dict[str, Any]]:
    """The review request params the app would send for ``specs``.

    Built by ``build_review_request`` with the switch set to ``switch`` —
    the same path the batch submitter and the real-time runner take.
    ``realtime`` applies the real-time transport's two pins (no service tier,
    extended output off).
    """
    from src.core.api_config import REVIEW_MODEL_DEFAULT
    from src.modules.registry import DEFAULT_MODULE, require_module
    from src.review.review_request_builder import ReviewRequestSpec, build_review_request

    # An unknown id is an error here, not the registry's silent default: a
    # typo must not capture California's layout under another module's name.
    module = require_module(module_id) if module_id else DEFAULT_MODULE
    built: list[dict[str, Any]] = []
    with project_context_cache_switch(switch):
        for spec in specs:
            request = build_review_request(
                ReviewRequestSpec(
                    spec_content=spec.content,
                    filename=spec.filename,
                    model=model or REVIEW_MODEL_DEFAULT,
                    cycle=module.cycle,
                    project_context=project_context,
                    paragraph_map=spec.paragraph_map,
                    retry_instruction=retry_instruction,
                    force_allow_extended_output=False if realtime else None,
                    include_service_tier=False if realtime else None,
                )
            )
            built.append(request.params)
    return built


# --------------------------------------------------------------------------
# Breakpoint budget of every phase
# --------------------------------------------------------------------------


def _synthetic_spec(filename: str, text: str):
    from src.input.extractor import ExtractedSpec

    return ExtractedSpec(filename=filename, content=text, word_count=len(text.split()))


def _verification_requests(switch: str | None) -> dict[str, dict[str, Any]]:
    """The real-time verification request and its first resume, as sent.

    Mirrors ``verifier._run_verification_call``: the central builder, then
    ``apply_resume_cache_config`` once the conversation holds an assistant
    turn. The batch continuation (``assistant_content`` passed to the
    builder) carries no request-level field.
    """
    from src.core.api_config import apply_resume_cache_config
    from src.core.code_cycles import DEFAULT_CYCLE
    from src.review.reviewer import Finding
    from src.verification.verification_routing import (
        build_verification_request,
        select_routing,
    )
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
    with project_context_cache_switch(switch):
        decision = select_routing(finding, cycle=DEFAULT_CYCLE)
        prompt = _build_verification_prompt(
            finding, cycle=DEFAULT_CYCLE, include_verdict_tool=decision.include_verdict_tool
        )
        system_prompt = _get_verification_system_prompt(
            DEFAULT_CYCLE, include_verdict_tool=decision.include_verdict_tool
        )
        first = build_verification_request(decision, prompt=prompt, system_prompt=system_prompt)
        resume = dict(first.params)
        resume["messages"] = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": "Searching."}]},
        ]
        apply_resume_cache_config(resume, resume["messages"])
        batch_continuation = build_verification_request(
            decision,
            prompt=prompt,
            system_prompt=system_prompt,
            assistant_content=[{"type": "text", "text": "Searching."}],
            include_service_tier=True,
        )
    return {
        "verification (first call)": dict(first.params),
        "verification (real-time pause_turn resume)": resume,
        "verification (batch continuation wave)": dict(batch_continuation.params),
    }


def _policy_row(phase: str, *, resumes: bool) -> dict[str, Any]:
    """A phase without a public request builder: its breakpoints from policy."""
    policy = api_config.cache_policy_for(phase)
    explicit = int(policy.cache_system) + int(policy.cache_tools)
    return {
        "source": "cache policy (the phase builds its request inline)",
        "explicit_breakpoints": explicit,
        "automatic_breakpoint_on_resume": resumes,
        "breakpoint_count": explicit + (1 if resumes else 0),
        "carries_project_context": False,
    }


def phase_breakpoint_budget(switch: str | None = None) -> dict[str, dict[str, Any]]:
    """Breakpoint slots each phase's request uses, with the switch at ``switch``.

    Built from the real builders where a phase has one; the phases that
    assemble their request inline are read from the cache policy and the
    resume rule (``api_config.apply_resume_cache_config``), which is what their
    code applies.
    """
    from src.compliance.compliance_checker import build_compliance_request
    from src.cross_check.cross_checker import build_cross_check_request
    from src.research.requirements_research import RequirementsProfile, ResearchItem
    from src.review.review_request_builder import RETRY_TRUNCATED_REVIEW_INSTRUCTION

    context = "Project: Example.\nClient: Example Owner."
    specs = [
        _synthetic_spec("230500.docx", "SECTION 23 05 00\nPART 1 GENERAL\n1.01 SUMMARY\nA. Text."),
        _synthetic_spec("230593.docx", "SECTION 23 05 93\nPART 1 GENERAL\n1.01 SUMMARY\nA. Text."),
    ]
    rows: dict[str, dict[str, Any]] = {}

    def from_params(params: Mapping[str, Any], *, carries_context: bool) -> dict[str, Any]:
        points = breakpoints(params)
        return {
            "source": "request builder",
            "breakpoints": [
                {"position": p.position, "ttl": p.ttl, "automatic": p.automatic}
                for p in points
            ],
            "breakpoint_count": len(points),
            "within_breakpoint_limit": len(points) <= MAX_BREAKPOINTS,
            "ttl_order_problems": ttl_order_problems(points),
            "carries_project_context": carries_context,
        }

    rows["review (batch)"] = from_params(
        review_requests(specs[:1], project_context=context, switch=switch)[0],
        carries_context=True,
    )
    rows["review (real time)"] = from_params(
        review_requests(specs[:1], project_context=context, switch=switch, realtime=True)[0],
        carries_context=True,
    )
    rows["review repair"] = from_params(
        review_requests(
            specs[:1],
            project_context=context,
            switch=switch,
            retry_instruction=RETRY_TRUNCATED_REVIEW_INSTRUCTION,
        )[0],
        carries_context=True,
    )
    with project_context_cache_switch(switch):
        rows["cross-check"] = from_params(
            build_cross_check_request(specs, [], project_context=context),
            carries_context=True,
        )
        profile = RequirementsProfile(
            items=[
                ResearchItem(
                    item_id="r-1",
                    dimension_id="governing_codes",
                    topic="Sprinklers",
                    category="adopted_code",
                    requirement="Comply with the adopted fire code.",
                    grounded=True,
                    accepted_sources=["https://example.gov/code"],
                )
            ]
        )
        rows["compliance"] = from_params(
            build_compliance_request(specs, profile, [], project_context=context),
            carries_context=True,
        )
    for label, params in _verification_requests(switch).items():
        rows[label] = from_params(params, carries_context=False)
    rows["requirements research (first call)"] = _policy_row(
        api_config.PHASE_RESEARCH, resumes=False
    )
    rows["requirements research (pause_turn resume)"] = _policy_row(
        api_config.PHASE_RESEARCH, resumes=True
    )
    rows["drawing impact"] = _policy_row(api_config.PHASE_DRAWING_IMPACT, resumes=False)
    rows["verification triage"] = _policy_row(api_config.PHASE_TRIAGE, resumes=False)
    return rows


# --------------------------------------------------------------------------
# Cost arithmetic (for reading measurements, never a substitute for them)
# --------------------------------------------------------------------------


def _cache_read_multiplier(model: str | None) -> float:
    """The model's cache-read rate as a multiple of its input rate."""
    price = price_for(model) if model else None
    if price is None or not price.input_per_mtok:
        return CACHE_READ_MULTIPLIER
    return price.cache_read_rate_per_mtok / price.input_per_mtok


def break_even_write_share(ttl: str, model: str | None = None) -> float:
    """The share of requests that may write the block before it costs more.

    Per request, a hit saves ``1 - read`` of the block's input price and a
    miss costs ``write - 1`` extra (it writes instead of paying plain input).
    The breakpoint pays while ``writes * (write - 1) < hits * (1 - read)``,
    i.e. while the write share stays under ``(1 - read) / (write - read)``.
    Batch discounts scale both sides alike. ``read`` is the model's own
    cache-read multiple when ``model`` is given (Opus 5.5 reads at 0.05×, not
    the usual 0.1×), else the usual one. This is arithmetic on list-price
    multipliers from ``core.pricing``, not a measured saving.
    """
    write = CACHE_WRITE_1H_MULTIPLIER if ttl == "1h" else CACHE_WRITE_5M_MULTIPLIER
    read = _cache_read_multiplier(model)
    return (1.0 - read) / (write - read)


# --------------------------------------------------------------------------
# Evaluation protocol
# --------------------------------------------------------------------------

EVALUATION_PROTOCOL: dict[str, str] = {
    "status": (
        "NOT RUN. No live request has been made for this experiment. The owner "
        "declined a live evaluation for S20 (2026-09-29), and no API key was in the "
        "session's environment. Nothing in this module or its decision record is a "
        "measured saving."
    ),
    "authorization": (
        "A live comparison needs its own authorization: a maximum spend, an API key "
        "in the environment, a fixed corpus, and a stopping rule agreed in advance. "
        "The presence of a key is not authorization to spend."
    ),
    "arms": (
        "baseline (switch unset), candidate_1h (SPEC_CRITIC_PROJECT_CONTEXT_CACHE=1h), "
        "candidate_5m (=5m). One change at a time: same corpus, module, review model, "
        "effort, transport, worker count, and Project Context across arms."
    ),
    "corpus": (
        "At least one module's worth of specs that share a sizable Project Context "
        "(a data-center module after research, or any module with an attached "
        "drawing analysis), "
        "recorded by SHA-256 of each .docx and of the Project Context text. Report "
        "the context's size in tokens from the count endpoint, not a local estimate."
    ),
    "cold_and_warm": (
        "Measure a cold run (nothing cached) and a warm repeat within the TTL "
        "separately, on both transports: Message Batches (hits are best-effort there; "
        "the provider cites 30-98%) and real time (the first workers' requests race "
        "and all write)."
    ),
    "changed_context_control": (
        "Repeat the candidate with one character of the Project Context changed. It "
        "must show writes and no reads at the new breakpoint; reads there would mean "
        "the prefix is not what this module says it is."
    ),
    "record_per_attempt": (
        "From the run's diagnostics export (Save as JSON, or scripts/recover_batch.py "
        "--diagnostics-json): every review attempt's input, output, cache-read, and "
        "cache-write tokens with the 5m / 1h split (plan WP-15 attempt records), the "
        "estimated cost by category, and each request's wall-clock latency. Never infer "
        "reuse from a configured cache_control field or from timing alone."
    ),
    "quality": (
        "The candidate changes the request's block structure, not its text. Compare the "
        "findings of the two arms on the same corpus (count, severity mix, and a sample "
        "read side by side) before calling the change neutral."
    ),
    "promotion": (
        "Enable by default only if, on both the cold and the warm measurement, the net "
        "review cost (every read and write priced per TTL) is lower than the baseline "
        "by a margin worth a default change, the changed-context control invalidates, "
        "no quality change is seen, and no request is rejected. Otherwise keep the "
        "switch off and record the result."
    ),
}


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def capture(
    spec_paths: Sequence[Path],
    *,
    project_context: str,
    module_id: str | None,
    ttl: str,
    with_tokens: bool,
) -> dict[str, Any]:
    """The full offline capture the decision record quotes."""
    from src.input.extractor import extract_text_from_docx

    specs = [extract_text_from_docx(Path(p)) for p in spec_paths]
    baseline = review_requests(specs, project_context=project_context, module_id=module_id)
    candidate = review_requests(
        specs, project_context=project_context, module_id=module_id, switch=ttl
    )
    # One character added to the context (a trailing space would be stripped
    # by the prompt builder and change nothing).
    changed = review_requests(
        specs[:1], project_context=project_context.strip() + ".", module_id=module_id, switch=ttl
    )
    first = describe_request(candidate[0])
    configuration = {
        "arms": {"baseline": None, "candidate": ttl},
        "model": first["model"],
        "thinking": first["thinking"],
        "effort": first["effort"],
        "tool_choice": first["tool_choice"],
        # The prompt protocol the arms share: the tool definitions and the
        # module's system prompt, by digest.
        "tools_and_system_sha256_12": [
            seg["sha256_12"]
            for seg in first["segments"]
            if seg["position"].startswith(("tools", "system"))
        ],
    }
    return {
        "configuration": configuration,
        "configuration_sha256": hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "inputs": {
            # ``file_sha256`` identifies the file given; ``content_sha256`` the
            # extracted text the request carries, which is the dataset identity:
            # two saves of one document can differ in their zip bytes alone.
            "specs": [
                {
                    "file": Path(p).name,
                    "file_sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest(),
                    "content_sha256": hashlib.sha256(spec.content.encode("utf-8")).hexdigest(),
                }
                for p, spec in zip(spec_paths, specs)
            ],
            "project_context_sha256": hashlib.sha256(
                project_context.encode("utf-8")
            ).hexdigest(),
            "module_id": module_id,
            "candidate_ttl": ttl,
        },
        "baseline": {
            "requests": [describe_request(p, with_tokens=with_tokens) for p in baseline],
            "shared_prefix": shared_prefix(baseline),
        },
        "candidate": {
            "requests": [describe_request(p, with_tokens=with_tokens) for p in candidate],
            "shared_prefix": shared_prefix(candidate),
        },
        "changed_context_control": shared_prefix([candidate[0], changed[0]]),
        "phase_breakpoint_budget": {
            "baseline": phase_breakpoint_budget(None),
            "candidate": phase_breakpoint_budget(ttl),
        },
        "break_even_write_share": {
            t: break_even_write_share(t, api_config.REVIEW_MODEL_DEFAULT) for t in ("1h", "5m")
        },
        "evaluation_protocol": EVALUATION_PROTOCOL,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.project_context_cache",
        description="Capture the review request layout for the EX-01 experiment (no API call).",
    )
    parser.add_argument("--spec", action="append", required=True, type=Path, help=".docx spec")
    parser.add_argument("--context-file", type=Path, required=True, help="Project Context text")
    parser.add_argument("--module", default=None, help="module id (default: the default module)")
    parser.add_argument("--ttl", choices=("1h", "5m"), default="1h")
    parser.add_argument(
        "--tokens",
        action="store_true",
        help="add unpadded local cl100k counts (needs the tiktoken rank file)",
    )
    args = parser.parse_args(argv)
    result = capture(
        args.spec,
        project_context=args.context_file.read_text(encoding="utf-8"),
        module_id=args.module,
        ttl=args.ttl,
        with_tokens=args.tokens,
    )
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
