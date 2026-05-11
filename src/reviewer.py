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

from .prompts import get_system_prompt, get_single_spec_user_message
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .review_modes import DEFAULT_REVIEW_MODE, ReviewMode
from .api_config import (
    MODEL_OPUS_46,
    MODEL_OPUS_47,
    PHASE_REVIEW,
    REVIEW_MODEL_DEFAULT,
    apply_thinking_config,
    extract_cache_usage,
    review_max_tokens,
    system_prompt_with_cache,
)
from .structured_schemas import (
    REVIEW_TOOL_NAME,
    extract_tool_use_block,
    review_findings_tool,
    review_tool_choice,
    structured_outputs_enabled,
)

REVIEW_MODELS = {"Opus 4.7": MODEL_OPUS_47}
StreamCallback = Callable[[str], None]

# ---------------------------------------------------------------------------
# Retryable connection-failure heuristic
# ---------------------------------------------------------------------------
# These patterns catch httpx / urllib3 / aiohttp transport-level failures
# that surface as generic Exception (not wrapped in anthropic APIError).
# They are transient and safe to retry.
_RETRYABLE_EXCEPTION_PATTERNS = (
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
    "connection closed",
    "timed out",
    "timeout",
    "broken pipe",
    "remotedisconnected",
    "connectionreset",
    "server disconnected",
    "eof occurred",
    "incomplete read",
)


def _is_retryable_connection_error(exc: Exception) -> bool:
    """Return True if a generic exception looks like a transient connection failure."""
    msg = str(exc).lower()
    return any(pattern in msg for pattern in _RETRYABLE_EXCEPTION_PATTERNS)


# Chunk L / plan section "Separate Findings From Edit Proposals":
# ``REPORT_ONLY`` is the explicit "no edit proposal" action type. Findings
# tagged this way are surfaced in the report but never produce edit
# candidates, so coordination/code findings that have no clean textual
# fix no longer have to manufacture replacement text just to satisfy a
# schema slot. ``EDIT_ACTION_TYPES`` is the set of action types that
# *do* carry a real edit proposal — every consumer that has to decide
# "is this thing editable?" routes through this constant so adding a
# new edit action is a one-line change.
REPORT_ONLY_ACTION: str = "REPORT_ONLY"
EDIT_ACTION_TYPES: frozenset[str] = frozenset({"ADD", "EDIT", "DELETE"})


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
    # ADD-action insertion model (audit Issue 5). When the model explicitly
    # provides an anchor and a side, the editor inserts deterministically
    # instead of falling back to brittle prefix/suffix text heuristics.
    anchorText: str | None = None
    insertPosition: str | None = None  # "before" | "after" | None
    # Chunk K3 / plan section "Stable Document IDs": optional pointer to
    # the paragraph / row / heading id (see ``ParagraphMapping.element_id``).
    # The locator prefers this id when it is non-empty and revalidates the
    # exact-text quote against the live element before applying any edit.
    # Empty string is the legacy fallback path (text-based matching).
    evidenceElementId: str | None = None
    # Chunk L / plan section "Separate Findings From Edit Proposals":
    # the optional structured edit half. Findings with no clean textual fix
    # leave this None and set ``actionType = "REPORT_ONLY"`` (or leave the
    # legacy fields blank). The locator and edit-candidate paths route
    # through :meth:`as_edit_proposal` so they see the same value whether
    # the proposal arrived from the new schema slot or was reconstructed
    # from legacy fields at runtime.
    edit_proposal: EditProposal | None = None
    # Chunk M / plan section "Cross-Check Dependency Tracking": stable
    # identifier assigned at dedup time. Cross-check findings cite these
    # ids in ``upstream_finding_ids`` so post-verification suppression can
    # be deterministic instead of relying on file/section overlap. Empty
    # string is the pre-Chunk-M / legacy path (suppression falls back to
    # heuristic matching).
    finding_id: str = ""
    # Chunk M: per-cross-check-finding dependency tracking. Populated only
    # on cross-check findings; review findings leave both empty. The model
    # emits these via the cross-check tool schema's ``upstreamFindingIds``
    # and ``independentEvidenceIds`` slots; the suppression filter uses
    # them to drop coordination claims whose every upstream went DISPUTED
    # while keeping claims that have independent spec evidence.
    upstream_finding_ids: list[str] = field(default_factory=list)
    independent_evidence_ids: list[str] = field(default_factory=list)
    # Chunk M: when the suppression filter drops a cross-check finding,
    # it stamps a human-readable reason here so the report can explain the
    # decision instead of silently omitting the finding. ``None`` is the
    # default "not suppressed" state; non-None implies the finding lives
    # on ``ReviewResult.suppressed_findings`` rather than the main list.
    suppression_reason: str | None = None

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
        """
        if self.edit_proposal is not None:
            return self.edit_proposal
        action = (self.actionType or "").strip().upper()
        if action not in EDIT_ACTION_TYPES:
            return None
        return EditProposal(
            action_type=action,
            existing_text=self.existingText,
            replacement_text=self.replacementText,
            anchor_text=self.anchorText,
            insert_position=self.insertPosition,
            target_element_id=self.evidenceElementId,
            edit_confidence=self.confidence,
        )

    def has_edit_proposal(self) -> bool:
        """Convenience predicate — True iff :meth:`as_edit_proposal` is non-None."""
        return self.as_edit_proposal() is not None


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""
    model: str = MODEL_OPUS_47
    input_tokens: int = 0
    output_tokens: int = 0
    # Phase 2 prompt-caching telemetry. Populated when the API returns
    # cache_creation_input_tokens / cache_read_input_tokens in usage.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    stop_reason: str | None = None
    parse_status: str | None = None
    cross_check_status: str | None = None
    # Chunk M / plan section "Cross-Check Dependency Tracking": findings
    # dropped by the upstream-disputed suppression filter live here, each
    # stamped with a ``suppression_reason``. The report renders them under
    # a dedicated "Suppressed coordination findings" subsection so the
    # decision is visible rather than silently making the finding vanish.
    # Verification skips suppressed findings — they are end-state.
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

    Phase 2.4 (audit Section 6.4) replaces this with structured tool-use
    outputs as the primary path. This function remains as a fallback when
    the model returns no tool_use block (e.g., refusal or feature flag off).
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
        return [], text.strip()

    raise ValueError(f"Could not extract JSON findings from response (stop_reason: {stop_reason}): {text[:200]}...")


def _extract_structured_findings(resp) -> tuple[list[dict], str] | None:
    """Pull findings out of a tool_use block when structured outputs are used.

    Returns ``(findings_list, analysis_summary)`` if a matching tool_use
    block is present, else None — callers fall back to text parsing.
    """
    payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME)
    if not isinstance(payload, dict):
        return None
    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    summary = str(payload.get("analysis_summary") or "")
    return findings, summary


def _parse_findings(data: list) -> list[Finding]:
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "")).strip().upper()
        if sev not in {"CRITICAL", "HIGH", "MEDIUM", "GRIPES"}:
            continue
        # Chunk L: actionType is no longer forced to "EDIT". A finding that
        # has no clean textual fix can declare ``REPORT_ONLY`` (or leave the
        # field blank, treated as REPORT_ONLY) and skip the edit slot
        # entirely. Anything outside the EDIT/ADD/DELETE/REPORT_ONLY set
        # is downgraded to REPORT_ONLY rather than silently coerced to EDIT,
        # so a model that hallucinates an unknown action type no longer
        # produces a phantom edit candidate.
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
        # Chunk K3: ``evidenceElementId`` is optional. Normalize to None on
        # empty / null so downstream "id is truthy" checks remain simple.
        evidence_raw = item.get("evidenceElementId")
        evidence_id: str | None
        if evidence_raw is None:
            evidence_id = None
        else:
            evidence_id = str(evidence_raw).strip() or None
        # Chunk M: cross-check findings can cite upstream review finding
        # ids and independent raw-spec evidence ids. The review schema does
        # not surface these fields, so review findings parse as empty lists.
        # The cross-check schema lists them as required arrays (possibly
        # empty), so a missing field on a well-formed cross-check payload
        # is unusual but tolerated to keep fallback parsing robust.
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
        # Chunk L: build the structured EditProposal alongside the legacy
        # fields. When the action is REPORT_ONLY the proposal is None and
        # we zero out the edit-shaped legacy fields so a stale quote from a
        # model that filled them in anyway cannot accidentally produce an
        # edit candidate downstream.
        if action in EDIT_ACTION_TYPES:
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
        ))
    return findings


def _stream_review(client: Anthropic, system_prompt: str, user_message: str, *, model: str = MODEL_OPUS_47, max_retries: int = 3, verbose: bool = False, stream_callback: Optional[StreamCallback] = None) -> ReviewResult:
    start_time = time.time()
    result = ReviewResult(model=model)
    # Per-call output cap. Real-time and batch share the same baseline so
    # findings cannot diverge between modes; the 300k extended path is a
    # batch-only API capability (300k beta header is not honored on stream).
    output_limit = review_max_tokens(model=model)
    # Chunk J: phase-aware cache policy. Real-time review uses the
    # PHASE_REVIEW policy (cache=on, ttl=1h). Routing through the phase
    # parameter keeps the policy decision in api_config so a future
    # tuning pass touches one place.
    system_payload = system_prompt_with_cache(system_prompt, phase=PHASE_REVIEW)
    # Phase 2.4: when structured outputs are enabled, force the model to
    # emit a tool_use block whose ``input`` matches the finding schema.
    # ``tool_choice`` removes the "did the model wrap its output in tags?"
    # parse-failure mode entirely.
    use_structured = structured_outputs_enabled()
    request_kwargs: dict = {
        "model": model,
        "max_tokens": output_limit,
        "system": system_payload,
        "messages": [{"role": "user", "content": user_message}],
    }
    apply_thinking_config(request_kwargs, model=model, phase=PHASE_REVIEW)
    if use_structured:
        request_kwargs["tools"] = [review_findings_tool()]
        request_kwargs["tool_choice"] = review_tool_choice()
    last_exception: Exception | None = None
    for attempt in range(max_retries):
        is_last_attempt = attempt == max_retries - 1
        try:
            if verbose:
                print(f"Calling Claude {model} (attempt {attempt + 1}/{max_retries})...")
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

            # Tool-use stops report stop_reason="tool_use", which is the
            # success path when structured outputs are forced.
            if result.stop_reason not in ("end_turn", "tool_use"):
                result.parse_status = "incomplete"
                result.error = f"Response incomplete (stop_reason: {result.stop_reason}). The model likely ran out of output tokens. Partial response preserved in raw_response."
                result.elapsed_seconds = time.time() - start_time
                return result

            structured = _extract_structured_findings(resp) if use_structured else None
            if structured is not None:
                data, thinking = structured
            else:
                data, thinking = _extract_json_array(response_text, stop_reason=result.stop_reason)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.parse_status = "ok"
            result.elapsed_seconds = time.time() - start_time
            return result
        except (RateLimitError, APIConnectionError) as e:
            last_exception = e
            if is_last_attempt:
                break
            time.sleep(2 ** attempt * 5)
        except InternalServerError as e:
            last_exception = e
            if is_last_attempt:
                break
            time.sleep(2 ** attempt * 10)
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 529 or e.__class__.__name__ == "OverloadedError":
                last_exception = e
                if is_last_attempt:
                    break
                time.sleep(2 ** attempt * 10)
                continue
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result
        except APIError as e:
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result
        except Exception as e:
            # Retry transient connection failures, but don't sleep after the
            # final attempt — and surface the underlying exception detail
            # rather than a generic "failed after N attempts" (audit Issue 9).
            if _is_retryable_connection_error(e) and not is_last_attempt:
                backoff = 2 ** attempt * 5
                last_exception = e
                if verbose:
                    print(f"Retryable connection error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            result.error = f"Error: {e}"
            result.parse_status = "parse_error"
            result.elapsed_seconds = time.time() - start_time
            return result
    if last_exception is not None:
        result.error = (
            f"Failed after {max_retries} attempts: "
            f"{type(last_exception).__name__}: {last_exception}"
        )
    else:
        result.error = f"Failed after {max_retries} attempts."
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
    mode: ReviewMode = DEFAULT_REVIEW_MODE,
    paragraph_map=None,
) -> ReviewResult:
    """Stream a real-time review for one spec.

    Chunk K2: callers that have an ``ExtractedSpec`` should forward its
    ``paragraph_map`` so the model sees element ids alongside the spec
    text. Legacy callers (a raw ``spec_content`` string) keep working
    unchanged because the prompt builder falls back to the plain-body
    rendering when no map is provided.
    """
    client = _get_client()
    return _stream_review(
        client,
        get_system_prompt(cycle, mode=mode),
        get_single_spec_user_message(
            spec_content,
            filename,
            project_context=project_context,
            cycle=cycle,
            mode=mode,
            paragraph_map=paragraph_map,
        ),
        model=model,
        max_retries=max_retries,
        verbose=verbose,
        stream_callback=stream_callback,
    )