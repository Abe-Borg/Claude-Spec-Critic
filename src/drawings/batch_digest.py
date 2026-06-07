"""Batch-mode per-sheet drawing digest via the Message Batches API.

The real-time path (:func:`src.drawings.digest.digest_sheet`) sends one
synchronous vision request per sheet. This path instead submits all *uncached*
sheets as a single Message Batch — 50% cheaper per Anthropic's batch pricing —
and references each sheet's images by Files-API ``file_id`` (see
:mod:`src.drawings.file_upload`) so no per-item body approaches the 32 MB
Messages-API request limit that broke the inline-base64 path on dense sheets.

Flow::

    render → cache hit?  ── yes → reuse digest (no upload, no batch item)
                         └─ no  → upload images + build a batch request
    submit batch → poll to completion → collect → parse each item → SheetDigest
    → write fresh digests to the cache → delete the uploaded files

Caching is preserved exactly as in the real-time path: an unchanged sheet on a
re-run is served from the digest cache and never enters the batch. Results are
assembled in page order regardless of completion order. Per-sheet failures
(upload error, batch item ``errored``/``expired``) are captured on that sheet's
:class:`SheetDigest` and never abort the rest of the set.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.api_config import REVIEW_MODEL_DEFAULT
from ..core.tokenizer import estimate_image_tokens_total
from ..tracing import capture_hooks as _trace
from .digest import (
    DEFAULT_DIGEST_EFFORT,
    DEFAULT_DIGEST_MAX_TOKENS,
    DIGEST_PROMPT_VERSION,
    SheetDigest,
    _clean_error,
    _get,
    _message_text,
    _message_usage,
    build_digest_request_params,
)
from .digest_cache import digest_cache_key
from .file_upload import FILES_API_BETA, delete_files, upload_sheet_images
from .models import SheetRef

# Bounded polling. Mirrors the review/verification batch policy: bound by total
# elapsed (drawing batches of a handful of sheets typically land in minutes, but
# the Batches API may take up to 24h), with progressive backoff so a long batch
# doesn't hammer the poll endpoint.
DEFAULT_BATCH_MAX_ELAPSED_SECONDS = 4 * 3600
DEFAULT_POLL_INTERVAL_SECONDS = 15
DEFAULT_POLL_MAX_INTERVAL_SECONDS = 120
DEFAULT_POLL_BACKOFF_AFTER_SECONDS = 5 * 60
DEFAULT_MAX_CONSECUTIVE_POLL_ERRORS = 10

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[..., None]


@dataclass
class _Slot:
    """One sheet's place in the page-ordered result, plus how it's being served."""

    index: int
    ref: SheetRef
    image_estimate: int
    # Set immediately for a cache hit or an upload failure (no batch item).
    digest: SheetDigest | None = None
    # Set when the sheet was uploaded and submitted as a batch item.
    custom_id: str | None = None
    cache_key: str | None = None
    file_ids: list[str] = field(default_factory=list)


@dataclass
class DrawingBatch:
    """A submitted (or fully-cached) drawing batch, awaiting collection."""

    batch_id: str | None
    slots: list[_Slot]
    total: int

    @property
    def submitted_slots(self) -> list[_Slot]:
        return [s for s in self.slots if s.custom_id is not None]

    @property
    def all_file_ids(self) -> list[str]:
        return [fid for s in self.slots for fid in s.file_ids]


def _batch_item_error_text(result_obj: Any) -> str:
    """Human-readable error for a non-succeeded batch item (no repr noise)."""
    rtype = _get(result_obj, "type", "errored") or "errored"
    error = _get(result_obj, "error", None)
    if error is not None:
        inner = _get(error, "error", error)
        etype = str(_get(inner, "type", "") or "")
        emsg = str(_get(inner, "message", "") or "").strip()
        if emsg:
            return f"{etype}: {emsg}" if etype else emsg
    return f"batch request {rtype}"


def _normalize_status(status: Any) -> str:
    return str(status or "").replace("-", "_")


def _progressive_interval(elapsed: float) -> int:
    """Snappy while short, stretching toward the cap for a long-running batch."""
    base = DEFAULT_POLL_INTERVAL_SECONDS
    cap = DEFAULT_POLL_MAX_INTERVAL_SECONDS
    threshold = DEFAULT_POLL_BACKOFF_AFTER_SECONDS
    if elapsed <= threshold:
        return base
    progress = min(1.0, (elapsed - threshold) / max(1, threshold))
    return max(base, min(cap, int(base + (cap - base) * progress)))


def submit_drawing_batch(
    rendered_sheets,
    *,
    client: Any,
    model: str = REVIEW_MODEL_DEFAULT,
    max_tokens: int = DEFAULT_DIGEST_MAX_TOKENS,
    use_thinking: bool = True,
    effort: str | None = DEFAULT_DIGEST_EFFORT,
    cache: Any = None,
    progress: ProgressCallback | None = None,
    total: int = 0,
) -> DrawingBatch:
    """Render-stream → cache-or-upload → submit one Message Batch.

    ``rendered_sheets`` is an iterable of :class:`RenderedSheet` (streamed, so at
    most one sheet's images are held at a time). Cache hits are recorded directly
    and skip both the upload and the batch; misses upload their images via the
    Files API and become one batch item. Returns a :class:`DrawingBatch` to hand
    to :func:`collect_drawing_batch`. ``batch_id`` is ``None`` when every sheet
    was cached (or failed to upload) — there is nothing to poll.
    """
    slots: list[_Slot] = []
    reqs: list[dict] = []

    for index, sheet in enumerate(rendered_sheets):
        image_est = estimate_image_tokens_total(sheet.image_sizes, model=model)
        slot = _Slot(index=index, ref=sheet.ref, image_estimate=image_est)

        cache_key: str | None = None
        if cache is not None:
            cache_key = digest_cache_key(
                sheet,
                model=model,
                prompt_version=DIGEST_PROMPT_VERSION,
                max_tokens=max_tokens,
                effort=effort,
                use_thinking=use_thinking,
            )
            hit = cache.get(cache_key)
            if hit is not None:
                slot.digest = SheetDigest(
                    ref=sheet.ref,
                    text=hit.get("text", ""),
                    input_tokens=int(hit.get("input_tokens", 0) or 0),
                    output_tokens=int(hit.get("output_tokens", 0) or 0),
                    image_token_estimate=image_est,
                    stop_reason=hit.get("stop_reason"),
                    error=None,
                    cached=True,
                )
                slots.append(slot)
                if progress is not None:
                    progress(index + 1, total or 0, f"Cached {sheet.ref.display_label}")
                continue

        try:
            upload = upload_sheet_images(client, sheet)
        except Exception as exc:  # noqa: BLE001 - one sheet's upload failing is captured, not fatal
            slot.digest = SheetDigest(
                ref=sheet.ref,
                text="",
                image_token_estimate=image_est,
                error=f"image upload failed: {_clean_error(exc)}",
            )
            slots.append(slot)
            if progress is not None:
                progress(index + 1, total or 0, f"Upload failed: {sheet.ref.display_label}")
            continue

        custom_id = f"sheet__{index}"
        slot.custom_id = custom_id
        slot.cache_key = cache_key
        slot.file_ids = upload.file_ids
        reqs.append(
            {
                "custom_id": custom_id,
                "params": build_digest_request_params(
                    upload.content,
                    model=model,
                    max_tokens=max_tokens,
                    use_thinking=use_thinking,
                    effort=effort,
                ),
            }
        )
        slots.append(slot)
        if progress is not None:
            progress(index + 1, total or 0, f"Uploaded {sheet.ref.display_label}")

    batch_id: str | None = None
    if reqs:
        mb = client.beta.messages.batches.create(requests=reqs, betas=[FILES_API_BETA])
        batch_id = _get(mb, "id")
        _trace.capture_note(
            None, "drawing batch submitted", batch_id=batch_id, request_count=len(reqs)
        )

    return DrawingBatch(batch_id=batch_id, slots=slots, total=total or len(slots))


def _poll_until_terminal(
    client: Any,
    batch_id: str,
    *,
    total: int,
    cached_done: int,
    progress: ProgressCallback | None,
    on_log: LogCallback | None,
    sleep: Callable[[float], None],
    max_elapsed_seconds: int,
) -> str:
    """Poll ``batch_id`` to a terminal state. Returns the status or a sentinel.

    Returns the terminal ``processing_status`` (``ended`` / ``failed`` / …), or
    ``"detached"`` when the elapsed bound is hit (the remote batch keeps running),
    or ``"poll_failed"`` after repeated retrieve errors.
    """
    started = time.monotonic()
    consecutive_errors = 0
    while True:
        elapsed = time.monotonic() - started
        if elapsed > max_elapsed_seconds:
            if on_log is not None:
                on_log(
                    f"Drawing batch still processing after "
                    f"{max_elapsed_seconds / 3600:.1f}h; id={batch_id}",
                    level="warning",
                )
            return "detached"
        try:
            batch = client.messages.batches.retrieve(batch_id)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 - retried; terminal after the cap
            consecutive_errors += 1
            if consecutive_errors >= DEFAULT_MAX_CONSECUTIVE_POLL_ERRORS:
                if on_log is not None:
                    on_log(f"Drawing batch poll failed repeatedly: {exc}", level="error")
                return "poll_failed"
            sleep(min(DEFAULT_POLL_INTERVAL_SECONDS * (2**consecutive_errors), 300))
            continue

        counts = _get(batch, "request_counts")
        done_in_batch = sum(
            int(_get(counts, k, 0) or 0)
            for k in ("succeeded", "errored", "canceled", "expired")
        )
        status = _normalize_status(_get(batch, "processing_status"))
        if progress is not None:
            done = min(total, cached_done + done_in_batch)
            progress(done, total, f"Analyzing {done}/{total} sheet(s) — batch {status}")
        if status in ("ended", "failed", "expired", "canceled"):
            return status
        sleep(_progressive_interval(elapsed))


def _parse_item(slot: _Slot, result: Any, *, cache: Any) -> SheetDigest:
    """Turn one batch result envelope into the sheet's :class:`SheetDigest`."""
    if result is None:
        return SheetDigest(
            ref=slot.ref,
            text="",
            image_token_estimate=slot.image_estimate,
            error="batch returned no result for this sheet",
        )
    rr = _get(result, "result")
    if _get(rr, "type") != "succeeded":
        return SheetDigest(
            ref=slot.ref,
            text="",
            image_token_estimate=slot.image_estimate,
            error=_batch_item_error_text(rr),
        )
    message = _get(rr, "message")
    text = _message_text(message)
    in_tok, out_tok = _message_usage(message)
    stop = _get(message, "stop_reason")
    error = None if text else f"empty digest (stop_reason={stop!r})"
    if cache is not None and slot.cache_key and error is None and text:
        cache.put(
            slot.cache_key,
            {
                "text": text,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "stop_reason": stop,
                "created_ts": time.time(),
            },
        )
    return SheetDigest(
        ref=slot.ref,
        text=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        image_token_estimate=slot.image_estimate,
        stop_reason=stop,
        error=error,
    )


def collect_drawing_batch(
    batch: DrawingBatch,
    *,
    client: Any,
    cache: Any = None,
    progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_elapsed_seconds: int = DEFAULT_BATCH_MAX_ELAPSED_SECONDS,
) -> list[SheetDigest]:
    """Poll the batch to completion and assemble per-sheet digests in page order.

    Cache hits / upload failures are already resolved on their slots. Submitted
    items are polled, collected, parsed, and (on success) written to the cache;
    the uploaded files are then deleted. If the batch can't be collected
    (detached past the elapsed bound, or repeated poll failures) the uploaded
    files are **left in place** (the remote batch may still be running and needs
    them) and each submitted sheet is marked with a clear, retriable error.
    """
    # Size by the actual slot count (one slot per rendered sheet, indices
    # 0..n-1 in page order) so a divergent display ``total`` can never
    # mis-size or drop a result.
    results: list[SheetDigest | None] = [None] * len(batch.slots)
    for slot in batch.slots:
        if slot.digest is not None:
            results[slot.index] = slot.digest

    submitted = batch.submitted_slots
    if batch.batch_id and submitted:
        cached_done = sum(1 for s in batch.slots if s.digest is not None)
        status = _poll_until_terminal(
            client,
            batch.batch_id,
            total=batch.total,
            cached_done=cached_done,
            progress=progress,
            on_log=on_log,
            sleep=sleep,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        if status in ("ended", "failed", "expired", "canceled"):
            raw = {}
            for result in client.messages.batches.results(batch.batch_id):
                raw[_get(result, "custom_id")] = result
            for slot in submitted:
                results[slot.index] = _parse_item(
                    slot, raw.get(slot.custom_id), cache=cache
                )
            delete_files(client, batch.all_file_ids)
        else:
            # Not collected — leave the uploaded files for the still-running
            # remote batch; surface a clear, retriable per-sheet error.
            for slot in submitted:
                results[slot.index] = SheetDigest(
                    ref=slot.ref,
                    text="",
                    image_token_estimate=slot.image_estimate,
                    error=(
                        f"drawing batch not collected ({status}); "
                        f"remote batch id={batch.batch_id} may still be running"
                    ),
                )

    ref_by_index = {s.index: s.ref for s in batch.slots}
    for i, digest in enumerate(results):
        if digest is None:  # defensive — every slot resolves above
            results[i] = SheetDigest(
                ref=ref_by_index.get(i, batch.slots[0].ref if batch.slots else None),
                text="",
                error="sheet produced no digest result",
            )
    return [r for r in results if r is not None]
