"""Claude API client for specification review."""
from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..verification.verifier import VerificationResult

from anthropic import Anthropic

from ..core.api_config import (
    REVIEW_MODEL_DEFAULT,  # re-exported for batch/resume/GUI importers
)

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

    Every executable edit must satisfy action-specific field requirements before
    it leaves the parser. The four rules are:

    * ``EDIT``   — non-empty ``existing_text`` and ``replacement_text`` that
      are not byte-for-byte identical (an identical pair is a no-op edit).
    * ``DELETE`` — non-empty ``existing_text``.
    * ``ADD``    — non-empty ``anchor_text`` and ``replacement_text``, plus
      ``insert_position`` in ``{"before", "after"}``.
    * ``REPORT_ONLY`` and any unknown action — None (REPORT_ONLY cleanup is
      the parser's job; unknown actions are coerced to REPORT_ONLY before
      this helper sees them).

    The return value is the short human-readable reason that the parser
    stamps on ``Finding.demotion_reason`` so diagnostics, the report, and
    the edit-instruction sidecar can all explain *why* a finding lost its
    edit slot rather than treating it as a generic REPORT_ONLY.
    """
    norm = (action or "").strip().upper()
    if norm == "EDIT":
        if not (existing_text and existing_text.strip()):
            return "EDIT action missing required existingText"
        if not (replacement_text and replacement_text.strip()):
            return "EDIT action missing required replacementText"
        # Reject a no-op EDIT: replacementText byte-for-byte identical to
        # existingText would reach the sidecar as an instruction a downstream
        # applier executes to no effect (find X, replace with the same X).
        # Demote to REPORT_ONLY so the finding's prose still surfaces but no
        # empty edit instruction is emitted. Exact equality only — a case- or
        # whitespace-only delta is not byte-equal and is intentionally allowed
        # through, since some are genuine fixes (defined-term capitalization,
        # spacing corrections).
        if existing_text == replacement_text:
            return "EDIT action is a no-op (existingText equals replacementText)"
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

    The previous schema forced every finding into
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
      the paragraph / row / heading the proposal targets. Lets a downstream
      applier disambiguate identical text in different sections.
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
    # Optional pointer to
    # the paragraph / row / heading id (see ``ParagraphMapping.element_id``).
    # A downstream applier can prefer this id when it is non-empty and
    # revalidate the exact-text quote against the live element before
    # applying any edit. Empty string is the legacy fallback (text-based
    # matching).
    evidenceElementId: str | None = None
    # The optional structured edit half. Findings with no clean textual fix
    # leave this None and set ``actionType = "REPORT_ONLY"`` (or leave the
    # legacy fields blank). Report rendering and the edit-instruction
    # sidecar route through :meth:`as_edit_proposal` so they see the same
    # value whether the proposal arrived from the new schema slot or was
    # reconstructed from legacy fields at runtime.
    edit_proposal: EditProposal | None = None
    # Stable identifier assigned at dedup time so the report and the
    # edit-instruction sidecar can reference the finding and the
    # cross-check pass can label the prior findings it was shown. Empty
    # string is the legacy path (finding constructed outside the dedup
    # helper).
    finding_id: str = ""
    # When the parser demotes an EDIT / DELETE / ADD action to REPORT_ONLY
    # because action-specific fields were missing, it stamps the short
    # reason here so diagnostics, the report's demoted-edits section, and
    # the edit-instruction sidecar can explain *why* the proposal was
    # rejected instead of treating the finding as a generic REPORT_ONLY.
    # ``None`` is
    # the default — the finding was either a real edit, a native
    # REPORT_ONLY emission, or an unknown action coerced to REPORT_ONLY
    # without a per-action shape requirement to cite.
    demotion_reason: str | None = None
    # When ``_deduplicate_findings`` collapses
    # findings from multiple files into one representative, the original
    # per-file member findings are retained here. Edit execution looks up
    # the matching original by ``fileName`` so the representative's
    # ``existingText`` / ``replacementText`` / ``anchorText`` /
    # ``evidenceElementId`` / ``edit_proposal`` are never fanned out
    # across files that may have different exact text. Singletons leave
    # this empty (the finding *is* its own original); legacy resume
    # payloads from older versions also load empty, in which case a downstream
    # applier sees only the representative's own per-file text.
    occurrence_originals: list["Finding"] = field(default_factory=list)

    def as_edit_proposal(self) -> EditProposal | None:
        """Return the structured edit proposal for this finding, if any.

        When ``edit_proposal`` is set, it is the
        authoritative answer. Otherwise the legacy fields are inspected:
        an actionType of ADD / EDIT / DELETE materializes an ``EditProposal``
        on the fly so older callers (resume-state loads, ad-hoc test
        Findings) keep working. Any other actionType — including the new
        ``REPORT_ONLY`` sentinel and the empty/legacy "no opinion" case —
        returns ``None`` so consumers can branch cleanly on
        "does this finding have a proposal?".

        Also validates action-specific shape requirements
        before returning a proposal. A Finding constructed with an
        EDIT/ADD/DELETE action but missing required fields (e.g.,
        ``actionType="EDIT"`` with ``existingText=None``) returns None
        instead of leaking an unusable proposal into the report or the
        edit-instruction sidecar. Parser callers should never hit this path because
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
    # Phase 2 prompt-caching telemetry. Populated when the API returns
    # cache_creation_input_tokens / cache_read_input_tokens in usage.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    stop_reason: str | None = None
    parse_status: str | None = None
    cross_check_status: str | None = None
    # Chunked cross-check telemetry (in-memory; 0 for non-chunked runs).
    # When a large project is cross-checked per CSI division, a chunk can
    # fail or skip while others complete — the overall ``cross_check_status``
    # is still ``"completed"`` (≥1 chunk produced findings), which would
    # otherwise hide that a division's coordination did not run. These counts
    # let the Run Diagnostics banner flag a partially-incomplete pass
    # (TRUST_AUDIT P1-3 follow-up). ``chunk_failures`` = chunks that errored;
    # ``chunk_skips`` = chunks skipped (e.g. a single division too large to
    # cross-check). Both default 0 so non-chunked results are unaffected.
    chunk_failures: int = 0
    chunk_skips: int = 0
    # When the model invoked the ``submit_review_findings`` tool,
    # this is the raw parsed tool input (the dict the model sent through
    # the schema). Held in memory so diagnostics can preserve the actual
    # structured payload instead of relying on ``raw_response``, which is
    # the text-block concatenation and is empty for tool-use responses.
    # In-memory only — telemetry describes runtime behavior, not durable
    # state.
    structured_payload: dict | None = None

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

    The primary path is tool-use output (the model calls
    ``submit_review_findings``). With ``tool_choice=auto`` the model MAY
    still return plain text — refusals, feature-flag-off runs, and
    occasional adaptive-thinking detours all land here. Strict tool use
    (on by default) grammar-constrains the payload *of a tool call*; it
    does not make the tool call itself contractual, and the
    ``SPEC_CRITIC_STRICT_TOOL_USE=0`` rollback path runs lenient — so this
    fallback stays permanently reachable as defense-in-depth.
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
        # A literal empty-array body is a legitimate "no findings"
        # response, not thinking. Storing ``"[]"`` as the thinking text was
        # a bug that polluted the report's analysis-summary field.
        return [], ""

    raise ValueError(f"Could not extract JSON findings from response (stop_reason: {stop_reason}): {text[:200]}...")


def _parse_findings(data: list) -> list[Finding]:
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "")).strip().upper()
        if sev not in {"CRITICAL", "HIGH", "MEDIUM", "GRIPES"}:
            continue
        # actionType is no longer forced to "EDIT". A finding that
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
        # ``evidenceElementId`` is optional. Normalize to None on
        # empty / null so downstream "id is truthy" checks remain simple.
        evidence_raw = item.get("evidenceElementId")
        evidence_id: str | None
        if evidence_raw is None:
            evidence_id = None
        else:
            evidence_id = str(evidence_raw).strip() or None
        existing_text = (
            str(item.get("existingText")) if item.get("existingText") is not None else None
        )
        replacement_text = (
            str(item.get("replacementText"))
            if item.get("replacementText") is not None
            else None
        )
        # Build the structured EditProposal alongside the legacy
        # fields. When the action is REPORT_ONLY the proposal is None and
        # we zero out the edit-shaped legacy fields so a stale quote from a
        # model that filled them in anyway cannot accidentally produce an
        # edit candidate downstream.
        #
        # If the model claims EDIT/DELETE/ADD but omits an action-specific
        # required field, demote the finding to REPORT_ONLY *here*, stamp
        # a clear ``demotion_reason``, and clear every executable edit
        # field. Downstream consumers (the report and the edit-instruction
        # sidecar) then see a clean REPORT_ONLY and the finding's underlying
        # issue is preserved for the report. The previous behavior
        # silently built an EditProposal with missing fields and pushed
        # the error detection downstream, where it surfaced as vague
        # warnings like "Finding has no anchor text" instead of citing
        # the specific schema field that was missing.
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
            demotion_reason=demotion_reason,
        ))
    return findings

