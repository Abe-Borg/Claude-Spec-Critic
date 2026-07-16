"""Drawing-impact synthesis: explain how the drawings informed the review.

The app can turn an attached set of construction drawings into a plain-text
DIGEST that is merged into Project Context (``input/drawing_digest.py``), so
the drawings ride on every review / cross-check / verification call as
reference context. But nothing downstream attributes any *finding* back to
the drawings, so the exported report cannot answer the operator's question:
"did uploading the drawings actually help?"

This module closes that gap with one grounded, post-review synthesis call.
After the findings are collected, id-stamped, and verified, it hands the
model (a) the drawing digest and (b) the final findings and asks it to
explain — honestly — how the drawings bear on the review: which findings the
drawings corroborate, contradict, or contextualize (each linked by its exact
finding id and citing the digest's own ``[<file> p.N]`` page references), and
an overall impact level. The result renders as a dedicated report section.

Shape mirrors the cross-check pass (``cross_check/cross_checker.py``): one
synchronous structured-tool call, retries via
``DEFAULT_REALTIME_RETRY_POLICY``, a tagged-JSON text fallback, and telemetry
carried back on the result. It sends **no** web/search tools — the task is
synthesis over text the run already produced, not fresh research — so there
is no ``pause_turn`` continuation loop.

Grounding guardrails, consistent with the rest of the trust model:

* The prompt forbids inventing page references or manufacturing a connection
  a finding does not actually have, and instructs the model to report an
  honest "the drawings added little" (impact ``none`` / ``minimal``) when
  that is the truth.
* Every returned ``finding_link`` whose ``finding_id`` is not one of the real
  findings passed in is dropped at parse time — a hallucinated id can never
  reach the report.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.api_config import (
    DRAWING_IMPACT_MODEL_DEFAULT,
    PHASE_DRAWING_IMPACT,
    apply_effort_config,
    apply_thinking_config,
    drawing_impact_max_tokens,
    extract_cache_usage,
    system_prompt_with_cache,
    tools_with_cache,
)
from ..input.drawing_digest import DIGEST_ATTACHMENT_LABEL
from ..modules import DEFAULT_MODULE, ReviewModule
from ..review.prompt_serialization import (
    TAG_FINDING,
    escape_text,
    render_blocks,
    wrap_data_block,
    wrap_document_block,
)
from ..review.reviewer import Finding, _get_client
from ..review.structured_schemas import (
    DRAWING_IMPACT_LEVELS,
    DRAWING_IMPACT_RELATIONSHIPS,
    DRAWING_IMPACT_TOOL_NAME,
    drawing_impact_tool,
    drawing_impact_tool_choice,
    extract_tool_use_block,
    structured_tool_output_enabled,
)
from ..verification.retry_policy import (
    DEFAULT_REALTIME_RETRY_POLICY,
    FailureClass,
    classify_exception,
    compute_backoff_seconds,
    is_retryable_failure_class,
)

LogFn = Callable[..., None]


def _noop_log(_msg: str, **_kwargs: object) -> None:
    return


# The digest is wrapped into Project Context by
# ``drawing_digest.wrapped_digest_block`` via ``wrap_attachment`` — its
# BEGIN/END marker lines are a stable schema string, so the extractor keys
# off them directly. Kept in sync with ``context_attachment.wrap_attachment``.
_DIGEST_BEGIN = f"--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---"
_DIGEST_END = f"--- END ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---"
_DIGEST_BLOCK_RE = re.compile(
    re.escape(_DIGEST_BEGIN) + r"\n(.*?)\n" + re.escape(_DIGEST_END),
    re.DOTALL,
)

# Cap each finding's issue text in the prompt so a verbose finding set can't
# dominate the input — the synthesizer needs the gist, not the full body
# (mirrors cross-check's 160-char prior-finding cap, a touch longer here
# because a single finding is the unit being reasoned about).
_ISSUE_PREVIEW_CHARS = 280


# ---------------------------------------------------------------------------
# Digest extraction
# ---------------------------------------------------------------------------


def extract_drawing_digest(project_context: str | None) -> str:
    """Return the drawing-digest text spliced into ``project_context``, or ``""``.

    Pulls every ``Construction Drawing Digest`` attachment block (a re-attach
    merges more than one) and joins their inner text. Returns ``""`` when no
    digest is present — the caller uses that as the gate, so a run without
    drawings produces no impact pass and a byte-identical report.

    Only the exact BEGIN/END marker lines are matched, so a context file a
    user happened to name "Construction Drawing Digest.docx" (whose
    attachment label carries the extension) is not mistaken for a digest.
    """
    if not project_context:
        return ""
    blocks = [m.group(1).strip() for m in _DIGEST_BLOCK_RE.finditer(project_context)]
    blocks = [b for b in blocks if b]
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawingFindingLink:
    """One finding the drawings bear on, grounded in digest page references."""

    finding_id: str
    relationship: str  # one of DRAWING_IMPACT_RELATIONSHIPS
    explanation: str
    sheet_references: list[str] = field(default_factory=list)


@dataclass
class DrawingImpactResult:
    """The synthesized explanation plus usage telemetry.

    ``status`` is ``"completed"`` or ``"failed"`` (an operational/parse
    failure). ``impact_level`` is one of :data:`DRAWING_IMPACT_LEVELS`.
    Report surfaces read every field defensively via ``getattr`` so a
    profile-less / drawing-less run — which carries ``None`` instead of this
    object — renders no section at all.
    """

    status: str
    impact_level: str = "none"
    narrative: str = ""
    finding_links: list[DrawingFindingLink] = field(default_factory=list)
    model: str = DRAWING_IMPACT_MODEL_DEFAULT
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    elapsed_seconds: float | None = None
    stop_reason: str | None = None
    structured_payload: dict | None = None

    @property
    def linked_finding_count(self) -> int:
        return len(self.finding_links)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_impact_system_prompt() -> str:
    """Protocol/format contract for the synthesis. Engine-owned, domain-neutral.

    Byte-identical across runs and modules (the drawing-digest precedent):
    the domain flavor lives entirely in the digest + findings passed in the
    user message, so this prefix stays cacheable.
    """
    return (
        "You are writing one section of a construction-specification review "
        "report. Earlier in this run, a set of construction drawings was "
        "analyzed into the plain-text DRAWING DIGEST provided below, and that "
        "digest was supplied as reference context to every stage of the "
        "specification review. Your job is to tell the reader whether — and "
        "how — having the drawings available actually informed the review's "
        "findings.\n"
        "\n"
        "You are given:\n"
        "- DRAWING DIGEST: a structured transcription of the drawings (sheet "
        "index, general notes, schedules, coordination observations) with page "
        "references in the form [<file> p.N].\n"
        "- REVIEW FINDINGS: the issues the review identified, each with a "
        "stable id, severity, spec file, and description (some carry a "
        "verification verdict).\n"
        "\n"
        "<task>\n"
        "Identify the findings the drawings genuinely bear on and link each by "
        "its exact id. For each link, say whether the drawings corroborate, "
        "contradict, or contextualize the finding, and cite the digest page "
        "reference(s) that support the link. Then write a short plain-text "
        "narrative of the drawings' overall contribution — what they made "
        "checkable that the spec text alone did not, and where drawings and "
        "specs agree or conflict — and assign an overall impact level.\n"
        "</task>\n"
        "\n"
        "<grounding_rules>\n"
        "This report is trusted by engineers, so do not overstate the "
        "drawings' value:\n"
        "- Cite specific drawing content by its [<file> p.N] page reference "
        "whenever you claim the drawings showed something. Never invent a page "
        "reference or a sheet that is not in the digest.\n"
        "- Link a finding ONLY when the drawings actually bear on it. Do not "
        "link a finding merely because it exists. A review can find real "
        "issues the drawings say nothing about — that is expected, not a gap.\n"
        "- If the drawings did not materially affect the review — they only "
        "restated the specs, or no finding turns on drawing content — say so "
        "plainly and choose impact level 'none' or 'minimal'. An honest 'the "
        "drawings added little here' is more useful than a manufactured "
        "connection.\n"
        "- Treat digest content marked [ILLEGIBLE] or 'None found' as a limit "
        "on what the drawings could contribute, not as evidence.\n"
        "- Use only the ids present in the REVIEW FINDINGS list; never invent "
        "a finding id.\n"
        "</grounding_rules>\n"
        "\n"
        "<output>\n"
        "Submit your analysis by calling the submit_drawing_impact tool "
        "exactly once. The tool's input schema is the source of truth for "
        "field shapes. Use plain text in every text field — no markdown "
        "headers, bullets, or bold.\n"
        "Fallback: if for any reason you cannot call the tool, emit the JSON "
        "object wrapped in <drawing_impact_json>...</drawing_impact_json> "
        "tags. Prefer the tool.\n"
        "</output>"
    )


def render_findings_block(findings: list[Finding]) -> str:
    """Render the ``<review_findings>`` block over the id-carrying findings.

    Each finding is one ``<finding id=... severity=... file=... verdict=...>``
    data block (issue text as the body, truncated), escaped so a finding body
    cannot break the wrapper. Findings without an id are the caller's concern
    (they are filtered before this is called) — the model can only link ids it
    is shown.
    """
    blocks: list[str] = []
    for f in findings:
        verdict = ""
        verification = getattr(f, "verification", None)
        if verification is not None:
            verdict = str(getattr(verification, "verdict", "") or "")
        attrs: dict[str, str | None] = {
            "id": f.finding_id,
            "severity": f.severity,
            "file": f.fileName,
        }
        if f.section:
            attrs["section"] = f.section
        if verdict:
            attrs["verdict"] = verdict
        blocks.append(
            wrap_data_block(TAG_FINDING, (f.issue or "")[:_ISSUE_PREVIEW_CHARS], attrs=attrs)
        )
    inner = render_blocks(blocks)
    return f"<review_findings>\n{inner}\n</review_findings>"


def build_impact_user_message(
    digest_text: str,
    findings: list[Finding],
    *,
    module_display_name: str = "",
) -> str:
    """Assemble the user turn: digest block + findings block + instruction."""
    focus = (
        f"This is a {module_display_name} specification review. "
        if module_display_name
        else ""
    )
    digest_block = wrap_document_block("drawing_digest", digest_text)
    findings_block = render_findings_block(findings)
    count = len(findings)
    return (
        f"{focus}Explain how the construction drawings informed the review "
        f"below. The review produced {count} finding(s) that carry an id; link "
        "only those the drawings genuinely bear on, citing digest page "
        "references.\n\n"
        f"{digest_block}\n\n"
        f"{findings_block}"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _sanitize_narrative(text: str) -> str:
    """Strip stray markdown header markers so the narrative renders cleanly.

    Small local copy of the cross-check helper (the prompt asks for plain
    text, but models occasionally emit ``## HEADING`` anyway); avoids coupling
    this package to cross_checker internals.
    """
    if not text:
        return ""
    cleaned: list[str] = []
    for line in text.split("\n"):
        stripped = line
        while stripped.startswith("#"):
            stripped = stripped[1:]
        stripped = stripped.strip()
        if line.startswith("#") and not stripped:
            continue
        cleaned.append(stripped if line.startswith("#") else line)
    return "\n".join(cleaned).strip()


def _extract_impact_object(raw: str) -> dict | None:
    """Text-fallback parser: pull the impact object from a plain-text response.

    Prefers the explicit ``<drawing_impact_json>`` wrapper the prompt asks for,
    then falls back to the outermost ``{...}`` span. Never raises.
    """
    if not raw:
        return None
    match = re.search(r"<drawing_impact_json>(.*?)</drawing_impact_json>", raw, re.DOTALL)
    candidate = match.group(1).strip() if match else None
    if candidate is None:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = raw[start : end + 1]
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_level(value: Any) -> str:
    level = str(value or "").strip().lower()
    return level if level in DRAWING_IMPACT_LEVELS else "minimal"


def _coerce_relationship(value: Any) -> str:
    rel = str(value or "").strip().lower()
    return rel if rel in DRAWING_IMPACT_RELATIONSHIPS else "contextualized"


def _parse_impact_payload(
    payload: dict, valid_ids: set[str]
) -> tuple[str, str, list[DrawingFindingLink]]:
    """Validate a raw impact payload into ``(impact_level, narrative, links)``.

    A ``finding_link`` whose id is not one of ``valid_ids`` is dropped — a
    hallucinated id can never reach the report. Duplicate ids collapse to the
    first occurrence so the report never renders the same finding twice.
    """
    impact_level = _coerce_level(payload.get("impact_level"))
    narrative = _sanitize_narrative(str(payload.get("narrative") or ""))

    links: list[DrawingFindingLink] = []
    seen: set[str] = set()
    raw_links = payload.get("finding_links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, dict):
                continue
            fid = str(entry.get("finding_id") or "").strip()
            if fid not in valid_ids or fid in seen:
                continue
            seen.add(fid)
            refs_raw = entry.get("sheet_references")
            refs = (
                [str(r).strip() for r in refs_raw if str(r).strip()]
                if isinstance(refs_raw, list)
                else []
            )
            links.append(
                DrawingFindingLink(
                    finding_id=fid,
                    relationship=_coerce_relationship(entry.get("relationship")),
                    explanation=str(entry.get("explanation") or "").strip(),
                    sheet_references=refs,
                )
            )
    return impact_level, narrative, links


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_drawing_impact(
    *,
    digest_text: str,
    findings: list[Finding],
    module: ReviewModule = DEFAULT_MODULE,
    model: str = DRAWING_IMPACT_MODEL_DEFAULT,
    max_retries: int = 3,
    log: LogFn = _noop_log,
    client: Any = None,
) -> DrawingImpactResult:
    """Run the single synthesis call and return the structured explanation.

    ``findings`` may include findings without an id (they are filtered — the
    model can only link ids it is shown) and may be empty (the narrative can
    still speak to the drawings' overall contribution). Never raises: every
    failure path returns a ``status="failed"`` result so the caller can record
    the outcome without a try/except.
    """
    linkable = [f for f in findings if (f.finding_id or "").strip()]
    valid_ids = {f.finding_id for f in linkable}

    system_prompt = build_impact_system_prompt()
    user_message = build_impact_user_message(
        digest_text, linkable, module_display_name=getattr(module, "display_name", "")
    )

    if client is None:
        client = _get_client()
    start = time.time()
    output_limit = drawing_impact_max_tokens(model=model)
    system_payload = system_prompt_with_cache(system_prompt, phase=PHASE_DRAWING_IMPACT)
    use_tool = structured_tool_output_enabled()
    request_kwargs: dict = {
        "model": model,
        "max_tokens": output_limit,
        "system": system_payload,
        "messages": [{"role": "user", "content": user_message}],
    }
    apply_thinking_config(request_kwargs, model=model, phase=PHASE_DRAWING_IMPACT)
    apply_effort_config(request_kwargs, model=model, phase=PHASE_DRAWING_IMPACT)
    if use_tool:
        request_kwargs["tools"] = tools_with_cache(
            [drawing_impact_tool(model=model)], phase=PHASE_DRAWING_IMPACT
        )
        request_kwargs["tool_choice"] = drawing_impact_tool_choice()

    def _failed(error: str, **usage: int) -> DrawingImpactResult:
        return DrawingImpactResult(
            status="failed",
            model=model,
            error=error,
            elapsed_seconds=time.time() - start,
            **usage,
        )

    policy = DEFAULT_REALTIME_RETRY_POLICY
    attempts_planned = max(1, max_retries)
    last_failure_class: FailureClass | None = None
    for attempt in range(attempts_planned):
        is_last_attempt = attempt == attempts_planned - 1
        try:
            with client.messages.stream(**request_kwargs) as stream:
                chunks: list[str] = []
                for text in stream.text_stream:
                    chunks.append(text)
                resp = stream.get_final_message()

            raw_response = "".join(chunks)
            stop_reason = getattr(resp, "stop_reason", None)
            usage = getattr(resp, "usage", None)
            in_tok = out_tok = cc_tok = cr_tok = 0
            if usage:
                in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                cache = extract_cache_usage(usage)
                cc_tok = cache["cache_creation_input_tokens"]
                cr_tok = cache["cache_read_input_tokens"]
            usage_kwargs = dict(
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_creation_input_tokens=cc_tok,
                cache_read_input_tokens=cr_tok,
            )

            if stop_reason not in ("end_turn", "tool_use"):
                # Truncation / refusal — not a transient network error, so
                # don't retry; surface it as a failed pass.
                return _failed(
                    f"Response incomplete (stop_reason: {stop_reason}).",
                    **usage_kwargs,
                )

            payload = (
                extract_tool_use_block(resp, DRAWING_IMPACT_TOOL_NAME) if use_tool else None
            )
            structured = payload if isinstance(payload, dict) else None
            if not isinstance(payload, dict):
                payload = _extract_impact_object(raw_response)
            if not isinstance(payload, dict):
                return _failed(
                    "Could not parse drawing-impact output.", **usage_kwargs
                )

            impact_level, narrative, links = _parse_impact_payload(payload, valid_ids)
            return DrawingImpactResult(
                status="completed",
                impact_level=impact_level,
                narrative=narrative,
                finding_links=links,
                model=model,
                elapsed_seconds=time.time() - start,
                stop_reason=stop_reason,
                structured_payload=structured,
                **usage_kwargs,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 — classified below
            failure_class = classify_exception(exc)
            last_failure_class = failure_class
            if not is_retryable_failure_class(failure_class) or is_last_attempt:
                return _failed(f"{type(exc).__name__}: {exc}")
            backoff = compute_backoff_seconds(
                policy, attempt=attempt, failure_class=failure_class
            )
            time.sleep(backoff)

    suffix = f" (class={last_failure_class.value})" if last_failure_class else ""
    return _failed(f"Failed after {attempts_planned} attempts{suffix}.")
