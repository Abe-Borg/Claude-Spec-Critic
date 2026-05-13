"""Claude API client for specification review."""
from __future__ import annotations

import os
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from anthropic import Anthropic, APIError, APIConnectionError, APIStatusError, RateLimitError, InternalServerError

from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .api_config import (
    MODEL_OPUS_47,
    REVIEW_MODEL_DEFAULT,
    extract_cache_usage,
)
from .retry_policy import (
    DEFAULT_REALTIME_RETRY_POLICY,
    FailureClass,
    classify_exception,
    compute_backoff_seconds,
    is_retryable_failure_class,
)
from .review_request_builder import (
    ReviewRequestSpec,
    build_realtime_review_kwargs,
    build_review_request,
)
from .structured_schemas import (
    REVIEW_TOOL_NAME,
    extract_tool_use_block,
    structured_tool_output_enabled,
)

StreamCallback = Callable[[str], None]

REPORT_ONLY_ACTION: str = "REPORT_ONLY"
EDIT_ACTION_TYPES: frozenset[str] = frozenset({"ADD", "EDIT", "DELETE"})

_INSERT_POSITIONS: frozenset[str] = frozenset({"before", "after"})


def validate_edit_shape(
    action: str,
    *,
    existing_text: str | None,
    replacement_text: str | None,
    anchor_text: str | None = None,
    insert_position: str | None = None,
) -> str | None:
    """Return a demotion reason if action-specific fields are missing, else None.

    Chunk 7 / plan section "Validate edit proposals at parse time": every
    executable edit must satisfy action-specific field requirements before
    it leaves the parser. The four rules are:

    * ``EDIT``   — non-empty ``existing_text`` and ``replacement_text``.
    * ``DELETE`` — non-empty ``existing_text``.
    * ``ADD``    — non-empty ``anchor_text`` and ``replacement_text``, plus
      ``insert_position`` in ``{"before", "after"}``.
    * ``REPORT_ONLY`` and any unknown action — None (REPORT_ONLY cleanup is
      the parser's job; unknown actions are coerced to REPORT_ONLY before
      this helper sees them).

    The return value is the short human-readable reason that the parser
    stamps on ``Finding.demotion_reason`` so diagnostics, the report, and
    the edit-candidate UI can all explain *why* a finding lost its edit
    slot rather than treating it as a generic REPORT_ONLY.
    """
    norm = (action or "").strip().upper()
    if norm == "EDIT":
        if not (existing_text and existing_text.strip()):
            return "EDIT action missing required existingText"
        if not (replacement_text and replacement_text.strip()):
            return "EDIT action missing required replacementText"
        return None
    if norm == "DELETE":
        if not (existing_text and existing_text.strip()):
            return "DELETE action missing required existingText"
        return None
    if norm == "ADD":
        if not (anchor_text and anchor_text.strip()):
            return "ADD action missing required anchorText"
        normalized_position = (insert_position or "").strip().lower()
        if normalized_position not in _INSERT_POSITIONS:
            return "ADD action missing required insertPosition (before|after)"
        if not (replacement_text and replacement_text.strip()):
            return "ADD action missing required replacementText"
        return None
    return None


@dataclass
class EditProposal:
    """An optional, separate, high-confidence action derived from a finding.

    Chunk L / plan section 5: the previous schema forced every finding into
    an edit shape (action / existingText / replacementText / ...). Many
    findings — coordination problems, constructability concerns, code
    interpretation questions — have no clean textual fix, so the model was
    asked to invent one. This class is the explicit "there is a direct
    text edit" half of the split: a finding either carries one or it does
    not.

    Fields mirror the legacy shape so the migration path stays local:

    * ``action_type``       — ``ADD`` / ``EDIT`` / ``DELETE``.
    * ``existing_text``     — verbatim text to edit/delete (None for ADD).
    * ``replacement_text``  — proposed replacement / new text.
    * ``anchor_text``       — ADD only: nearby paragraph used to locate the
      insertion point.
    * ``insert_position``   — ADD only: ``"before"`` / ``"after"``.
    * ``target_element_id`` — optional ``ParagraphMapping.element_id`` of
      the paragraph / row / heading the proposal targets. Disambiguates
      identical text in different sections and revalidates against the
      live element at apply time.
    * ``edit_confidence``   — 0.0-1.0 model confidence in the edit itself,
      separate from the finding's overall confidence. Defaults to the
      finding-level confidence when the schema does not surface a
      proposal-specific value.
    """

    action_type: str
    existing_text: str | None = None
    replacement_text: str | None = None
    anchor_text: str | None = None
    insert_position: str | None = None
    target_element_id: str | None = None
    edit_confidence: float = 0.5


@dataclass
class Finding:
    severity: str
    fileName: str
    section: str
    issue: str
    actionType: str
    existingText: str | None
    replacementText: str | None
    codeReference: str | None
    confidence: float = 0.5
    verification: VerificationResult | None = None
    affected_files: list[str] = field(default_factory=list)
    anchorText: str | None = None
    insertPosition: str | None = None
    evidenceElementId: str | None = None
    edit_proposal: EditProposal | None = None
    finding_id: str = ""
    upstream_finding_ids: list[str] = field(default_factory=list)
    independent_evidence_ids: list[str] = field(default_factory=list)
    suppression_reason: str | None = None
    demotion_reason: str | None = None
    occurrence_originals: list["Finding"] = field(default_factory=list)

    def as_edit_proposal(self) -> EditProposal | None:
        """Return the structured edit proposal for this finding, if any.

        Chunk L accessor. When ``edit_proposal`` is set, it is the
        authoritative answer. Otherwise the legacy fields are inspected:
        an actionType of ADD / EDIT / DELETE materializes an ``EditProposal``
        on the fly so older callers (resume-state loads, ad-hoc test
        Findings) keep working. Any other actionType — including the new
        ``REPORT_ONLY`` sentinel and the empty/legacy "no opinion" case —
        returns ``None`` so consumers can branch cleanly on
        "does this finding have a proposal?".

        Chunk 7 extension: validate action-specific shape requirements
        before returning a proposal. A Finding constructed with an
        EDIT/ADD/DELETE action but missing required fields (e.g.,
        ``actionType="EDIT"`` with ``existingText=None``) returns None
        instead of leaking an unusable proposal into the locator / edit
        pipeline. Parser callers should never hit this path because
        ``_parse_findings`` demotes invalid shapes at parse time; the
        defensive check guards legacy resume payloads and directly-
        constructed test Findings that bypass the parser.
        """
        if self.edit_proposal is not None:
            proposal = self.edit_proposal
        else:
            action = (self.actionType or "").strip().upper()
            if action not in EDIT_ACTION_TYPES:
                return None
            proposal = EditProposal(
                action_type=action,
                existing_text=self.existingText,
                replacement_text=self.replacementText,
                anchor_text=self.anchorText,
                insert_position=self.insertPosition,
                target_element_id=self.evidenceElementId,
                edit_confidence=self.confidence,
            )
        invalid = validate_edit_shape(
            proposal.action_type,
            existing_text=proposal.existing_text,
            replacement_text=proposal.replacement_text,
            anchor_text=proposal.anchor_text,
            insert_position=proposal.insert_position,
        )
        if invalid is not None:
            return None
        return proposal

    def has_edit_proposal(self) -> bool:
        """Convenience predicate — True iff :meth:`as_edit_proposal` is non-None."""
        return self.as_edit_proposal() is not None


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""
    model: str = REVIEW_MODEL_DEFAULT
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    stop_reason: str | None = None
    parse_status: str | None = None
    cross_check_status: str | None = None
    structured_payload: dict | None = None
    suppressed_findings: list[Finding] = field(default_factory=list)

    @property
    def critical_count(self) -> int: return sum(1 for f in self.findings if f.severity == "CRITICAL")
    @property
    def high_count(self) -> int: return sum(1 for f in self.findings if f.severity == "HIGH")
    @property
    def medium_count(self) -> int: return sum(1 for f in self.findings if f.severity == "MEDIUM")
    @property
    def gripe_count(self) -> int: return sum(1 for f in self.findings if f.severity == "GRIPES")
    @property
    def total_count(self) -> int: return len(self.findings)


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    return key


_cached_client: Anthropic | None = None
_cached_key: str | None = None


def _get_client() -> Anthropic:
    global _cached_client, _cached_key
    key = _get_api_key()
    if _cached_client is None or _cached_key != key:
        _cached_client = Anthropic(api_key=key)
        _cached_key = key
    return _cached_client


def _extract_json_array(text: str, *, stop_reason: str | None = None) -> tuple[list, str]:
    """Fallback parser for the legacy ``<findings_json>``-tagged text path.

    The primary path is best-effort tool-use output (the model calls
    ``submit_review_findings``). With ``tool_choice=auto`` the model MAY
    still return plain text — refusals, feature-flag-off runs, and
    occasional adaptive-thinking detours all land here — so this fallback
    must stay reachable until/unless a strict-tool-output mode is
    introduced as the default.
    """
    tagged = re.search(r"<\s*findings_json\s*>(.*?)<\s*/\s*findings_json\s*>", text, flags=re.IGNORECASE | re.DOTALL)
    if tagged:
        json_str = tagged.group(1).strip()
        thinking = text[:tagged.start()].strip()
        try:
            data = json.loads(json_str)
            if (
                isinstance(data, list)
                and all(isinstance(item, dict) for item in data)
                and all(("severity" in item and "issue" in item) for item in data)
            ):
                return data, thinking
        except json.JSONDecodeError:
            pass

    end_idx = text.rfind("]")
    while end_idx != -1:
        start_idx = text.rfind("[", 0, end_idx + 1)
        if start_idx == -1:
            break
        json_str = text[start_idx:end_idx + 1]
        thinking = text[:start_idx].strip()
        try:
            data = json.loads(json_str)
            if (
                isinstance(data, list)
                and all(isinstance(item, dict) for item in data)
                and all(("severity" in item and "issue" in item) for item in data)
            ):
                return data, thinking
        except json.JSONDecodeError:
            pass
        end_idx = text.rfind("]", 0, end_idx)

    if text.strip() == "[]":
        return [], ""

    raise ValueError(f"Could not extract JSON findings from response (stop_reason: {stop_reason}): {text[:200]}...")


def _extract_structured_findings(resp) -> tuple[list[dict], str, dict] | None:
    """Pull findings out of a ``submit_review_findings`` tool_use block.

    Returns ``(findings_list, analysis_summary, raw_payload)`` when a
    matching tool_use block is present, else ``None`` so callers fall
    back to text parsing. The third element is the parsed tool input
    dict; callers may surface it to diagnostics so the structured payload
    is preserved alongside the regular telemetry.
    """
    payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME)
    if not isinstance(payload, dict):
        return None
    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    summary = str(payload.get("analysis_summary") or "")
    return findings, summary, payload


def _parse_findings(data: list) -> list[Finding]:
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "")).strip().upper()
        if sev not in {"CRITICAL", "HIGH", "MEDIUM", "GRIPES"}:
            continue
        action_raw = item.get("actionType")
        action = str(action_raw).strip().upper() if action_raw is not None else ""
        if action not in EDIT_ACTION_TYPES and action != REPORT_ONLY_ACTION:
            action = REPORT_ONLY_ACTION
        issue = str(item.get("issue") or "").strip()
        if not issue:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except Exception:
            confidence = 0.5
        anchor_raw = item.get("anchorText")
        anchor_text = str(anchor_raw).strip() if anchor_raw is not None else None
        if anchor_text == "":
            anchor_text = None
        position_raw = item.get("insertPosition")
        position = str(position_raw).strip().lower() if position_raw is not None else None
        if position not in {"before", "after"}:
            position = None
        evidence_raw = item.get("evidenceElementId")
        evidence_id: str | None
        if evidence_raw is None:
            evidence_id = None
        else:
            evidence_id = str(evidence_raw).strip() or None
        upstream_raw = item.get("upstreamFindingIds")
        upstream_ids: list[str] = []
        if isinstance(upstream_raw, list):
            upstream_ids = [
                str(uid).strip()
                for uid in upstream_raw
                if str(uid).strip()
            ]
        independent_raw = item.get("independentEvidenceIds")
        independent_ids: list[str] = []
        if isinstance(independent_raw, list):
            independent_ids = [
                str(eid).strip()
                for eid in independent_raw
                if str(eid).strip()
            ]
        existing_text = (
            str(item.get("existingText")) if item.get("existingText") is not None else None
        )
        replacement_text = (
            str(item.get("replacementText"))
            if item.get("replacementText") is not None
            else None
        )
        demotion_reason: str | None = None
        if action in EDIT_ACTION_TYPES:
            demotion_reason = validate_edit_shape(
                action,
                existing_text=existing_text,
                replacement_text=replacement_text,
                anchor_text=anchor_text,
                insert_position=position,
            )
        if action in EDIT_ACTION_TYPES and demotion_reason is None:
            proposal: EditProposal | None = EditProposal(
                action_type=action,
                existing_text=existing_text,
                replacement_text=replacement_text,
                anchor_text=anchor_text,
                insert_position=position,
                target_element_id=evidence_id,
                edit_confidence=confidence,
            )
        else:
            if demotion_reason is not None:
                action = REPORT_ONLY_ACTION
            proposal = None
            existing_text = None
            replacement_text = None
            anchor_text = None
            position = None
        findings.append(Finding(
            severity=sev,
            fileName=str(item.get("fileName") or "").strip(),
            section=str(item.get("section") or "").strip(),
            issue=issue,
            actionType=action,
            existingText=existing_text,
            replacementText=replacement_text,
            codeReference=str(item.get("codeReference")) if item.get("codeReference") is not None else None,
            confidence=confidence,
            anchorText=anchor_text,
            insertPosition=position,
            evidenceElementId=evidence_id,
            edit_proposal=proposal,
            upstream_finding_ids=upstream_ids,
            independent_evidence_ids=independent_ids,
            demotion_reason=demotion_reason,
        ))
    return findings


def _stream_review(client: Anthropic, system_prompt: str, user_message: str, *, model: str = REVIEW_MODEL_DEFAULT, max_retries: int = 3, verbose: bool = False, stream_callback: Optional[StreamCallback] = None) -> ReviewResult:
    start_time = time.time()
    result = ReviewResult(model=model)
    request_kwargs = build_realtime_review_kwargs(
        system_prompt=system_prompt,
        user_message=user_message,
        model=model,
    )
    use_structured_tool = "tools" in request_kwargs
    policy = DEFAULT_REALTIME_RETRY_POLICY
    attempts_planned = max(1, max_retries)
    last_exception: Exception | None = None
    last_failure_class: FailureClass | None = None
    for attempt in range(attempts_planned):
        is_last_attempt = attempt == attempts_planned - 1
        try:
            if verbose:
                print(f"Calling Claude {model} (attempt {attempt + 1}/{attempts_planned})...")
            with client.messages.stream(**request_kwargs) as stream:
                chunks: list[str] = []
                for text in stream.text_stream:
                    chunks.append(text)
                    if stream_callback:
                        try: stream_callback(text)
                        except Exception: pass
                resp = stream.get_final_message()
            response_text = "".join(chunks)
            result.raw_response = response_text
            result.stop_reason = getattr(resp, "stop_reason", None)
            usage = getattr(resp, "usage", None)
            if usage:
                result.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                cache = extract_cache_usage(usage)
                result.cache_creation_input_tokens = cache["cache_creation_input_tokens"]
                result.cache_read_input_tokens = cache["cache_read_input_tokens"]

            if result.stop_reason not in ("end_turn", "tool_use"):
                result.parse_status = "incomplete"
                result.error = f"Response incomplete (stop_reason: {result.stop_reason}). The model likely ran out of output tokens. Partial response preserved in raw_response."
                result.elapsed_seconds = time.time() - start_time
                return result

            structured = _extract_structured_findings(resp) if use_structured_tool else None
            if structured is not None:
                data, thinking, structured_payload = structured
                result.structured_payload = structured_payload
            else:
                data, thinking = _extract_json_array(response_text, stop_reason=result.stop_reason)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.parse_status = "ok"
            result.elapsed_seconds = time.time() - start_time
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            failure_class = classify_exception(e)
            last_failure_class = failure_class
            if not is_retryable_failure_class(failure_class):
                if failure_class is FailureClass.INVALID_REQUEST:
                    result.error = f"API error: {e}"
                else:
                    result.error = f"Error: {e}"
                    result.parse_status = "parse_error"
                result.elapsed_seconds = time.time() - start_time
                return result
            last_exception = e
            if is_last_attempt:
                break
            backoff = compute_backoff_seconds(
                policy, attempt=attempt, failure_class=failure_class
            )
            if verbose:
                print(
                    f"Retryable {failure_class.value} error "
                    f"(attempt {attempt + 1}/{attempts_planned}): {e}. "
                    f"Retrying in {backoff:.0f}s..."
                )
            time.sleep(backoff)
    if last_exception is not None:
        suffix = (
            f" (class={last_failure_class.value})"
            if last_failure_class is not None
            else ""
        )
        result.error = (
            f"Failed after {attempts_planned} attempts{suffix}: "
            f"{type(last_exception).__name__}: {last_exception}"
        )
    else:
        result.error = f"Failed after {attempts_planned} attempts."
    result.elapsed_seconds = time.time() - start_time
    return result


def review_single_spec(
    spec_content: str,
    filename: str,
    *,
    project_context: str = "",
    model: str = REVIEW_MODEL_DEFAULT,
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: Optional[StreamCallback] = None,
    cycle: CodeCycle = DEFAULT_CYCLE,
    paragraph_map=None,
    pre_detected_alerts=None,
) -> ReviewResult:
    """Stream a real-time review for one spec.

    Chunk K2: callers that have an ``ExtractedSpec`` should forward its
    ``paragraph_map`` so the model sees element ids alongside the spec
    text. Legacy callers (a raw ``spec_content`` string) keep working
    unchanged because the prompt builder falls back to the plain-body
    rendering when no map is provided.

    Chunk D4.1: callers can forward the per-spec deterministic alerts
    produced by the preprocessor so the model is told what was already
    found locally and does not duplicate those items as new findings.
    ``None`` keeps the legacy message shape.

    Chunk 3: this is the production real-time entry point. It builds a
    :class:`ReviewRequestSpec` and routes through
    :func:`build_review_request` so the prompt builder, structured-tool
    flag, cache breakpoint, max_tokens, thinking config, and effort
    policy come from the same code as the batch path. The streaming
    transport keeps the older ``_stream_review`` signature for the
    handful of tests that inject raw prompt strings; that wrapper also
    routes through the central builder.
    """
    request_spec = ReviewRequestSpec(
        spec_content=spec_content,
        filename=filename,
        model=model,
        cycle=cycle,
        project_context=project_context,
        paragraph_map=paragraph_map,
        pre_detected_alerts=pre_detected_alerts,
        batch=False,
    )
    built = build_review_request(request_spec)
    return _stream_review(
        _get_client(),
        built.system_prompt,
        built.user_message,
        model=model,
        max_retries=max_retries,
        verbose=verbose,
        stream_callback=stream_callback,
    )