"""Core orchestration pipeline for Spec Critic."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..input.extractor import ExtractedSpec, SUPPORTED_EXTENSIONS
from ..input.extraction_cache import (
    cache_token_count,
    extract_multiple_specs_cached,
    extraction_cache_stats,
    get_cached_token_count,
)
from ..input.preprocessor import preprocess_spec, detect_inconsistent_file_naming
from ..core.tokenizer import (
    RECOMMENDED_MAX,
    count_tokens_via_api,
    exceeds_per_call_limit_for_model,
    local_estimate_safety_factor,
    safe_local_estimate,
)
from ..review.reviewer import ReviewResult, Finding
from ..review.review_request_builder import (
    ReviewRequestSpec,
    build_token_count_request,
    estimate_local_request_tokens,
    review_request_cache_key,
)
from ..batch.batch import BatchJob, submit_review_batch, retrieve_review_results
from ..batch.batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from ..core.api_config import REVIEW_MODEL_DEFAULT, token_count_preflight_enabled
from ..verification.verifier import (
    start_verification_batch,
    collect_verification_batch_results,
    prepare_findings_for_verification,
)
from ..verification.verification_cache import VerificationCache, cache_persist_enabled
from ..cross_check.cross_checker import run_chunked_cross_check
from ..core.code_cycles import CodeCycle, DEFAULT_CYCLE
from ..core.project_profile import ProjectProfile
from ..modules import DEFAULT_MODULE, ReviewModule, get_module
from ..tracing import capture_hooks as _trace

# Log/progress callbacks accept explicit ``level`` and ``phase`` keywords
# so pipeline code can categorize messages (info / success / warning /
# error / step / muted) and route them to the right diagnostics bucket
# without the GUI keyword-sniffing the message text. Single-arg callers
# still work — kwargs default cleanly.
LogFn = Callable[..., None]
ProgressFn = Callable[..., None]


def _noop_log(_msg: str, **_kwargs: object) -> None: return


def _noop_progress(_: float, __: str, **_kwargs: object) -> None: return


def _make_verification_cache(*, log: LogFn = _noop_log) -> VerificationCache:
    """Construct a verification cache, prepopulated from disk when enabled.

    Persistence is opt-out (``SPEC_CRITIC_VERIFICATION_CACHE_PERSIST=0``
    disables it). Load failures (missing file on first run, corrupt JSON,
    schema mismatch) silently fall back to an empty cache so a fresh user
    is never blocked by cache state.
    """
    cache = VerificationCache()
    if not cache_persist_enabled():
        return cache
    try:
        loaded = cache.load_from_disk()
    except Exception as exc:  # pragma: no cover - defensive
        log(f"Verification cache: load failed ({exc}); starting fresh.", level="warning")
        return cache
    if loaded:
        stats = cache.stats()
        expired_part = (
            f", {stats['expired_on_load']} expired"
            if stats.get("expired_on_load")
            else ""
        )
        log(
            f"Verification cache: loaded {loaded} entry(ies) from disk"
            f"{expired_part}.",
            level="info",
        )
    return cache


def _persist_verification_cache(cache: VerificationCache, *, log: LogFn = _noop_log) -> None:
    """Save the in-memory cache to disk if persistence is enabled.

    Failures are logged but never raised — a save failure should not abort
    a run that has already produced findings.
    """
    if not cache_persist_enabled():
        return
    try:
        size = cache.save_to_disk()
    except Exception as exc:  # pragma: no cover - defensive
        log(f"Verification cache: save failed ({exc}).", level="warning")
        return
    if size:
        log(f"Verification cache: saved {size} entry(ies) to disk.", level="info")


@dataclass
class PipelineResult:
    review_result: Optional[ReviewResult]
    files_reviewed: list[str] = field(default_factory=list)
    # Subset of ``files_reviewed`` whose individual review failed
    # (truncated / parse-error / errored / no result) and therefore
    # produced zero findings. Sourced from
    # ``CollectedBatchState.truncated_specs`` in ``finalize_batch_result``.
    # Carried onto the result so the exported report can distinguish a
    # spec that *failed* review (0 findings because it never completed)
    # from a genuinely-clean spec (0 findings because it has no issues) —
    # the two are otherwise indistinguishable in the final artifact, which
    # is the single honesty gap this field closes. Empty on a clean run.
    failed_review_specs: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cycle_label: str = DEFAULT_CYCLE.label
    # Identity of the module the run was reviewed under. Rides alongside
    # ``cycle_label`` (which stays for the verification-cache namespace and
    # legacy display) so report / sidecar surfaces can stamp provenance.
    module_id: str = DEFAULT_MODULE.module_id
    # Per-run project identity (city/state/country/client) as a serialized
    # dict, or ``None`` for a profile-less run. Additive + ``getattr``-read
    # downstream (report title lines, sidecar), mirroring ``module_id``; the
    # dict form keeps the result JSON-friendly. ``None`` on every existing
    # (profile-less) run, so those reports are byte-identical.
    project_profile: dict | None = None
    # Serialized ``RequirementsProfile`` from the WS-3 research phase, or
    # ``None`` when it didn't run. Carried so the WS-4 report section /
    # compliance pass / profile.json export can read it via ``getattr``;
    # additive like ``project_profile``.
    requirements_profile: dict | None = None
    total_elapsed_seconds: float | None = None
    # Remaining deterministic alert types collected during preflight.
    # Carrying them through here lets the report render every detector's
    # output, not just LEED / placeholder.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)
    # The extracted specs themselves so the
    # report's Run Diagnostics banner can count specs whose extraction
    # surfaced warnings (drawing-heavy documents, embedded objects).
    # The banner reads ``ExtractedSpec.extraction_warnings`` per spec
    # and reports the number of affected specs. Carrying the list here
    # (rather than just a count) keeps the door open for the report to
    # render the warning text inline in a future enhancement without
    # threading new fields through.
    extracted_specs: list[ExtractedSpec] = field(default_factory=list)


@dataclass
class CollectedBatchState:
    submission: BatchSubmission
    review_result: ReviewResult
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cross_check_skipped_due_to_missing_specs: bool = False
    truncated_specs: list[str] = field(default_factory=list)
    # Propagate the rest of the deterministic alerts through the
    # collect / finalize handoff so the resulting PipelineResult carries
    # them.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)
    # Tracing: the pipeline span_id carries the batch-mode root span across
    # the separate function calls (submit, poll, collect, cross-check,
    # verify, finalize). Default empty string when tracing was disabled at
    # submit time.
    trace_span_id: str = ""


def _get_spec_files(input_dir: Path) -> list[Path]:
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(input_dir.glob(f"*{ext}"))
    return sorted([p for p in files if not p.name.startswith("~$")], key=lambda p: p.name.lower())


# ---------------------------------------------------------------------------
# Formal grouping vs occurrence types.
#
# Separates the *display* concept ("the same issue appears in N files")
# from the *executable edit* concept ("apply this change to file X at
# location Y"). The behavioral split is enforced through
# ``Finding.affected_files``; the explicit types below make it harder to
# lose per-file occurrences when new code paths get added (e.g., report
# exporter, edit dialog, comments mode). These dataclasses are produced by
# ``group_findings()`` and consumed by code that needs the formal split.
# The list-of-Finding API is preserved so existing callers do not change.
# ---------------------------------------------------------------------------


@dataclass
class FindingOccurrence:
    """One executable edit candidate: a finding bound to a single file.

    ``finding`` is the display-level (representative) finding.
    ``original_finding`` is the per-file pre-merge member finding when
    available — that is the source of truth for executable edit fields
    (``existingText``, ``replacementText``, ``anchorText``,
    ``evidenceElementId``, ``edit_proposal``). ``original_finding`` is
    ``None`` when the merged representative did not record a per-file
    original for this file (singleton finding where the representative
    *is* the only original, or pre-tracking resume payload); callers that
    need executable edit fields should resolve via
    :meth:`executable_finding`.
    """
    occurrence_id: str
    file_name: str
    finding: Finding
    original_finding: Finding | None = None

    def executable_finding(self) -> Finding:
        """Return the per-file original when available, else the representative.

        Edit execution should prefer the original member finding for each
        affected file so a representative's edit text is never applied to
        a file whose original text differed. Falls back to the
        representative when no per-file original was recorded — a
        downstream applier that treats that fallback as unsafe should
        check :meth:`has_original` first.
        """
        return self.original_finding if self.original_finding is not None else self.finding

    def has_original(self) -> bool:
        """True iff a per-file pre-merge original was recorded for this file."""
        return self.original_finding is not None


@dataclass
class FindingGroup:
    """A display-level group of findings that share dedup identity.

    The ``representative`` finding is the highest-severity / highest-
    confidence example used to render the report. ``occurrences`` lists the
    per-file edit candidates so callers fanning out edits do not have to
    re-derive them from ``affected_files``.
    """
    group_id: str
    representative: Finding
    occurrences: list[FindingOccurrence] = field(default_factory=list)

    @property
    def file_names(self) -> list[str]:
        return [o.file_name for o in self.occurrences]


def _occurrence_id(group_id: str, file_name: str, idx: int) -> str:
    return f"{group_id}::{idx:03d}::{file_name}"


def _originals_by_filename(finding: Finding) -> dict[str, Finding]:
    """Build a per-file lookup of ``Finding.occurrence_originals``.

    The first original per ``fileName`` wins (originals with the same
    fileName share a dedup key by construction, so they have equivalent
    edit text). Empty-fileName originals are skipped — there is no
    executable file to bind them to.
    """
    by_name: dict[str, Finding] = {}
    for orig in finding.occurrence_originals:
        if orig.fileName:
            by_name.setdefault(orig.fileName, orig)
    return by_name


def group_findings(findings: list[Finding]) -> list[FindingGroup]:
    """Convert a deduplicated finding list into formal ``FindingGroup`` rows.

    Each group's ``occurrences`` list expands ``Finding.affected_files`` so a
    multi-file finding produces one ``FindingOccurrence`` per file. Findings
    with no ``affected_files`` and no ``fileName`` produce a single
    placeholder occurrence with an empty file name; downstream code should
    check for that and skip.

    Each ``FindingOccurrence`` is also bound to the per-file pre-merge
    original via ``occurrence_originals`` so edit execution can use the
    per-file ``existingText`` instead of the representative's. When the
    merged finding is a singleton (its ``fileName`` matches the lone
    affected file), the occurrence binds the representative itself as its
    own original — the representative *is* the original in that case.
    """
    groups: list[FindingGroup] = []
    for idx, f in enumerate(findings):
        files = list(dict.fromkeys(f.affected_files)) or (
            [f.fileName] if f.fileName else [""]
        )
        group_id = f"grp-{idx:04d}"
        originals_by_file = _originals_by_filename(f)
        occurrences: list[FindingOccurrence] = []
        for i, name in enumerate(files):
            original = originals_by_file.get(name)
            if original is None and name and not f.occurrence_originals and name == f.fileName:
                # Singleton path: the merged finding has no recorded
                # originals AND this occurrence is for the representative's
                # own file. The representative *is* the original — bind it
                # as such so executable_finding() returns the right thing.
                original = f
            occurrences.append(
                FindingOccurrence(
                    occurrence_id=_occurrence_id(group_id, name, i),
                    file_name=name,
                    finding=f,
                    original_finding=original,
                )
            )
        groups.append(FindingGroup(group_id=group_id, representative=f, occurrences=occurrences))
    return groups


def _normalize_issue_text(text: str) -> str:
    normalized = re.sub(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _normalized_text_digest(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    # Hash the full text so long passages can never collide just because
    # their first 200 characters happen to match. Truncating before hashing
    # silently merged distinct findings.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedup_key(f: Finding) -> tuple:
    return (
        _normalize_issue_text(f.issue),
        (f.section or "").strip().lower(),
        (f.codeReference or "").strip().lower(),
        f.actionType,
        _normalized_text_digest(f.existingText),
        _normalized_text_digest(f.replacementText),
    )


def compute_finding_id(f: Finding, *, prefix: str = "rf") -> str:
    """Compute a stable, deterministic id for a finding.

    Each review finding gets a stable id at dedup time so the report and
    the edit-instruction sidecar can reference it and so the cross-check
    pass can label the prior findings it was shown.

    The id is derived from the same key the dedup helper uses, so two
    findings with the same dedup identity share the same id (and a
    representative carries the id of the group). The hash is truncated to
    12 hex chars — collision risk is negligible at the per-run scale we
    operate at (typically <100 findings) and the short form keeps the id
    readable in transcripts and reports.

    ``prefix`` namespaces the id by finding origin: review findings use
    ``rf-`` (the default), cross-check / coordination findings use ``cf-``
    (see :func:`assign_cross_check_finding_ids`). Because the id is purely
    content-derived, the prefix is what guarantees a review finding and a
    coordination finding that happen to share an identical dedup key never
    collide into one sidecar entry.
    """
    key = _dedup_key(f)
    digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:12]}"


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    if len(findings) <= 1:
        # Singleton lists still need a stable finding_id so the report and
        # edit-instruction sidecar can reference the finding.
        for f in findings:
            if not f.finding_id:
                f.finding_id = compute_finding_id(f)
        return findings
    groups: dict[tuple, list[Finding]] = {}
    for f in findings:
        groups.setdefault(_dedup_key(f), []).append(f)

    rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    out: list[Finding] = []
    for group in groups.values():
        if len(group) == 1:
            f = group[0]
            if not f.affected_files and f.fileName:
                f.affected_files = [f.fileName]
            # Stamp a stable finding id so the report and edit-instruction
            # sidecar can reference it. Computed from the dedup key so the
            # id is deterministic across runs of the same content.
            if not f.finding_id:
                f.finding_id = compute_finding_id(f)
            out.append(f)
            continue
        group.sort(key=lambda f: (rank.get(f.severity, 99), -f.confidence))
        rep = group[0]
        files = list(dict.fromkeys([f.fileName for f in group if f.fileName]))
        # Carry the representative's edit_proposal onto the merged finding
        # so REPORT_ONLY findings stay REPORT_ONLY after dedupe and so the
        # report / edit-instruction sidecar (and any downstream applier) do
        # not see a freshly-constructed Finding that lost its proposal half.
        merged_proposal = rep.as_edit_proposal()
        # Derive the merged finding's id from the representative before
        # issue-text mutation. The dedup key already collapses the whole
        # group to one identity, so every member would hash to the same
        # id; using rep is the cheaper of the two equivalent paths.
        merged_id = rep.finding_id or compute_finding_id(rep)
        # Retain the per-file pre-merge member findings on the merged
        # representative so a downstream applier can use each file's own
        # ``existingText`` / ``replacementText`` / ``anchorText`` /
        # ``evidenceElementId`` / ``edit_proposal`` instead of fanning the
        # representative's text across files that may have differed. The
        # representative itself is included because it IS the original for
        # its own file. Members keep their own ``occurrence_originals``
        # empty (they are themselves singletons), so this terminates after
        # one level — no recursive nesting.
        member_originals = list(group)
        out.append(Finding(
            severity=rep.severity,
            fileName=files[0] if files else rep.fileName,
            section=rep.section,
            issue=f"{rep.issue} (found in {len(files)} specs: {', '.join(files)})",
            actionType=rep.actionType,
            existingText=rep.existingText,
            replacementText=rep.replacementText,
            codeReference=rep.codeReference,
            confidence=max(f.confidence for f in group),
            affected_files=files,
            anchorText=rep.anchorText,
            insertPosition=rep.insertPosition,
            evidenceElementId=rep.evidenceElementId,
            edit_proposal=merged_proposal,
            finding_id=merged_id,
            # Carry the representative's parse-time demotion reason onto
            # the merged finding so dedup cannot rehydrate a demoted edit.
            # If the rep was demoted (proposal cleared, reason stamped),
            # the merged finding inherits both halves of that decision;
            # if the rep was a clean edit, this stays None.
            demotion_reason=rep.demotion_reason,
            occurrence_originals=member_originals,
        ))
    return out


def assign_cross_check_finding_ids(findings: list[Finding]) -> list[Finding]:
    """Stamp a stable, content-derived id on each cross-check finding.

    Cross-check (coordination) findings are produced *after* the review
    dedup pass and never flow through :func:`_deduplicate_findings` — the
    only place review findings are id-stamped. Without this, every
    coordination finding carries ``finding_id=""`` all the way into the
    edit-instruction sidecar, so a downstream applier that keys, dedupes,
    or cross-references edits by ``finding_id`` would see every
    coordination edit collide on the empty key, and the cross-check
    verification spans would correlate as ``unknown`` in the trace viewer.

    The id reuses :func:`compute_finding_id` (the same content hash review
    ids derive from) with a ``cf-`` prefix so it (a) stays stable across
    runs of identical content, (b) never collides with a review finding's
    ``rf-`` id even when the two share a dedup key, and (c) gives the
    cross-check verification pass and the trace viewer a real per-finding
    handle. Two coordination findings with the same content key
    intentionally share an id — that is the dedup signal a downstream
    applier keys on, mirroring how review ids behave post-dedup.

    Mutates in place (only filling empty ids) and returns the same list so
    callers can chain. Idempotent: a finding that already carries an id is
    left untouched.
    """
    for f in findings:
        if not f.finding_id:
            f.finding_id = compute_finding_id(f, prefix="cf")
    return findings


@dataclass
class _PreparedSpecs:
    """Preflight alerts ride alongside the leed/placeholder alerts.

    The pipeline forwards every one of them through the submission /
    collected-state / pipeline-result chain so the report can render them.

    - ``template_marker_alerts`` — TODO/FIXME/XXX/???
    - ``invalid_code_cycle_alerts`` — year/code citations whose year is
      not a real California cycle
    - ``duplicate_paragraph_alerts`` — verbatim long-paragraph duplicates
    - ``code_cycle_alerts`` — references to a stale cycle for the
      selected :class:`CodeCycle`
    - ``structural_alerts`` — empty sections and duplicate headings
    - ``naming_alerts`` — project-level CSI naming consistency
    """
    specs: list[ExtractedSpec]
    leed_alerts: list[dict]
    placeholder_alerts: list[dict]
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)
    # Per-spec view of every deterministic alert that fired for that
    # filename. The reviewer / batch paths use this to populate the
    # ``<pre_detected>`` block in each per-spec user message so the model
    # is told what was already detected locally and does not duplicate it.
    # Naming-style alerts attach to the file they describe; the project-
    # wide ``inconsistent_filename`` rule is still surfaced because each
    # alert is tagged with the offending filename.
    pre_detected_by_filename: dict[str, list[dict]] = field(default_factory=dict)


# How many specs we exact-count before falling back to a top-K selection.
# The Anthropic ``count_tokens`` endpoint is a real API call — every spec
# we count adds latency to preflight and consumes a token-counting
# request. For typical project sizes (≤ this many specs) we count every
# one so no spec slips past. Above the threshold we exact-count the top K
# candidates ranked by the FULL local request shape, not the raw spec body.
_PREFLIGHT_EXACT_COUNT_ALL_THRESHOLD = 8
_PREFLIGHT_EXACT_COUNT_TOP_K = 4


def _run_exact_token_preflight(
    request_specs: list[ReviewRequestSpec],
    *,
    model: str,
    log: LogFn,
) -> None:
    """Validate each request fits under :data:`RECOMMENDED_MAX` with exact counts.

    Counts the *same* request shape that the batch path will submit
    (system prompt + user message including the ``<pre_detected>`` block +
    tool schema + cache controls). The cache is keyed on a hash of the
    full request shape so a cached count is only reused when those inputs
    are unchanged — adding or removing a ``pre_detected`` alert
    deterministically invalidates the entry.

    For small batches (``≤ _PREFLIGHT_EXACT_COUNT_ALL_THRESHOLD``) every
    spec is exact-counted. Above the threshold the top-K ranked by full
    local estimate are counted; the local-only gate in
    :func:`_prepare_specs` still applies the model-aware safety factor to
    the rest so an undercount cannot mask an overage.

    Raises ``ValueError`` when any exact count exceeds the recommended
    maximum. ``count_tokens_via_api`` returning ``None`` (preflight
    disabled, missing key, SDK mismatch) is treated as "preflight
    unavailable" — the local gate is the fallback authority.
    """
    if not request_specs:
        return

    if len(request_specs) <= _PREFLIGHT_EXACT_COUNT_ALL_THRESHOLD:
        candidates = list(request_specs)
    else:
        # Rank by the FULL local request shape (system + user_message
        # including pre_detected alerts). Reordering files cannot cause a
        # smaller raw spec to bypass exact-count when its alert block
        # makes the real request larger.
        scored = sorted(
            ((estimate_local_request_tokens(rs), idx, rs) for idx, rs in enumerate(request_specs)),
            key=lambda triple: triple[0],
            reverse=True,
        )
        candidates = [rs for _, _, rs in scored[:_PREFLIGHT_EXACT_COUNT_TOP_K]]

    for rs in candidates:
        cache_key = review_request_cache_key(rs)
        exact_tokens = get_cached_token_count(cache_key)
        if exact_tokens is None:
            _, count_kwargs = build_token_count_request(rs)
            exact_tokens = count_tokens_via_api(**count_kwargs)
            if exact_tokens is not None:
                cache_token_count(cache_key, exact_tokens)
        if exact_tokens is None:
            # Preflight unavailable for this spec — the local gate will
            # still apply the model-aware safety factor in the caller.
            continue
        local = estimate_local_request_tokens(rs)
        log(
            f"Token preflight ({rs.filename}, model={model}): "
            f"local~{local:,} | exact={exact_tokens:,}",
            level="info",
        )
        if exact_tokens > RECOMMENDED_MAX:
            raise ValueError(
                f"Spec '{rs.filename}' is too large for a single API call: "
                f"exact API token count {exact_tokens:,} exceeds recommended "
                f"maximum {RECOMMENDED_MAX:,} for model {model}."
            )


def _prepare_specs(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE, model: str = REVIEW_MODEL_DEFAULT, preflight: bool = True) -> _PreparedSpecs:
    # ``preflight=False`` skips the token-size gates (exact + local). Used by
    # the resume path: the batch already passed preflight at submit time, so a
    # large spec must not raise here and block recovery of an in-flight batch.
    spec_files = [Path(f) for f in files] if files else _get_spec_files(Path(input_dir))
    if not spec_files:
        raise FileNotFoundError(f"No specification files found in: {input_dir}")

    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []
    code_cycle_alerts: list[dict] = []
    structural_alerts: list[dict] = []
    template_marker_alerts: list[dict] = []
    invalid_code_cycle_alerts: list[dict] = []
    duplicate_paragraph_alerts: list[dict] = []
    # Per-filename view of the per-spec alerts so the reviewer / batch
    # paths can hand each spec only its own alerts when building the
    # ``<pre_detected>`` block.
    pre_detected_by_filename: dict[str, list[dict]] = {}
    progress(0.0, "Extracting text from specifications...")
    # Parallel extraction. Order is preserved by extract_multiple_specs,
    # so deterministic file ordering and per-spec progress reporting
    # remain stable. Per-file errors still propagate to the caller — the
    # pool maintains the original semantics.
    # Extraction is cached by file identity so repeated runs with toggled
    # options skip the DOCX parse; misses fall through to the parallel
    # extractor.
    extracted = extract_multiple_specs_cached(spec_files)
    for i, (p, spec) in enumerate(zip(spec_files, extracted), start=1):
        if spec.word_count == 0 or not spec.content.strip():
            log(f"Skipping {p.name}: no extractable text content", level="warning")
            progress((i / len(spec_files)) * 25.0, f"Loaded {i}/{len(spec_files)}")
            continue
        specs.append(spec)
        pre = preprocess_spec(spec.content, spec.filename, cycle=cycle)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)
        code_cycle_alerts.extend(pre.code_cycle_alerts)
        structural_alerts.extend(pre.structural_alerts)
        template_marker_alerts.extend(pre.template_marker_alerts)
        invalid_code_cycle_alerts.extend(pre.invalid_code_cycle_alerts)
        duplicate_paragraph_alerts.extend(pre.duplicate_paragraph_alerts)
        # Cache this spec's alerts under its filename so the reviewer /
        # batch paths can hand them to the prompt builder. Naming-style
        # alerts are appended below once the project-wide check runs
        # (they aren't part of ``preprocess_spec``).
        pre_detected_by_filename[spec.filename] = [
            *pre.leed_alerts,
            *pre.placeholder_alerts,
            *pre.code_cycle_alerts,
            *pre.structural_alerts,
            *pre.template_marker_alerts,
            *pre.invalid_code_cycle_alerts,
            *pre.duplicate_paragraph_alerts,
        ]
        progress((i / len(spec_files)) * 25.0, f"Loaded {i}/{len(spec_files)}")
    if not specs:
        raise FileNotFoundError("All files failed extraction. No specs to review.")

    # Project-level naming consistency check. Logged so users see it
    # before submission; never raises.
    naming_alerts = detect_inconsistent_file_naming([s.filename for s in specs])
    # Route project-level naming alerts back to the file they describe so
    # the model sees them in its ``<pre_detected>`` block too.
    for alert in naming_alerts:
        fname = alert.get("filename")
        if fname:
            pre_detected_by_filename.setdefault(fname, []).append(alert)
    if code_cycle_alerts:
        log(
            f"Preflight: {len(code_cycle_alerts)} stale code-cycle reference(s) "
            f"detected against the {cycle.label} cycle.",
            level="warning",
        )
    if structural_alerts:
        log(
            f"Preflight: {len(structural_alerts)} structural issue(s) "
            "(empty/duplicate sections) detected.",
            level="warning",
        )
    if naming_alerts:
        log(
            f"Preflight: {len(naming_alerts)} file(s) use a non-dominant CSI "
            "naming style.",
            level="warning",
        )
    if template_marker_alerts:
        log(
            f"Preflight: {len(template_marker_alerts)} unresolved template "
            "marker(s) (TODO/FIXME/XXX/???) detected.",
            level="warning",
        )
    if invalid_code_cycle_alerts:
        log(
            f"Preflight: {len(invalid_code_cycle_alerts)} invalid California "
            "code-cycle citation(s) detected (year is not a real cycle).",
            level="warning",
        )
    if duplicate_paragraph_alerts:
        log(
            f"Preflight: {len(duplicate_paragraph_alerts)} duplicate "
            "paragraph(s) detected (verbatim copy-paste).",
            level="warning",
        )

    # Build a ReviewRequestSpec per ExtractedSpec so the preflight counts
    # the same request shape that the batch path will submit. The builder
    # owns the prompt construction including the ``<pre_detected>`` alert
    # block and the id-tagged paragraph rendering, so a spec with a small
    # body but a large alert block cannot slip past preflight.
    request_specs: list[ReviewRequestSpec] = [
        ReviewRequestSpec(
            spec_content=spec.content,
            filename=spec.filename,
            model=model,
            cycle=cycle,
            project_context=project_context,
            paragraph_map=spec.paragraph_map,
            pre_detected_alerts=pre_detected_by_filename.get(spec.filename),
        )
        for spec in specs
    ]

    # When the Anthropic ``count_tokens`` endpoint returns a number, that
    # is the authoritative gate. The local cl100k_base count is only used
    # as a fast pre-check and as the fallback when the API call is
    # disabled or fails. Candidates are ranked by the FULL local request
    # shape (system + user_message including pre_detected alerts) rather
    # than by raw spec body length, so reordering files cannot cause a
    # smaller raw spec to bypass exact-count when its wrapper / alerts
    # make the real request larger.
    if preflight and token_count_preflight_enabled() and request_specs:
        _run_exact_token_preflight(
            request_specs,
            model=model,
            log=log,
        )

    # Per-spec local gate. Runs whether or not the exact preflight fired;
    # if exact counts are available the candidates are already known safe,
    # but every spec must still pass the local + safety-factor gate. The
    # model-specific safety multiplier prevents a cl100k_base undercount
    # from masking a real overage; the gate uses the *full* request shape
    # (system + materialized user message with pre_detected alerts) so it
    # does not undercount when alerts dominate the request body.
    if preflight:
        safety = local_estimate_safety_factor(model)
        for spec, rs in zip(specs, request_specs):
            total_local = estimate_local_request_tokens(rs)
            # The exceeds-limit helper compares (spec + overhead) against the
            # recommended max with the safety factor. We feed it ``total_local``
            # as the spec component and zero overhead so the existing helper
            # still applies the model-aware safety factor to the full count.
            if exceeds_per_call_limit_for_model(total_local, 0, model=model):
                padded = safe_local_estimate(total_local, model=model)
                raise ValueError(
                    f"Spec '{spec.filename}' is too large for a single API call: "
                    f"~{total_local:,} cl100k tokens (×{safety:.2f} safety factor "
                    f"for {model} → ~{padded:,}) exceeds recommended max "
                    f"{RECOMMENDED_MAX:,}."
                )

    cache_stats = extraction_cache_stats()
    if cache_stats["hits"]:
        log(
            f"Extraction cache: {cache_stats['hits']} hit(s) reused; "
            f"{cache_stats['misses']} miss(es).",
            level="info",
        )

    return _PreparedSpecs(
        specs=specs,
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
        code_cycle_alerts=code_cycle_alerts,
        structural_alerts=structural_alerts,
        naming_alerts=naming_alerts,
        template_marker_alerts=template_marker_alerts,
        invalid_code_cycle_alerts=invalid_code_cycle_alerts,
        duplicate_paragraph_alerts=duplicate_paragraph_alerts,
        pre_detected_by_filename=pre_detected_by_filename,
    )


@dataclass
class BatchSubmission:
    job: BatchJob
    files_reviewed: list[str] = field(default_factory=list)
    review_request_ids: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    model: str = REVIEW_MODEL_DEFAULT
    project_context: str = ""
    prepared_specs: list[ExtractedSpec] | None = None
    cycle_label: str = DEFAULT_CYCLE.label
    # Registry id of the module this batch was submitted under. The stage
    # functions (repair, cross-check, headless collection) re-resolve the
    # module from this id via ``get_module`` — one source of truth, same
    # degrade-to-default posture as the legacy cycle_label lookup. Persisted
    # into the pending-batch resume state by ``batch_resume.PendingBatch``.
    module_id: str = DEFAULT_MODULE.module_id
    # Per-run project identity (city/state/country/client) as a serialized
    # dict, or ``None`` when the selected module doesn't collect a profile.
    # Persisted into the pending-batch resume state and stamped onto the
    # PipelineResult; additive, ``None`` on every profile-less run.
    project_profile: dict | None = None
    # Serialized ``RequirementsProfile`` (the WS-3 research phase's typed
    # output), or ``None`` when the phase didn't run. The rendered TEXT of
    # the profile is already inside ``project_context`` (spliced pre-submit),
    # so resume recovers the reviewers' view for free; this dict is what the
    # compliance pass and report surfaces (WS-4) reconstruct the structured
    # items from. Additive — same precedent as ``project_profile``.
    requirements_profile: dict | None = None
    cross_check_enabled: bool = False
    # Carry the remaining deterministic alert lists so the collect /
    # finalize path can hand them off to the final PipelineResult.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)
    # Tracing: pipeline span_id carried from start_batch_review through to
    # finalize_batch_result so the batch-mode root span can be closed at
    # the end of the run. Empty string when tracing was disabled.
    trace_span_id: str = ""


def _research_phase_applies(module: ReviewModule, profile: ProjectProfile | None, *, log: LogFn = _noop_log) -> bool:
    """Gate for the WS-3 requirements-research phase.

    Every condition must hold: the module opted in
    (``project_profile_enabled``), a complete profile was collected, and the
    module actually defines research dimensions. With any of them false the
    submit path is byte-identical to a profile-less run (invariant 2). An
    enabled module with a present-but-incomplete profile is logged rather
    than silently skipped — the GUI blocks that combination at validation,
    so hitting it means a headless caller passed bad input.
    """
    if not getattr(module, "project_profile_enabled", False) or profile is None:
        return False
    if not profile.is_complete():
        log(
            "Project profile is incomplete (city/state/country/client); "
            "skipping location research.",
            level="warning",
        )
        return False
    return bool(getattr(module, "research_dimensions", ()))


def _run_research_phase(
    *,
    module: ReviewModule,
    profile: ProjectProfile,
    input_dir: Path,
    files: Optional[list[Path]],
    user_context: str,
    log: LogFn,
    progress: ProgressFn,
    diagnostics=None,
) -> tuple[str, dict]:
    """Corpus scrape → research fan-out → context splice (WS-3, D-3).

    Returns ``(effective_context, requirements_profile_dict)``. The
    extraction gate below is authoritative, the scrape itself best-effort;
    the fan-out raises :class:`~src.research.ResearchFanoutError` when EVERY
    dimension fails, aborting the submit before anything is billed for
    review.

    Deferred import: the research package pulls in the verifier's streaming
    stack, which profile-less runs (the CA module, i.e. every run today)
    never need.
    """
    from ..research import (
        run_requirements_research,
        scrape_corpus_signals,
        splice_profile_into_context,
    )

    progress(0.0, "Researching location requirements...")
    # Extraction gate — MUST hold before the API-backed fan-out spends
    # anything. A missing/corrupt/empty spec set would kill the run at
    # ``_prepare_specs`` anyway; discovering that only AFTER research would
    # burn the whole research budget with no review submitted. The gates
    # mirror ``_prepare_specs``' own failure modes (same error messages) and
    # the extraction is LRU-cached, so the later ``_prepare_specs`` call
    # re-uses this work rather than repeating it.
    spec_files = [Path(f) for f in files] if files else _get_spec_files(Path(input_dir))
    if not spec_files:
        raise FileNotFoundError(f"No specification files found in: {input_dir}")
    # Per-file extraction errors (corrupt DOCX) propagate and abort here.
    extracted = extract_multiple_specs_cached(spec_files)
    if not any(spec.word_count > 0 and spec.content.strip() for spec in extracted):
        raise FileNotFoundError("All files failed extraction. No specs to review.")

    # The scrape over the successfully-extracted text stays best-effort:
    # it is a research seed, and a scrape bug must not sink the run.
    corpus_signals = None
    try:
        corpus_signals = scrape_corpus_signals(extracted, module=module)
    except Exception as exc:  # noqa: BLE001 — scrape is a seed, never a gate
        log(
            f"Corpus-signal scrape skipped ({exc}); research runs profile-only.",
            level="warning",
        )
    research_profile = run_requirements_research(
        module,
        profile,
        corpus_signals=corpus_signals,
        log=log,
        progress=progress,
        diag=diagnostics,
    )
    effective_context, _dropped = splice_profile_into_context(
        user_context, research_profile, log=log
    )
    return effective_context, research_profile.to_dict()


def start_batch_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = REVIEW_MODEL_DEFAULT, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, module: ReviewModule = DEFAULT_MODULE, cross_check_enabled: bool = False, project_profile: ProjectProfile | None = None, diagnostics=None) -> BatchSubmission:
    cycle = module.cycle
    profile_dict = project_profile.to_dict() if project_profile is not None else None
    trace_pipeline = _trace.capture_pipeline_start(
        mode="batch",
        model=model,
        cycle_label=cycle.label,
        module_id=module.module_id,
        files=[str(f) for f in (files or [])],
        project_profile=profile_dict,
    )
    # WS-3 requirements-research phase: runs BEFORE spec preparation so its
    # rendered profile is inside ``project_context`` when preflight counts
    # tokens and the batch submits. Living here (the one engine submission
    # entry point) keeps the GUI submit thread and any headless submitter in
    # lockstep by construction (invariant 7). Resume/recovery paths call
    # ``reconstruct_batch_submission`` instead, so research is never re-run
    # on resume (D-12). ``diagnostics`` is an optional duck-typed
    # ``DiagnosticsReport`` for per-dimension API-call telemetry.
    effective_context = project_context
    requirements_profile_dict: dict | None = None
    if _research_phase_applies(module, project_profile, log=log):
        effective_context, requirements_profile_dict = _run_research_phase(
            module=module,
            profile=project_profile,
            input_dir=input_dir,
            files=files,
            user_context=project_context,
            log=log,
            progress=progress,
            diagnostics=diagnostics,
        )
    prepared = _prepare_specs(input_dir=input_dir, files=files, project_context=effective_context, log=log, progress=progress, cycle=cycle, model=model)
    _trace.capture_note(
        trace_pipeline,
        "specs prepared",
        spec_count=len(prepared.specs),
        leed_alerts=len(prepared.leed_alerts),
        placeholder_alerts=len(prepared.placeholder_alerts),
        cross_check_enabled=cross_check_enabled,
    )
    job = submit_review_batch(
        prepared.specs,
        project_context=effective_context,
        model=model,
        cycle=cycle,
        # Feed each spec's deterministic alerts to the prompt builder so
        # the model is told what local rules already detected and skips
        # duplicating those items as new findings.
        pre_detected_alerts=prepared.pre_detected_by_filename,
    )
    # The "review batch submitted" trace note is emitted inside
    # ``submit_review_batch`` (with the extended-output-beta flag); no
    # second note here so the trace shows the submission once.
    ordered_ids = [cid for cid, _ in sorted(job.request_map.items(), key=lambda item: item[1]["index"])]
    return BatchSubmission(
        job=job,
        files_reviewed=[s.filename for s in prepared.specs],
        review_request_ids=ordered_ids,
        leed_alerts=prepared.leed_alerts,
        placeholder_alerts=prepared.placeholder_alerts,
        model=model,
        # The EFFECTIVE context (research profile spliced in) — this is what
        # the batch was submitted with, what cross-check/verification must
        # see, and what resume recovers the profile text from.
        project_context=effective_context,
        prepared_specs=prepared.specs,
        cycle_label=cycle.label,
        module_id=module.module_id,
        project_profile=profile_dict,
        requirements_profile=requirements_profile_dict,
        cross_check_enabled=cross_check_enabled,
        code_cycle_alerts=prepared.code_cycle_alerts,
        structural_alerts=prepared.structural_alerts,
        naming_alerts=prepared.naming_alerts,
        template_marker_alerts=prepared.template_marker_alerts,
        invalid_code_cycle_alerts=prepared.invalid_code_cycle_alerts,
        duplicate_paragraph_alerts=prepared.duplicate_paragraph_alerts,
        trace_span_id=(trace_pipeline.span_id if trace_pipeline is not None else ""),
    )


def _is_retryable_batch_review_result(rr: ReviewResult | None) -> bool:
    if rr is None:
        return True
    if rr.parse_status in ("parse_error", "incomplete"):
        return True
    if not rr.error:
        return False
    lowered = rr.error.lower()
    return any(token in lowered for token in ("batch request errored", "batch request expired", "batch request canceled"))


def _recover_retryable_review_batch_results(
    submission: BatchSubmission,
    results_by_request: dict[str, ReviewResult],
    *,
    log: LogFn = _noop_log,
) -> dict[str, ReviewResult]:
    retryable_request_ids = [rid for rid in submission.review_request_ids if _is_retryable_batch_review_result(results_by_request.get(rid))]
    if not retryable_request_ids:
        return results_by_request
    if not submission.prepared_specs:
        log("Batch review fallback skipped: original extracted specs are unavailable.", level="warning")
        return results_by_request

    # Resolve the module (and thus the cycle) from the submission's persisted
    # identity — the same degrade-to-default posture as the legacy
    # ``AVAILABLE_CYCLES`` lookup this replaced. ``getattr`` keeps
    # hand-built test doubles without the field working.
    cycle = get_module(getattr(submission, "module_id", None)).cycle
    # Resolve each retryable request's spec by FILENAME first. The positional
    # index is only reliable in the normal submit flow, where request_map is
    # built from the same prepared_specs list (index ⇄ position). On the resume
    # path prepared_specs is RE-extracted and can diverge from the persisted
    # indices — directory-sort order, or a spec that now extracts to empty text
    # being dropped shifts later positions — so a positional lookup would repair
    # (and mis-attribute) the wrong spec. Filename keying is order-independent;
    # the index stays as a fallback for any legacy entry without a filename.
    by_filename = {s.filename: s for s in submission.prepared_specs}
    repair_specs: list[ExtractedSpec] = []
    repair_id_map: dict[str, str] = {}
    for rid in retryable_request_ids:
        meta = submission.job.request_map.get(rid) or {}
        filename = meta.get("filename")
        spec = by_filename.get(filename) if isinstance(filename, str) else None
        if spec is None:
            spec_index = meta.get("index")
            if isinstance(spec_index, int) and 0 <= spec_index < len(submission.prepared_specs):
                spec = submission.prepared_specs[spec_index]
        if spec is None:
            log(f"Review repair skipped for {rid}: original spec is unavailable.", level="warning")
            continue
        repair_specs.append(spec)
        repair_id_map[spec.filename] = rid

    if not repair_specs:
        log("No specs eligible for review repair batch.", level="warning")
        return results_by_request

    log(f"Submitting review repair batch for {len(repair_specs)} failed item(s)...", level="step")
    # The repair batch reuses the same prompt builder, so it should also
    # tell the model what was already detected locally. Alerts are
    # deterministic given (content, filename, cycle), so we recompute
    # them here rather than threading the original map through resume
    # state.
    repair_pre_detected: dict[str, list[dict]] = {}
    for spec in repair_specs:
        pre = preprocess_spec(spec.content, spec.filename, cycle=cycle)
        repair_pre_detected[spec.filename] = [
            *pre.leed_alerts,
            *pre.placeholder_alerts,
            *pre.code_cycle_alerts,
            *pre.structural_alerts,
            *pre.template_marker_alerts,
            *pre.invalid_code_cycle_alerts,
            *pre.duplicate_paragraph_alerts,
        ]
    repair_job = submit_review_batch(
        repair_specs,
        project_context=submission.project_context,
        model=submission.model,
        cycle=cycle,
        retry_instruction=(
            "This is a retry of a previously truncated review. Submit findings via the "
            "submit_review_findings tool with analysis_summary set to an empty string. "
            "Spend the entire output budget on the findings array."
        ),
        pre_detected_alerts=repair_pre_detected,
    )
    outcome = poll_batch_bounded(
        repair_job.batch_id,
        policy=DEFAULT_REVIEW_POLL_POLICY,
        log=log,
        progress_cb=lambda _status: None,
    )
    if outcome.detached or outcome.poll_failed:
        log(
            f"Review repair batch did not complete. {len(retryable_request_ids)} item(s) "
            "will appear as failed in the report.",
            level="warning",
        )
        return results_by_request

    repair_results = retrieve_review_results(repair_job, model=submission.model)
    recovered = 0
    for repair_custom_id, repair_rr in repair_results.items():
        repair_meta = repair_job.request_map.get(repair_custom_id) or {}
        original_rid = repair_id_map.get(repair_meta.get("filename", ""))
        if original_rid and repair_rr and not repair_rr.error:
            results_by_request[original_rid] = repair_rr
            recovered += 1
    repair_level = "success" if recovered == len(repair_specs) else "warning"
    log(f"Review repair batch recovered {recovered}/{len(repair_specs)} item(s).", level=repair_level)
    return results_by_request


def _log_cross_check_status(log: LogFn, cross: ReviewResult):
    if cross.cross_check_status == "completed":
        if cross.findings:
            log(f"Cross-check found {len(cross.findings)} coordination issues", level="info")
        else:
            log("Cross-check completed — no coordination issues found", level="success")
    elif cross.cross_check_status == "skipped":
        log(f"Cross-check skipped: {cross.thinking}", level="warning")
    elif cross.cross_check_status == "failed":
        log(f"Cross-check failed: {cross.error}", level="error")


def collect_review_batch_results(submission: BatchSubmission, *, log: LogFn = _noop_log) -> CollectedBatchState:
    results_by_request = retrieve_review_results(submission.job, model=submission.model)
    results_by_request = _recover_retryable_review_batch_results(submission, results_by_request, log=log)
    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    errors: list[str] = []
    truncated_specs: list[str] = []
    in_tok = out_tok = 0

    for rid in submission.review_request_ids:
        meta = submission.job.request_map.get(rid)
        filename = meta["filename"] if meta else rid
        rr = results_by_request.get(rid)
        if rr is None:
            errors.append(f"{filename}: No result returned from batch")
            truncated_specs.append(filename)
            continue
        if rr.parse_status == "incomplete":
            errors.append(
                f"{filename}: Review response truncated — output exceeded token limit. "
                "No findings extracted. Re-run this spec individually."
            )
            truncated_specs.append(filename)
            continue
        if rr.parse_status == "parse_error":
            errors.append(
                f"{filename}: Could not parse review output. "
                "No findings extracted. Re-run this spec individually."
            )
            truncated_specs.append(filename)
            continue
        if rr.error:
            errors.append(f"{filename}: {rr.error}")
            # Errored/expired/canceled batch requests are surfaced as
            # truncated so the GUI flags the spec and downstream filters
            # exclude it. Letting them fall through silently as "no
            # findings" would let cross-check run as if the spec had been
            # reviewed cleanly.
            truncated_specs.append(filename)
            continue
        all_findings.extend(rr.findings)
        if rr.thinking:
            all_thinking.append(f"--- {filename} ---\n{rr.thinking}")
        in_tok += rr.input_tokens
        out_tok += rr.output_tokens

    all_findings = _deduplicate_findings(all_findings)
    combined = ReviewResult(findings=all_findings, thinking="\n\n".join(all_thinking), model=submission.model, input_tokens=in_tok, output_tokens=out_tok, elapsed_seconds=time.time() - submission.job.created_at)
    if errors:
        combined.thinking += "\n\n--- Batch Errors ---\n" + "\n".join(f"  - {e}" for e in errors)
        # --- FIX 2a: Surface per-spec errors on combined result ---
        combined.error = f"{len(errors)} spec(s) had errors: " + "; ".join(errors)

    return CollectedBatchState(
        submission=submission,
        review_result=combined,
        files_reviewed=submission.files_reviewed,
        leed_alerts=submission.leed_alerts,
        placeholder_alerts=submission.placeholder_alerts,
        truncated_specs=truncated_specs,
        # Forward every alert list the submission carries so
        # finalize_batch_result can ship them on the PipelineResult.
        code_cycle_alerts=list(submission.code_cycle_alerts),
        structural_alerts=list(submission.structural_alerts),
        naming_alerts=list(submission.naming_alerts),
        template_marker_alerts=list(submission.template_marker_alerts),
        invalid_code_cycle_alerts=list(submission.invalid_code_cycle_alerts),
        duplicate_paragraph_alerts=list(submission.duplicate_paragraph_alerts),
        # Carry the pipeline span_id through so finalize_batch_result can
        # close the root span at the end of the batch lifecycle.
        trace_span_id=submission.trace_span_id,
    )


def run_cross_check_for_batch(state: CollectedBatchState, *, specs: list[ExtractedSpec] | None = None, project_context: str | None = None, log: LogFn = _noop_log) -> CollectedBatchState:
    if not state.submission.cross_check_enabled:
        return state
    if specs is None:
        specs = state.submission.prepared_specs
    if project_context is None:
        project_context = state.submission.project_context
    if not specs:
        state.cross_check_skipped_due_to_missing_specs = True
        skipped = ReviewResult(findings=[], cross_check_status="skipped", thinking="Cross-check skipped: original extracted spec content is not available.")
        state.cross_check_result = skipped
        _log_cross_check_status(log, skipped)
        return state
    # Exclude specs whose individual review failed/truncated so cross-check
    # does not make coordination claims based on a spec that was never
    # successfully reviewed.
    failed_filenames = set(state.truncated_specs or [])
    if failed_filenames:
        filtered_specs = [s for s in specs if s.filename not in failed_filenames]
        if len(filtered_specs) != len(specs):
            log(
                "Cross-check excluding "
                f"{len(specs) - len(filtered_specs)} spec(s) that failed review: "
                + ", ".join(sorted(failed_filenames)),
                level="warning",
            )
        specs = filtered_specs
    if not specs:
        state.cross_check_skipped_due_to_missing_specs = True
        skipped = ReviewResult(findings=[], cross_check_status="skipped", thinking="Cross-check skipped: every spec failed review.")
        state.cross_check_result = skipped
        _log_cross_check_status(log, skipped)
        return state
    dedup_findings = [
        f for f in state.review_result.findings
        if not (f.verification and f.verification.verdict == "DISPUTED")
    ]
    disputed_excluded = len(state.review_result.findings) - len(dedup_findings)
    if disputed_excluded:
        log(
            f"Cross-check input: excluding {disputed_excluded} DISPUTED "
            "review finding(s) from the 'already identified' context. "
            "They remain on the final result; only the cross-check input is filtered.",
            level="info",
        )
    # The cycle comes from the submission's module identity — the state
    # object is the single source, so a caller can't pair one module's
    # submission with another module's cycle.
    cycle = get_module(getattr(state.submission, "module_id", None)).cycle
    # Split by CSI division when the combined input would otherwise
    # exceed the cross-check token budget. Without this, a large project
    # would get a ``skipped`` status and no coordination review at all.
    # ``run_chunked_cross_check`` falls back to the single-pass
    # ``run_cross_check`` when the input fits.
    cross = run_chunked_cross_check(specs, dedup_findings, project_context=project_context, cycle=cycle, log=log)
    # Stamp a stable, content-derived id on each coordination finding
    # before it flows into cross-check verification and the edit sidecar.
    # These findings never pass through the review dedup pass, so without
    # this they carry ``finding_id=""`` — colliding on the empty key in the
    # sidecar and correlating as "unknown" in verification traces.
    assign_cross_check_finding_ids(cross.findings)
    state.cross_check_result = cross
    _log_cross_check_status(log, cross)
    return state


def start_batch_verification(
    findings: list[Finding],
    *,
    module: ReviewModule = DEFAULT_MODULE,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    cache: VerificationCache | None = None,
) -> BatchJob | None:
    """Submit a verification batch, applying the local pre-pass first.

    Returns ``None`` if every finding resolved locally (local-skip or cache
    hit) — callers should treat that as "verification complete" without
    polling. Returns the BatchJob otherwise.
    """
    cycle = module.cycle
    remaining = prepare_findings_for_verification(findings, cycle=cycle, cache=cache, log=log)
    if not remaining:
        progress(60.0, "Verification: all findings resolved locally / cached.")
        return None
    progress(60.0, f"Submitting {len(remaining)} verification requests...")
    job = start_verification_batch(remaining, cycle=cycle)
    job.submitted_findings = remaining
    log(f"Verification batch submitted: {job.batch_id}", level="step")
    return job


def collect_batch_verification_results(
    job: BatchJob,
    findings: list[Finding],
    *,
    module: ReviewModule = DEFAULT_MODULE,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    poll_interval: int = 15,
    cache: VerificationCache | None = None,
) -> list[Finding]:
    submitted = job.submitted_findings if job.submitted_findings is not None else findings
    return collect_verification_batch_results(
        job,
        submitted,
        cycle=module.cycle,
        log=log,
        progress=lambda p, m: progress(60.0 + (p / 100.0) * 35.0, m),
        poll_interval=poll_interval,
        cache=cache,
    )


def finalize_batch_result(state: CollectedBatchState) -> PipelineResult:
    # ``prepared_specs`` rides through to PipelineResult.extracted_specs for
    # the report's extraction-warnings banner. The recovery path may null it.
    prepared_specs = state.submission.prepared_specs or []
    all_findings: list[Finding] = []
    if state.review_result and state.review_result.findings:
        all_findings.extend(state.review_result.findings)
    if state.cross_check_result and state.cross_check_result.findings:
        all_findings.extend(state.cross_check_result.findings)
    # Tracing: snapshot every finding's terminal state and close the
    # batch-mode pipeline span.
    for _f in all_findings:
        _trace.capture_finding_terminal(_f)
    if state.trace_span_id:
        _trace.capture_pipeline_end_by_id(
            state.trace_span_id,
            success=True,
            summary={
                "finding_count": len(all_findings),
                "review_finding_count": len(state.review_result.findings) if state.review_result else 0,
                "cross_check_finding_count": (
                    len(state.cross_check_result.findings) if state.cross_check_result else 0
                ),
                "truncated_specs": list(state.truncated_specs),
            },
        )
    return PipelineResult(
        review_result=state.review_result,
        files_reviewed=state.files_reviewed,
        # Specs whose review failed/truncated ride through to the report
        # so the Run Diagnostics banner and the "Files Reviewed" count can
        # flag them as not-actually-reviewed.
        failed_review_specs=list(state.truncated_specs),
        leed_alerts=state.leed_alerts,
        placeholder_alerts=state.placeholder_alerts,
        cross_check_result=state.cross_check_result,
        cycle_label=state.submission.cycle_label,
        module_id=getattr(state.submission, "module_id", "") or DEFAULT_MODULE.module_id,
        project_profile=getattr(state.submission, "project_profile", None),
        requirements_profile=getattr(state.submission, "requirements_profile", None),
        total_elapsed_seconds=time.time() - state.submission.job.created_at,
        # Pass the deterministic-check lists through to the report.
        code_cycle_alerts=list(state.code_cycle_alerts),
        structural_alerts=list(state.structural_alerts),
        naming_alerts=list(state.naming_alerts),
        template_marker_alerts=list(state.template_marker_alerts),
        invalid_code_cycle_alerts=list(state.invalid_code_cycle_alerts),
        duplicate_paragraph_alerts=list(state.duplicate_paragraph_alerts),
        # Ride the extracted specs through to
        # the PipelineResult so the report banner can surface extraction
        # warnings. ``prepared_specs`` may be nulled by the recovery
        # path; the empty-list fallback keeps the banner showing 0
        # extraction warnings instead of crashing.
        extracted_specs=list(prepared_specs),
    )


def reconstruct_batch_submission(
    *,
    batch_id: str,
    request_map: dict,
    review_request_ids: list[str],
    files_reviewed: list[str],
    input_dir: str | None,
    files: list[str] | None,
    model: str,
    project_context: str,
    module: ReviewModule,
    cross_check_enabled: bool,
    created_at: float,
    project_profile: dict | None = None,
    requirements_profile: dict | None = None,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> BatchSubmission:
    """Rebuild a :class:`BatchSubmission` for an already-submitted review batch.

    The counterpart to :func:`start_batch_review` for the resume / recovery
    path: it does NOT submit anything — the batch is already running remotely.
    ``request_map`` / ``review_request_ids`` are restored verbatim from the
    persisted state, so review-result collection
    (:func:`collect_review_batch_results`) is fully decoupled from the local
    files — a detached batch's findings come back even if every source file was
    moved or deleted.

    When the source files are still present they are re-extracted
    (deterministic + content-cached, so this reproduces the bodies the model
    actually reviewed) to repopulate ``prepared_specs`` and the deterministic
    alert lists, which re-enables cross-spec coordination, the review-repair
    fallback, extraction-warning surfacing, and ``<pre_detected>`` rendering.
    Re-extraction runs with ``preflight=False`` (the batch already cleared the
    token gate at submit) and is best-effort: any failure (missing/renamed
    files, parse error) degrades to a findings-only recovery with a warning,
    never an exception.
    """
    job = BatchJob(
        batch_id=batch_id,
        job_type="review",
        request_map=dict(request_map or {}),
        created_at=created_at,
    )
    prepared_specs: list[ExtractedSpec] | None = None
    # Eight DISTINCT empty lists (not a chained alias) so a future ``.append``
    # on one can't corrupt the others; the re-extract success branch reassigns
    # each from ``prepared.*``.
    leed, placeholder, code_cycle, structural, naming, template, invalid, dup = (
        [], [], [], [], [], [], [], [],
    )

    resolved_files = [Path(f) for f in files] if files else None
    if resolved_files and all(p.exists() for p in resolved_files):
        # ``_prepare_specs`` discovers files only when ``files`` is None; with an
        # explicit list it ignores ``input_dir``, so fall back to the files'
        # parent when the original input_dir wasn't captured.
        extract_dir = Path(input_dir) if input_dir else resolved_files[0].parent
        try:
            prepared = _prepare_specs(
                input_dir=extract_dir,
                files=resolved_files,
                project_context=project_context,
                log=log,
                progress=progress,
                cycle=module.cycle,
                model=model,
                preflight=False,
            )
            prepared_specs = prepared.specs
            leed = prepared.leed_alerts
            placeholder = prepared.placeholder_alerts
            code_cycle = prepared.code_cycle_alerts
            structural = prepared.structural_alerts
            naming = prepared.naming_alerts
            template = prepared.template_marker_alerts
            invalid = prepared.invalid_code_cycle_alerts
            dup = prepared.duplicate_paragraph_alerts
            if {s.filename for s in prepared.specs} != set(files_reviewed):
                log(
                    "Resumed spec set differs from the originally reviewed files; "
                    "review findings are unaffected but cross-check may be inconsistent.",
                    level="warning",
                )
        except Exception as exc:  # noqa: BLE001 — recovery must never crash on re-extract
            prepared_specs = None
            log(
                f"Could not re-extract source specs for resume ({exc}); recovering "
                "review findings without cross-check / extraction context.",
                level="warning",
            )
    elif files:
        log(
            "Original spec files were not found; recovering review findings only "
            "(cross-check and extraction context are unavailable).",
            level="warning",
        )

    return BatchSubmission(
        job=job,
        files_reviewed=list(files_reviewed or []),
        review_request_ids=list(review_request_ids or []),
        leed_alerts=leed,
        placeholder_alerts=placeholder,
        model=model,
        project_context=project_context,
        prepared_specs=prepared_specs,
        cycle_label=module.cycle.label,
        module_id=module.module_id,
        project_profile=project_profile,
        # Research is never re-run on resume (D-12): the profile TEXT is
        # already inside the persisted ``project_context``; the structured
        # dict rides back in here from the persisted state (or None for a
        # recovery path that has no saved state).
        requirements_profile=requirements_profile,
        cross_check_enabled=cross_check_enabled,
        code_cycle_alerts=code_cycle,
        structural_alerts=structural,
        naming_alerts=naming,
        template_marker_alerts=template,
        invalid_code_cycle_alerts=invalid,
        duplicate_paragraph_alerts=dup,
        trace_span_id="",
    )


def run_batch_collection_headless(
    submission: BatchSubmission,
    *,
    cache: VerificationCache | None = None,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> PipelineResult:
    """Collect → verify → cross-check → finalize a submitted batch, headlessly.

    The synchronous, UI-free counterpart to
    :func:`src.gui.batch_controller.collect_batch_results`: it runs the exact
    same orchestration sequence (review collection, finding verification,
    cross-spec coordination, cross-check verification, finalize) with plain
    ``log`` / ``progress`` callbacks instead of Tk dispatch and per-finding
    diagnostics. Used by the standalone recovery tool
    (``scripts/recover_batch.py``) and any other non-GUI driver.

    Assumes the review batch has already ended — poll first (e.g. via
    :func:`src.batch.batch_runtime.poll_batch_bounded`) if it may still be
    processing. When ``cache`` is not supplied this owns cache creation and
    persistence; pass one to share a cache across calls.
    """
    owns_cache = cache is None
    if cache is None:
        cache = _make_verification_cache(log=log)
    module = get_module(getattr(submission, "module_id", None))

    review_state = collect_review_batch_results(submission, log=log)

    verifiable = list(review_state.review_result.findings) if review_state.review_result else []
    if verifiable:
        job = start_batch_verification(verifiable, module=module, log=log, progress=progress, cache=cache)
        if job is not None:
            collect_batch_verification_results(job, verifiable, module=module, log=log, progress=progress, cache=cache)

    review_state = run_cross_check_for_batch(
        review_state,
        specs=submission.prepared_specs,
        project_context=submission.project_context,
        log=log,
    )
    cross_findings = (
        list(review_state.cross_check_result.findings)
        if (review_state.cross_check_result and review_state.cross_check_result.findings)
        else []
    )
    if cross_findings:
        cc_job = start_batch_verification(cross_findings, module=module, log=log, progress=progress, cache=cache)
        if cc_job is not None:
            collect_batch_verification_results(cc_job, cross_findings, module=module, log=log, progress=progress, cache=cache)

    if owns_cache:
        _persist_verification_cache(cache, log=log)
    return finalize_batch_result(review_state)

