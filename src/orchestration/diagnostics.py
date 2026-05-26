"""In-memory diagnostics report for Spec Critic pipeline runs."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# Cap retained events so a long-running batch poll cannot grow the in-memory
# report unbounded. Truncation tracking lets the report still surface that
# older events were dropped.
_DEFAULT_MAX_EVENTS = 5000

# Structured tool payloads (the parsed ``submit_review_findings`` /
# ``submit_verification_verdict`` input dicts) preserve "what the model
# actually emitted" alongside the regular telemetry. The serialized form is
# byte-capped so a large findings array on a 50-spec batch run cannot blow
# up diagnostics memory or report size.
_STRUCTURED_PAYLOAD_MAX_BYTES = 4096

# Bounded diagnostics. The event-count cap (above) keeps the list length
# finite, but a single event can still carry a multi-megabyte field (long
# raw_response, sprawling source list, etc.). These caps put a hard byte
# ceiling on per-event data and on the cumulative data footprint so a
# pathological prompt cannot bloat memory or blow up JSON exports.
_DEFAULT_MAX_EVENT_DATA_BYTES = 16 * 1024     # 16 KiB per event
_DEFAULT_MAX_TOTAL_DATA_BYTES = 8 * 1024 * 1024  # 8 MiB total across all events
_MAX_STRING_FIELD_BYTES = 4 * 1024            # 4 KiB per individual string field
_TRUNCATION_MARKER = "...(truncated)"

# Secrets scrub. Diagnostics never need to retain credentials, so any data
# field whose key looks secret-shaped or whose value matches a well-known
# secret prefix is replaced with ``"<redacted>"``. The patterns below are
# deliberately conservative — false positives only obscure data; false
# negatives leak credentials.
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|passwd|auth|bearer|access[_-]?token|"
    r"private[_-]?key|credentials?|client[_-]?secret|x[_-]?api[_-]?key)",
    re.IGNORECASE,
)
# Anthropic API keys begin with ``sk-ant-``; OpenAI / cloud keys often begin
# with ``sk-`` or ``AKIA``. Pattern matches a prefix followed by a long
# token-shaped run of safe characters.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{12,}", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
_REDACTED = "<redacted>"


def _truncate_string(value: str, *, max_bytes: int = _MAX_STRING_FIELD_BYTES) -> str:
    """Cap ``value`` to ``max_bytes`` of UTF-8 with a visible marker.

    Splits cleanly on a UTF-8 boundary so the result stays decodable.
    """
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut + _TRUNCATION_MARKER


def _scrub_value(value: Any) -> Any:
    """Replace ``value`` with ``"<redacted>"`` when it looks like a secret."""
    if not isinstance(value, str):
        return value
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return _REDACTED
    return value


def _scrub_and_bound(data: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub secrets and cap long string fields.

    ``_depth`` is bounded so a cyclic dict cannot loop forever (the
    JSON serializer would also catch this, but the early exit avoids
    paying for it). The recursion is bounded at six levels — deeper
    nesting is replaced with its ``repr()`` so the field is still
    visible without escaping the bound.
    """
    if _depth > 6:
        return _truncate_string(repr(data))
    if isinstance(data, dict):
        out: dict = {}
        for key, value in data.items():
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                out[key] = _REDACTED
                continue
            out[key] = _scrub_and_bound(value, _depth=_depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        scrubbed = [_scrub_and_bound(v, _depth=_depth + 1) for v in data]
        return scrubbed if isinstance(data, list) else tuple(scrubbed)
    if isinstance(data, str):
        return _truncate_string(_scrub_value(data))
    return data


def _event_data_byte_size(data: Optional[dict]) -> int:
    """Approximate JSON byte size of an event's data dict (for cap tracking)."""
    if not data:
        return 0
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(repr(data).encode("utf-8"))


def _bound_event_data(
    data: Optional[dict], *, max_bytes: int = _DEFAULT_MAX_EVENT_DATA_BYTES
) -> tuple[Optional[dict], bool, int]:
    """Scrub secrets and cap a single event's data payload by byte size.

    Returns ``(scrubbed_dict, truncated, redaction_count)``:

    - ``scrubbed_dict`` — the bounded, JSON-serializable payload.
    - ``truncated`` — ``True`` when any field was reduced (string
      truncation, secret redaction, or whole-field eviction).
    - ``redaction_count`` — number of fields whose value was replaced
      with ``<redacted>`` during scrubbing. Captured *before* byte-cap
      eviction so a small per-event cap cannot mask the fact that
      secret values were observed.
    """
    if not data:
        return data, False, 0
    scrubbed = _scrub_and_bound(data)
    truncated = scrubbed != data
    redaction_count = 0
    if scrubbed is not None:
        try:
            redaction_count = json.dumps(
                scrubbed, ensure_ascii=False, default=str
            ).count(_REDACTED)
        except (TypeError, ValueError):
            redaction_count = 0
    size = _event_data_byte_size(scrubbed)
    if size <= max_bytes:
        return scrubbed, truncated, redaction_count

    # Whole-event still over cap. Drop the largest *string-shaped* fields
    # first; never touch numeric telemetry (input_tokens, output_tokens,
    # etc.) because the summary rollup parses those with ``int(...)`` and
    # a marker string would crash it. Numbers are tiny by definition, so
    # excluding them from eviction does not weaken the cap.
    safe: dict = {}
    if isinstance(scrubbed, dict):
        safe.update(scrubbed)
        # Only evict containers / strings. Booleans / ints / floats /
        # ``None`` are left alone — they're typically <8 bytes each so
        # they cannot be the source of the overrun.
        evictable_keys = [
            k for k, v in safe.items()
            if isinstance(v, (str, list, tuple, dict))
            and k not in ("api_call", "model", "call_mode", "retry_status")
        ]
        sized = sorted(
            evictable_keys,
            key=lambda k: _event_data_byte_size({k: safe[k]}),
            reverse=True,
        )
        for k in sized:
            if _event_data_byte_size(safe) <= max_bytes:
                break
            safe[k] = _TRUNCATION_MARKER
        safe.setdefault("_event_truncated", True)
        return safe, True, redaction_count
    return scrubbed, True, redaction_count


def bound_structured_payload(
    payload: object, *, max_bytes: int = _STRUCTURED_PAYLOAD_MAX_BYTES
) -> dict | None:
    """Serialize a structured tool payload into a byte-bounded diagnostic record.

    Returns ``None`` when there is nothing useful to record. Otherwise
    returns a dict carrying a JSON serialization of the payload, the
    serialized byte length, and a ``truncated`` flag. The serialized
    form is intentionally a string field so the byte cap is enforced
    even when the same payload would later be re-serialized for the
    on-disk diagnostics report.
    """
    if payload is None:
        return None
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    encoded = serialized.encode("utf-8", errors="replace")
    truncated = False
    if len(encoded) > max_bytes:
        truncated = True
        cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
        serialized = cut + "...(truncated)"
        encoded_len = len(serialized.encode("utf-8", errors="replace"))
    else:
        encoded_len = len(encoded)
    return {
        "serialized": serialized,
        "bytes": encoded_len,
        "truncated": truncated,
    }


@dataclass
class DiagnosticEvent:
    timestamp: float
    elapsed: float
    phase: str
    level: str
    message: str
    data: Optional[dict] = None


@dataclass
class DiagnosticsReport:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    mode: str = ""
    model: str = ""
    cycle_label: str = ""
    files_selected: list[str] = field(default_factory=list)
    project_context_tokens: int = 0
    cross_check_enabled: bool = False
    events: list[DiagnosticEvent] = field(default_factory=list)
    # Actionable fields. Populated by the pipeline / GUI when the
    # corresponding phase records actionable failure or skip information.
    failed_specs: list[str] = field(default_factory=list)
    skipped_specs: list[str] = field(default_factory=list)
    edit_skip_reasons: dict[str, int] = field(default_factory=dict)
    ambiguous_locator_count: int = 0
    edits_applied_total: int = 0
    edits_skipped_total: int = 0
    edits_failed_total: int = 0
    # Locator-method telemetry. Surfaces how many findings the locator
    # resolved via the id path vs. text-based fallbacks. Keys are
    # :attr:`EditLocation.match_method` values (``"id"`` / ``"exact"`` /
    # ``"normalized"`` / ``"section_anchored"`` / ``"fuzzy"``) so the
    # breakdown reads cleanly in diagnostics.
    locator_methods: dict[str, int] = field(default_factory=dict)
    # Phase 1 / Step 1.1 of the auto-apply quality plan: count of edits
    # whose replacement text was rewritten to match the source
    # document's typographic conventions (curly vs straight quotes,
    # em-dash vs hyphen, NBSP in measurements, etc.) before being
    # applied. Aggregated from ``EditReport.replacement_normalized_count``
    # by :func:`apply_edits.execute_edit_plan`.
    replacement_text_normalized_count: int = 0
    # Phase 1 / Step 1.2: count of edits whose trailing punctuation was
    # repaired (drop avoidance or doubling prevention) before being
    # applied. Aggregated from
    # ``EditReport.punctuation_boundary_fixed_count`` by
    # :func:`apply_edits.execute_edit_plan`.
    punctuation_boundary_fixed_count: int = 0
    # Phase 2 / Step 2.2: count of ADD findings that the auto-apply
    # pipeline refused at apply time because the recorded
    # ``insertPosition`` was missing or invalid ("before" / "after" are
    # the only acceptable values). The parser normally demotes such
    # ADDs at parse time via :func:`validate_edit_shape`; the
    # apply-layer counter is a defensive net for legacy resume payloads
    # or directly-constructed Findings that bypass parsing. Aggregated
    # from ``EditReport.add_demoted_missing_position_count`` by
    # :func:`apply_edits.execute_edit_plan`.
    add_demoted_missing_position_count: int = 0
    # Phase 3 / Step 3.2: count of token spans that the auto-apply
    # pipeline re-bolded after a partial replacement crossed runs with
    # distinct formatting. Default-off via
    # ``SPEC_CRITIC_RESTORE_KNOWN_FORMATTING``; non-zero only when an
    # operator opted in and a recognized standards / code token
    # (``NFPA 13``, ``CBC 2025``, ``Section 23 21 13``, …) landed
    # inside the replacement span of a paragraph whose original runs
    # carried inline emphasis.
    known_pattern_formatting_restored_count: int = 0
    # Phase 4 / Step 4.1: count of narrower edits whose correction was
    # silently discarded by a broader containing edit whose replacement
    # did NOT carry the narrower's text forward. The broader edit still
    # applies (preserving user agency); this counter surfaces "you
    # probably want to revisit these manually" at the run level so the
    # report and diagnostics can flag the loss. Aggregated from
    # ``EditReport.contained_edits_lost_intent_count`` by
    # :func:`apply_edits.execute_edit_plan`.
    contained_edits_lost_intent_count: int = 0
    # Phase 4 / Step 4.3: count of findings whose cross-paragraph
    # ``existingText`` matched more than one identical window in the
    # document, so the locator refused to guess which one to edit and
    # routed the finding to manual review. Separate from the general
    # ``ambiguous_locator_count`` because cross-paragraph multi-window
    # ambiguity is a distinct signal (the model emitted a repeated
    # multi-paragraph quote) that operators may want to act on
    # differently than ordinary single-paragraph ambiguity.
    # Aggregated from
    # ``LocatorResult.cross_paragraph_ambiguous`` by
    # :func:`apply_edits.execute_edit_plan`.
    cross_paragraph_ambiguity_routed_to_manual_count: int = 0
    # Phase 5 / Step 5.1: count of CORRECTED findings whose
    # ``verification.correction`` failed the replaceability sanity
    # check (``correction_looks_replaceable``), so the applied edit
    # used the model's original ``replacement_text`` instead of the
    # verifier's correction. The verifier's correction is still
    # preserved on the result for the report — only the *applied* edit
    # text changes. Non-zero values are a useful "the verifier emitted
    # explanation, not clean replacement text" signal so operators can
    # revisit those findings manually. Aggregated from
    # ``EditReport.verifier_correction_rejected_as_replacement_count``
    # by :func:`apply_edits.execute_edit_plan`.
    verifier_correction_rejected_as_replacement_count: int = 0
    max_events: int = _DEFAULT_MAX_EVENTS
    events_dropped: int = 0
    # Diagnostic byte caps. Prevent a single event from blowing up
    # in-memory size and prevent the cumulative event payload from growing
    # unbounded. Both caps default to conservative ceilings; operators that
    # need every byte of detail can bump them per-instance.
    max_event_data_bytes: int = _DEFAULT_MAX_EVENT_DATA_BYTES
    max_total_data_bytes: int = _DEFAULT_MAX_TOTAL_DATA_BYTES
    total_data_bytes: int = 0
    events_truncated_by_size: int = 0
    secrets_redacted: int = 0
    bytes_dropped: int = 0

    def _accept_event_data(self, data: Optional[dict]) -> tuple[Optional[dict], int]:
        """Apply per-event byte caps + secret scrubbing.

        Returns the bounded ``(data, byte_size)`` tuple. Tracks the global
        ``secrets_redacted`` counter so the summary can flag that scrubbing
        actually fired during the run.
        """
        if not data:
            return data, 0
        bounded, was_truncated, redactions = _bound_event_data(
            data, max_bytes=self.max_event_data_bytes
        )
        # ``was_truncated`` covers any kind of reduction (scrub, string
        # truncation, whole-field eviction). Only the whole-event byte
        # eviction is what users care about as "size truncation" — we
        # increment that counter only when the per-event cap was hit.
        if bounded is not None and _event_data_byte_size(data) > self.max_event_data_bytes:
            self.events_truncated_by_size += 1
        elif was_truncated and isinstance(bounded, dict) and bounded.get("_event_truncated"):
            self.events_truncated_by_size += 1
        if redactions:
            self.secrets_redacted += redactions
        size = _event_data_byte_size(bounded)
        return bounded, size

    def _enforce_total_byte_cap(self) -> None:
        """Drop oldest events until cumulative byte usage fits the cap.

        Runs after every append so the running ``total_data_bytes`` counter
        is the same as ``sum(_event_data_byte_size(e.data) for e in events)``
        modulo the events that have already been evicted.
        """
        cap = self.max_total_data_bytes
        if cap <= 0:
            return
        while self.events and self.total_data_bytes > cap:
            oldest = self.events.pop(0)
            evicted_size = _event_data_byte_size(oldest.data)
            self.total_data_bytes = max(0, self.total_data_bytes - evicted_size)
            self.events_dropped += 1
            self.bytes_dropped += evicted_size

    def log(self, phase: str, level: str, message: str, data: Optional[dict] = None) -> None:
        # Cap the event list to bound memory on long-running batch polls.
        # When the cap is exceeded, drop the oldest event and remember that
        # truncation happened so the summary can flag it.
        if self.max_events > 0 and len(self.events) >= self.max_events:
            oldest = self.events.pop(0)
            self.events_dropped += 1
            self.total_data_bytes = max(
                0, self.total_data_bytes - _event_data_byte_size(oldest.data)
            )
        bounded_data, byte_size = self._accept_event_data(data)
        self.events.append(DiagnosticEvent(
            timestamp=time.time(),
            elapsed=time.time() - self.started_at,
            phase=phase,
            level=level,
            message=message,
            data=bounded_data,
        ))
        self.total_data_bytes += byte_size
        self._enforce_total_byte_cap()

    def record_failed_spec(self, filename: str) -> None:
        if filename and filename not in self.failed_specs:
            self.failed_specs.append(filename)

    def record_skipped_spec(self, filename: str) -> None:
        if filename and filename not in self.skipped_specs:
            self.skipped_specs.append(filename)

    def record_edit_skip(self, reason: str) -> None:
        if not reason:
            return
        self.edit_skip_reasons[reason] = self.edit_skip_reasons.get(reason, 0) + 1
        if reason == "ambiguous":
            self.ambiguous_locator_count += 1

    def record_edit_report(
        self,
        *,
        applied: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> None:
        self.edits_applied_total += int(applied)
        self.edits_skipped_total += int(skipped)
        self.edits_failed_total += int(failed)

    def _auto_apply_quality_lines(self) -> list[str]:
        """Render the Phase 1 auto-apply quality counters.

        Returns an empty list when every counter is zero so
        :meth:`to_text` can skip the section header for runs that did
        not exercise the edit pipeline. New counters added by later
        Phase 1 / 2 steps land here as additional one-liners.
        """
        rows: list[tuple[str, int]] = [
            ("Replacement text normalized", self.replacement_text_normalized_count),
            ("Punctuation boundary fixed", self.punctuation_boundary_fixed_count),
            (
                "ADD demoted (missing insertPosition)",
                self.add_demoted_missing_position_count,
            ),
            (
                "Known-pattern formatting restored",
                self.known_pattern_formatting_restored_count,
            ),
            (
                "Contained-edit intent loss",
                self.contained_edits_lost_intent_count,
            ),
            (
                "Cross-paragraph ambiguity routed to manual",
                self.cross_paragraph_ambiguity_routed_to_manual_count,
            ),
            (
                "Verifier correction rejected as replacement",
                self.verifier_correction_rejected_as_replacement_count,
            ),
        ]
        active = [(label, count) for label, count in rows if count]
        if not active:
            return []
        return [f"  {label:35s} {count}" for label, count in active]

    def record_locator_method(self, method: str) -> None:
        """Count how many findings used each locator method.

        Called by ``apply_edits.execute_edit_plan`` for every successful
        locator match. The higher the ``id`` bucket's share, the less the
        pipeline depends on fuzzy text rediscovery.
        """
        if not method:
            return
        self.locator_methods[method] = self.locator_methods.get(method, 0) + 1

    def record_api_call(
        self,
        *,
        phase: str,
        model: str = "",
        message: str = "",
        level: str = "info",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        web_search_requests: int = 0,
        max_output_tokens: int = 0,
        stop_reason: str | None = None,
        mode: str | None = None,           # "realtime" | "batch"
        retry_status: str | None = None,   # "initial" | "retry" | "continuation"
        structured_payload: object = None,
        extra: dict | None = None,
    ) -> None:
        """Record a single Anthropic API call with normalized telemetry data.

        Every Anthropic call records phase / model / token usage / cache
        usage / web-search count / batch-vs-realtime / retry-status under
        one consistent key set so the per-phase rollup in :meth:`summary`
        can answer "which phases cost the most?" and "which phases get
        cache hits?" without each call site re-inventing the data shape.

        ``structured_payload`` is the parsed tool input dict from
        ``submit_review_findings`` / ``submit_verification_verdict`` when
        the model invoked the custom tool. It is byte-bounded via
        :func:`bound_structured_payload` before being recorded so a large
        findings array cannot blow up the diagnostics in-memory footprint.

        ``extra`` is merged in last and can carry call-specific fields
        (severity counts, verification_mode, etc.) without overriding any of
        the standard telemetry keys.
        """
        data: dict = {
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_creation_input_tokens": int(cache_creation_input_tokens or 0),
            "cache_read_input_tokens": int(cache_read_input_tokens or 0),
            "web_search_requests": int(web_search_requests or 0),
            "max_output_tokens": int(max_output_tokens or 0),
            "stop_reason": stop_reason,
            "api_call": True,
        }
        if mode is not None:
            data["call_mode"] = mode
        if retry_status is not None:
            data["retry_status"] = retry_status
        bounded = bound_structured_payload(structured_payload)
        if bounded is not None:
            data["structured_payload"] = bounded
        if extra:
            for k, v in extra.items():
                data.setdefault(k, v)
        self.log(phase, level, message or f"API call ({phase})", data)

    def finish(self) -> None:
        if self.ended_at is None:
            self.ended_at = time.time()

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        total_time = (self.ended_at or time.time()) - self.started_at
        error_events = [e for e in self.events if e.level == "error"]
        warning_events = [e for e in self.events if e.level == "warning"]
        success_events = [e for e in self.events if e.level == "success"]

        # Phase durations: first event to last event per phase
        phase_times: dict[str, dict] = {}
        for e in self.events:
            if e.phase not in phase_times:
                phase_times[e.phase] = {"start": e.elapsed, "end": e.elapsed}
            else:
                phase_times[e.phase]["end"] = e.elapsed

            if e.data and any(key in e.data for key in ("verdict", "confidence")):
                synthetic = phase_times.setdefault("verification", {"start": e.elapsed, "end": e.elapsed})
                synthetic["start"] = min(synthetic["start"], e.elapsed)
                synthetic["end"] = max(synthetic["end"], e.elapsed)
        phase_durations = {
            p: round(t["end"] - t["start"], 2) for p, t in phase_times.items()
        }

        # Aggregate token data from events
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        total_web_search_requests = 0
        # Output-size and search-budget telemetry. We track the maximum
        # output observed per phase, the count of truncated calls
        # (stop_reason != end_turn), and aggregate search budget consumption
        # so future tuning has data to draw on.
        output_samples: list[int] = []
        output_max_by_phase: dict[str, int] = {}
        truncated_calls = 0
        truncated_phases: dict[str, int] = {}
        max_output_cap_observed = 0
        # Per-phase rollup so the summary can answer "which phases cost the
        # most?" / "which phases get cache hits?" / "how many retries/
        # continuations occurred?" / "which phases were batch vs real-time?"
        # without re-walking events. Each phase entry accumulates calls,
        # tokens, cache usage, search requests, and retry/continuation/
        # realtime/batch counts as the loop runs.
        def _new_phase_bucket() -> dict:
            return {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "web_search_requests": 0,
                "models": [],          # ordered, deduped
                "retries": 0,
                "continuations": 0,
                "realtime_calls": 0,
                "batch_calls": 0,
                "truncated_calls": 0,
            }
        phase_telemetry: dict[str, dict] = {}
        for e in self.events:
            if not e.data:
                continue
            in_tok = int(e.data.get("input_tokens", 0) or 0)
            out_tok = int(e.data.get("output_tokens", 0) or 0)
            cache_create = int(e.data.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(e.data.get("cache_read_input_tokens", 0) or 0)
            search_count = int(e.data.get("web_search_requests", 0) or 0)
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            total_cache_creation_tokens += cache_create
            total_cache_read_tokens += cache_read
            total_web_search_requests += search_count
            if out_tok > 0:
                output_samples.append(out_tok)
                phase_max = output_max_by_phase.get(e.phase, 0)
                if out_tok > phase_max:
                    output_max_by_phase[e.phase] = out_tok
            stop_reason = e.data.get("stop_reason")
            is_truncated = bool(
                stop_reason and stop_reason not in ("end_turn", "tool_use", None)
            )
            if is_truncated:
                truncated_calls += 1
                truncated_phases[e.phase] = truncated_phases.get(e.phase, 0) + 1
            cap = int(e.data.get("max_output_tokens", 0) or 0)
            if cap > max_output_cap_observed:
                max_output_cap_observed = cap

            # Per-phase rollup. Only events that look like API calls (have
            # any of the token/search/api_call markers) contribute to the
            # phase ``calls`` counter so phase-tagged informational logs do
            # not inflate the count.
            looks_like_api_call = bool(
                e.data.get("api_call")
                or in_tok
                or out_tok
                or cache_create
                or cache_read
                or search_count
                or e.data.get("model")
            )
            if not looks_like_api_call:
                continue
            bucket = phase_telemetry.setdefault(e.phase, _new_phase_bucket())
            bucket["calls"] += 1
            bucket["input_tokens"] += in_tok
            bucket["output_tokens"] += out_tok
            bucket["cache_creation_input_tokens"] += cache_create
            bucket["cache_read_input_tokens"] += cache_read
            bucket["web_search_requests"] += search_count
            model = str(e.data.get("model") or "").strip()
            if model and model not in bucket["models"]:
                bucket["models"].append(model)
            retry_status = str(e.data.get("retry_status") or "").lower()
            if retry_status == "retry":
                bucket["retries"] += 1
            elif retry_status == "continuation":
                bucket["continuations"] += 1
            call_mode = str(e.data.get("call_mode") or "").lower()
            if call_mode == "realtime":
                bucket["realtime_calls"] += 1
            elif call_mode == "batch":
                bucket["batch_calls"] += 1
            if is_truncated:
                bucket["truncated_calls"] += 1

        # Verification verdict breakdown + evidence telemetry
        verdicts: dict[str, int] = {}
        verification_stats = {
            "grounded": 0,
            "ungrounded": 0,
            "escalated": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "local_skips": 0,
            "search_errors": 0,
            "search_requests": 0,
        }
        # Escalation telemetry rollup. The verifier records before-and-after
        # fields on every result that triggered the Sonnet -> Opus escalation
        # path; aggregating them here answers "is escalation actually
        # changing verdicts?".
        #
        # - ``attempts`` counts findings where a second pass ran, regardless
        #   of whether the escalated result was kept.
        # - ``changed_verdict`` counts findings whose final verdict differs
        #   from the initial Sonnet verdict.
        # - ``no_change`` is the difference, useful for spotting wasted
        #   escalations.
        # - ``by_reason`` buckets attempts by the router's classification
        #   tag (``initial_unverified`` / ``initial_ungrounded`` / etc.).
        # - ``by_initial_verdict`` and ``by_final_verdict`` track the
        #   verdict transitions for a future routing-tuning pass.
        # - ``by_severity`` reports counts per finding severity so a
        #   future routing-tuning pass can see whether escalation pays
        #   off more on CRITICAL than HIGH.
        escalation_stats = {
            "attempts": 0,
            "changed_verdict": 0,
            "no_change": 0,
            "by_reason": {},          # type: dict[str, int]
            "by_initial_verdict": {}, # type: dict[str, int]
            "by_final_verdict": {},   # type: dict[str, int]
            "by_severity": {},        # type: dict[str, int]
        }
        # Per-mode counter. Keys are :class:`VerificationMode` string values;
        # missing-mode events are bucketed under ``"unknown"`` so an entry
        # without a mode is still visible.
        verification_modes: dict[str, int] = {}
        # Per-profile counter so reports can tell at a glance which kinds of
        # claims dominated a run.
        verification_profiles: dict[str, int] = {}
        # Retry / continuation telemetry. Aggregates the ``retry_telemetry``
        # block the verifier stamps onto findings that hit retries, the
        # continuation cap, or terminal-unverified via the wave failure
        # tracker. Empty on runs where no finding consumed a retry.
        retry_stats = {
            "findings_with_retries": 0,
            "total_retry_attempts": 0,
            "total_continuations": 0,
            "by_failure_class": {},   # type: dict[str, int]
            "by_terminal_reason": {}, # type: dict[str, int]
        }
        for e in self.events:
            if e.data and "verdict" in e.data:
                v = e.data["verdict"]
                verdicts[v] = verdicts.get(v, 0) + 1
                # Optional evidence fields. Missing keys are simply ignored.
                if e.data.get("grounded") is True:
                    verification_stats["grounded"] += 1
                elif "grounded" in e.data:
                    verification_stats["ungrounded"] += 1
                if e.data.get("escalated") is True:
                    verification_stats["escalated"] += 1
                cs = e.data.get("cache_status")
                if cs == "hit":
                    verification_stats["cache_hits"] += 1
                elif cs == "miss":
                    verification_stats["cache_misses"] += 1
                elif cs == "local_skip":
                    verification_stats["local_skips"] += 1
                verification_stats["search_errors"] += int(e.data.get("search_error_count", 0) or 0)
                verification_stats["search_requests"] += int(e.data.get("web_search_requests", 0) or 0)
                mode_key = str(e.data.get("verification_mode") or "unknown")
                verification_modes[mode_key] = verification_modes.get(mode_key, 0) + 1
                profile_key = str(e.data.get("verification_profile") or "unknown")
                verification_profiles[profile_key] = verification_profiles.get(profile_key, 0) + 1
                # Aggregate escalation telemetry per-finding. Missing keys
                # (non-escalation events) are treated as
                # ``escalation_attempted=False`` and silently skipped.
                if e.data.get("escalation_attempted") is True:
                    escalation_stats["attempts"] += 1
                    if e.data.get("escalation_changed_verdict") is True:
                        escalation_stats["changed_verdict"] += 1
                    else:
                        escalation_stats["no_change"] += 1
                    reason_key = str(e.data.get("escalation_reason") or "unknown")
                    escalation_stats["by_reason"][reason_key] = (
                        escalation_stats["by_reason"].get(reason_key, 0) + 1
                    )
                    iv_key = str(e.data.get("initial_verdict") or "unknown")
                    escalation_stats["by_initial_verdict"][iv_key] = (
                        escalation_stats["by_initial_verdict"].get(iv_key, 0) + 1
                    )
                    fv_key = str(e.data.get("verdict") or "unknown")
                    escalation_stats["by_final_verdict"][fv_key] = (
                        escalation_stats["by_final_verdict"].get(fv_key, 0) + 1
                    )
                    sev_key = str(e.data.get("finding_severity") or "unknown")
                    escalation_stats["by_severity"][sev_key] = (
                        escalation_stats["by_severity"].get(sev_key, 0) + 1
                    )
                # Aggregate retry_telemetry. ``None`` is the default
                # (success path with no retries); only events carrying a
                # non-empty dict count toward the rollup so the "no
                # retries observed" run still shows zeros.
                rt = e.data.get("retry_telemetry") or None
                if isinstance(rt, dict) and rt:
                    attempts_count = int(rt.get("attempts", 0) or 0)
                    cont_count = int(rt.get("continuation_count", 0) or 0)
                    if attempts_count or cont_count:
                        retry_stats["findings_with_retries"] += 1
                        retry_stats["total_retry_attempts"] += attempts_count
                        retry_stats["total_continuations"] += cont_count
                        fc = rt.get("failure_class")
                        if fc:
                            fc_key = str(fc)
                            retry_stats["by_failure_class"][fc_key] = (
                                retry_stats["by_failure_class"].get(fc_key, 0) + 1
                            )
                        tr = rt.get("terminal_reason")
                        if tr:
                            tr_key = str(tr)
                            retry_stats["by_terminal_reason"][tr_key] = (
                                retry_stats["by_terminal_reason"].get(tr_key, 0) + 1
                            )

        # Search-budget telemetry. We aggregate per-finding search-request
        # counts so a future tuning pass can see whether the default
        # ``max_uses`` is over- or under-allocated. Findings with zero
        # web-search activity (local-skip / cache hit) are excluded so the
        # budget percentile reflects calls that actually used the tool.
        search_budget_samples: list[int] = []
        budget_ceiling = 0
        try:
            from ..core.api_config import DEFAULT_VERIFICATION_MAX_USES
            budget_ceiling = int(DEFAULT_VERIFICATION_MAX_USES)
        except Exception:
            budget_ceiling = 0
        budget_saturated = 0
        for e in self.events:
            if not e.data or "verdict" not in e.data:
                continue
            if (e.data.get("cache_status") or "") in {"hit", "local_skip"}:
                continue
            requests = int(e.data.get("web_search_requests", 0) or 0)
            if requests <= 0:
                continue
            search_budget_samples.append(requests)
            if budget_ceiling and requests >= budget_ceiling:
                budget_saturated += 1

        def _percentile(values: list[int], pct: float) -> int:
            if not values:
                return 0
            ordered = sorted(values)
            idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
            return ordered[idx]

        search_budget = {
            "samples": len(search_budget_samples),
            "ceiling": budget_ceiling,
            "saturated_calls": budget_saturated,
            "max_observed": max(search_budget_samples) if search_budget_samples else 0,
            "p50": _percentile(search_budget_samples, 50),
            "p95": _percentile(search_budget_samples, 95),
            "total": sum(search_budget_samples),
        }
        output_telemetry = {
            "samples": len(output_samples),
            "max_observed": max(output_samples) if output_samples else 0,
            "p50": _percentile(output_samples, 50),
            "p95": _percentile(output_samples, 95),
            "max_by_phase": dict(output_max_by_phase),
            "truncated_calls": truncated_calls,
            "truncated_by_phase": dict(truncated_phases),
            "max_cap_observed": max_output_cap_observed,
        }

        # Finding severity breakdown
        severities: dict[str, int] = {}
        for e in self.events:
            if e.data and "severity_counts" in e.data:
                for sev, cnt in e.data["severity_counts"].items():
                    severities[sev] = severities.get(sev, 0) + cnt

        # Derive per-phase ``cache_hit_ratio`` and a cross-phase rollup so
        # a glance at the summary tells you whether caching is actually
        # paying off. Ratio is reads / (reads + creates), the same shape
        # Anthropic recommends for prompt-cache effectiveness.
        for bucket in phase_telemetry.values():
            denom = bucket["cache_creation_input_tokens"] + bucket["cache_read_input_tokens"]
            bucket["cache_hit_ratio"] = (
                round(bucket["cache_read_input_tokens"] / denom, 4) if denom else 0.0
            )
        cache_total = total_cache_creation_tokens + total_cache_read_tokens
        cost_summary = {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_creation_input_tokens": total_cache_creation_tokens,
            "total_cache_read_input_tokens": total_cache_read_tokens,
            "total_web_search_requests": total_web_search_requests,
            "cache_hit_ratio": (
                round(total_cache_read_tokens / cache_total, 4) if cache_total else 0.0
            ),
            "phases": dict(phase_telemetry),
        }

        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "cycle_label": self.cycle_label,
            "total_time_seconds": round(total_time, 2),
            "files_selected": len(self.files_selected),
            "total_events": len(self.events),
            "errors": len(error_events),
            "warnings": len(warning_events),
            "successes": len(success_events),
            "phase_durations": phase_durations,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_creation_input_tokens": total_cache_creation_tokens,
            "total_cache_read_input_tokens": total_cache_read_tokens,
            "total_web_search_requests": total_web_search_requests,
            "verification_verdicts": verdicts,
            "verification_evidence": verification_stats,
            # Explicit routing visibility. Per-mode counts answer "which
            # path handled how many findings?"; per-profile counts answer
            # "what kind of claims dominated?".
            "verification_modes": verification_modes,
            "verification_profiles": verification_profiles,
            # Escalation telemetry rollup. ``escalation_stats`` carries the
            # per-reason / per-severity / per-verdict counts plus a derived
            # ``change_rate`` so an operator can answer "is the Sonnet ->
            # Opus escalation actually paying off?" at a glance.
            # ``change_rate`` is 0.0 on runs with no escalation attempts
            # (avoiding divide-by-zero) and is rounded to four decimal
            # places for stable JSON output.
            "escalation_stats": {
                **escalation_stats,
                "change_rate": (
                    round(
                        escalation_stats["changed_verdict"]
                        / escalation_stats["attempts"],
                        4,
                    )
                    if escalation_stats["attempts"]
                    else 0.0
                ),
            },
            # Retry / continuation telemetry rollup. The individual fields
            # are populated from per-finding ``retry_telemetry`` blocks the
            # verifier stamps on outcomes that hit the wave tracker, the
            # continuation cap, or the unresolved-tail branch. Empty
            # by-class / by-reason dicts on runs where no retry occurred.
            "retry_stats": retry_stats,
            "search_budget": search_budget,
            "output_telemetry": output_telemetry,
            "severity_counts": severities,
            # Per-phase telemetry rollup + a small ``cost_summary`` block.
            # ``phase_telemetry`` is the per-phase breakdown; ``cost_summary``
            # echoes the cross-phase totals plus the global cache hit ratio
            # so reports do not have to recompute them.
            "phase_telemetry": dict(phase_telemetry),
            "cost_summary": cost_summary,
            "failed_specs": list(self.failed_specs),
            "skipped_specs": list(self.skipped_specs),
            "edit_skip_reasons": dict(self.edit_skip_reasons),
            "ambiguous_locator_count": self.ambiguous_locator_count,
            "edits_applied_total": self.edits_applied_total,
            "edits_skipped_total": self.edits_skipped_total,
            "edits_failed_total": self.edits_failed_total,
            "locator_methods": dict(self.locator_methods),
            "events_dropped": self.events_dropped,
            # Diagnostics-cap visibility. Operators can see at a glance
            # whether the run hit the per-event byte cap or had any
            # secret-shaped values scrubbed.
            "events_truncated_by_size": self.events_truncated_by_size,
            "secrets_redacted": self.secrets_redacted,
            "bytes_dropped": self.bytes_dropped,
            "total_data_bytes": self.total_data_bytes,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _event_to_dict(self, e: DiagnosticEvent) -> dict:
        d: dict = {
            "timestamp": e.timestamp,
            "elapsed": round(e.elapsed, 3),
            "phase": e.phase,
            "level": e.level,
            "message": e.message,
        }
        if e.data:
            d["data"] = e.data
        return d

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("SPEC CRITIC — DIAGNOSTICS REPORT")
        lines.append("=" * 72)
        lines.append("")

        # Config
        lines.append("RUN CONFIGURATION")
        lines.append("-" * 40)
        lines.append(f"  Run ID:          {self.run_id}")
        lines.append(f"  Mode:            {self.mode}")
        lines.append(f"  Model:           {self.model}")
        lines.append(f"  Code Cycle:      {self.cycle_label}")
        lines.append(f"  Files:           {len(self.files_selected)}")
        for f in self.files_selected:
            lines.append(f"                   - {f}")
        lines.append(f"  Context Tokens:  {self.project_context_tokens:,}")
        lines.append(f"  Cross-Check:     {'Yes' if self.cross_check_enabled else 'No'}")
        started = datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"  Started:         {started}")
        if self.ended_at:
            ended = datetime.fromtimestamp(self.ended_at).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"  Ended:           {ended}")
            lines.append(f"  Duration:        {self.ended_at - self.started_at:.1f}s")
        lines.append("")

        # Summary
        s = self.summary()
        lines.append("SUMMARY")
        lines.append("-" * 40)
        lines.append(f"  Total Time:      {s['total_time_seconds']:.1f}s")
        lines.append(f"  Events:          {s['total_events']}")
        lines.append(f"  Errors:          {s['errors']}")
        lines.append(f"  Warnings:        {s['warnings']}")
        lines.append(f"  Input Tokens:    {s['total_input_tokens']:,}")
        lines.append(f"  Output Tokens:   {s['total_output_tokens']:,}")
        cache_create = s.get("total_cache_creation_input_tokens", 0)
        cache_read = s.get("total_cache_read_input_tokens", 0)
        if cache_create or cache_read:
            lines.append(f"  Cache Creation:  {cache_create:,} tokens")
            lines.append(f"  Cache Read:      {cache_read:,} tokens")
            # Surface the global cache hit ratio so an operator can see at
            # a glance whether caching is actually paying off without
            # eyeballing the per-phase rollup below.
            cache_total = cache_create + cache_read
            if cache_total:
                hit_ratio = cache_read / cache_total
                lines.append(f"  Cache Hit Ratio: {hit_ratio:.1%}")
        if s["severity_counts"]:
            lines.append(f"  Findings:        {s['severity_counts']}")
        if s["verification_verdicts"]:
            lines.append(f"  Verdicts:        {s['verification_verdicts']}")
        evidence = s.get("verification_evidence")
        if evidence and any(evidence.values()):
            lines.append(
                "  Evidence:        "
                f"grounded={evidence['grounded']}, "
                f"ungrounded={evidence['ungrounded']}, "
                f"escalated={evidence['escalated']}, "
                f"cache_hits={evidence['cache_hits']}, "
                f"local_skips={evidence['local_skips']}, "
                f"search_errors={evidence['search_errors']}"
            )
        modes_breakdown = s.get("verification_modes") or {}
        if modes_breakdown:
            lines.append(f"  Modes:           {modes_breakdown}")
        profiles_breakdown = s.get("verification_profiles") or {}
        if profiles_breakdown:
            lines.append(f"  Profiles:        {profiles_breakdown}")
        # Render the escalation rollup only when at least one escalation
        # was attempted, so runs that did not trigger any escalation stay
        # compact.
        esc_stats = s.get("escalation_stats") or {}
        if esc_stats.get("attempts"):
            change_rate = esc_stats.get("change_rate") or 0.0
            lines.append(
                "  Escalation:      "
                f"attempts={esc_stats['attempts']}, "
                f"changed={esc_stats['changed_verdict']}, "
                f"no_change={esc_stats['no_change']}, "
                f"change_rate={change_rate:.1%}"
            )
            if esc_stats.get("by_reason"):
                lines.append(f"    by_reason:     {esc_stats['by_reason']}")
            if esc_stats.get("by_severity"):
                lines.append(f"    by_severity:   {esc_stats['by_severity']}")
        # Render the retry rollup only when at least one finding consumed
        # a retry or continuation. Keeps the summary tight on runs where
        # everything went through cleanly.
        retry_stats = s.get("retry_stats") or {}
        if retry_stats.get("findings_with_retries"):
            lines.append(
                "  Retry/Continue:  "
                f"findings={retry_stats['findings_with_retries']}, "
                f"attempts={retry_stats['total_retry_attempts']}, "
                f"continuations={retry_stats['total_continuations']}"
            )
            if retry_stats.get("by_failure_class"):
                lines.append(
                    f"    by_class:      {retry_stats['by_failure_class']}"
                )
            if retry_stats.get("by_terminal_reason"):
                lines.append(
                    f"    by_terminal:   {retry_stats['by_terminal_reason']}"
                )
        if s["phase_durations"]:
            lines.append("  Phase Durations:")
            for phase, dur in s["phase_durations"].items():
                lines.append(f"    {phase:20s} {dur:.1f}s")
        # Surface output-size and search-budget usage so operators can see
        # whether dynamic caps and ``max_uses`` defaults match real
        # workloads.
        out_t = s.get("output_telemetry") or {}
        if out_t.get("samples"):
            lines.append(
                "  Output Tokens (samples="
                f"{out_t['samples']}): max={out_t['max_observed']:,}, "
                f"p50={out_t['p50']:,}, p95={out_t['p95']:,}"
            )
            if out_t.get("truncated_calls"):
                lines.append(
                    f"    Truncated calls: {out_t['truncated_calls']}"
                    + (
                        f" — by phase: {out_t['truncated_by_phase']}"
                        if out_t.get("truncated_by_phase")
                        else ""
                    )
                )
        budget = s.get("search_budget") or {}
        if budget.get("samples"):
            ceiling = budget.get("ceiling") or 0
            saturated = budget.get("saturated_calls") or 0
            ceiling_part = f"/{ceiling}" if ceiling else ""
            saturated_part = (
                f", saturated={saturated}" if ceiling and saturated else ""
            )
            lines.append(
                f"  Search Budget (samples={budget['samples']}): "
                f"max={budget['max_observed']}{ceiling_part}, "
                f"p50={budget['p50']}, p95={budget['p95']}, "
                f"total={budget['total']}{saturated_part}"
            )
        # Per-phase telemetry rollup. One line per phase that actually made
        # API calls so an operator can see at a glance which phase dominated
        # the token spend, which got cache hits, and whether retries /
        # continuations piled up. Phases with zero API calls (e.g.,
        # extraction-only events) are intentionally omitted to keep the
        # output compact.
        per_phase = s.get("phase_telemetry") or {}
        if per_phase:
            lines.append("")
            lines.append("  Phase Telemetry:")
            for phase_name, bucket in per_phase.items():
                bits = [f"calls={bucket['calls']}"]
                bits.append(
                    f"in={bucket['input_tokens']:,}/out={bucket['output_tokens']:,}"
                )
                cache_total = (
                    bucket["cache_creation_input_tokens"]
                    + bucket["cache_read_input_tokens"]
                )
                if cache_total:
                    bits.append(
                        f"cache_hit={bucket['cache_hit_ratio']:.0%} "
                        f"(read={bucket['cache_read_input_tokens']:,}, "
                        f"create={bucket['cache_creation_input_tokens']:,})"
                    )
                if bucket["web_search_requests"]:
                    bits.append(f"searches={bucket['web_search_requests']}")
                if bucket["retries"]:
                    bits.append(f"retries={bucket['retries']}")
                if bucket["continuations"]:
                    bits.append(f"continuations={bucket['continuations']}")
                if bucket["truncated_calls"]:
                    bits.append(f"truncated={bucket['truncated_calls']}")
                if bucket["realtime_calls"] or bucket["batch_calls"]:
                    bits.append(
                        f"realtime={bucket['realtime_calls']}/"
                        f"batch={bucket['batch_calls']}"
                    )
                if bucket["models"]:
                    bits.append("models=" + ",".join(bucket["models"]))
                lines.append(f"    {phase_name:20s} {', '.join(bits)}")

        # Surface failed specs, skipped edits, ambiguous locator count, and
        # event truncation so users can see what required attention without
        # scanning the timeline.
        if s.get("failed_specs"):
            lines.append("")
            lines.append(f"  Failed Specs:    {len(s['failed_specs'])}")
            for fname in s["failed_specs"]:
                lines.append(f"                   - {fname}")
        if s.get("skipped_specs"):
            lines.append(f"  Skipped Specs:   {len(s['skipped_specs'])}")
            for fname in s["skipped_specs"]:
                lines.append(f"                   - {fname}")
        if s.get("edit_skip_reasons"):
            lines.append("  Edit Skips:")
            for reason, cnt in s["edit_skip_reasons"].items():
                lines.append(f"    {reason:20s} {cnt}")
        if s.get("ambiguous_locator_count"):
            lines.append(f"  Ambiguous Locators: {s['ambiguous_locator_count']}")
        if (s.get("edits_applied_total") or s.get("edits_skipped_total")
                or s.get("edits_failed_total")):
            lines.append(
                "  Edit Application: "
                f"applied={s['edits_applied_total']}, "
                f"skipped={s['edits_skipped_total']}, "
                f"failed={s['edits_failed_total']}"
            )
        # Locator-method breakdown. Hidden when no edit plan ran.
        locator_methods = s.get("locator_methods") or {}
        if locator_methods:
            lines.append(f"  Locator Methods: {locator_methods}")
        # Phase 1 of the auto-apply quality plan: surface per-fix
        # counters under a dedicated section so users can see which
        # quality guards fired. Hidden entirely when every counter is
        # zero (typical for runs without an edit pass).
        quality_lines = self._auto_apply_quality_lines()
        if quality_lines:
            lines.append("")
            lines.append("AUTO-APPLY QUALITY")
            lines.append("-" * 40)
            lines.extend(quality_lines)
        if s.get("events_dropped"):
            lines.append(
                f"  Events Dropped:  {s['events_dropped']} "
                f"(cap={self.max_events:,}; older events truncated)"
            )
        if s.get("events_truncated_by_size"):
            lines.append(
                f"  Events Truncated: {s['events_truncated_by_size']} "
                f"(per-event cap={self.max_event_data_bytes:,} bytes)"
            )
        if s.get("secrets_redacted"):
            lines.append(
                f"  Secrets Redacted: {s['secrets_redacted']} field(s) replaced with <redacted>"
            )

        # Timeline
        lines.append("EVENT TIMELINE")
        lines.append("-" * 72)
        level_icons = {
            "info": " ",
            "success": "+",
            "warning": "!",
            "error": "X",
            "step": ">",
        }
        for e in self.events:
            icon = level_icons.get(e.level, " ")
            ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S")
            elapsed = f"{e.elapsed:8.2f}s"
            phase_tag = f"[{e.phase}]" if e.phase else ""
            lines.append(f"  {ts} {elapsed} {icon} {phase_tag:20s} {e.message}")
            if e.data:
                for k, v in e.data.items():
                    lines.append(f"{'':42s} {k}: {v}")
        lines.append("")
        lines.append("=" * 72)
        return "\n".join(lines)

