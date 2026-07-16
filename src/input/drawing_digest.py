"""Construction-drawing digest: a one-time vision pass over drawing PDFs.

The app is otherwise text-only — spec bodies and Project Context attachments
are extracted to plain text before any API call. Construction drawings defeat
that: a scanned set has no text layer at all, and a CAD-exported set yields
jumbled fragments with every graphic invisible. This module sends the drawing
PDFs themselves to a vision-capable model **once**, as native base64
``document`` content blocks (the API rasterizes each page for vision AND
reads its text layer, so vector and scanned sets both work; no beta header),
and returns a structured plain-text digest.

The digest text is merged into Project Context by the caller (GUI attach
flow or a headless script) via the ordinary attachment helpers — so
everything downstream of the textbox (review, cross-check, compliance,
pending-batch resume, prompt caching) keeps seeing plain text, and the
vision cost is paid exactly once rather than on every review call.

Shape mirrors the requirements-research fan-out (`src/research/
requirements_research.py`): one streaming call per chunk run in parallel,
retries via ``DEFAULT_REALTIME_RETRY_POLICY`` with billed-usage
carry-forward, workers that never raise, and a partial-failure policy
(>=1 chunk succeeded -> partial digest with the failures inlined; ALL
chunks failed -> :exc:`DrawingDigestError`). Unlike research the digest
sends **no tools** — the task is transcription of provided documents, so
there is no web budget and no ``pause_turn`` continuation loop (an
unexpected ``pause_turn`` is treated as that chunk's failure).

Chunking: the API allows 600 PDF pages / 32 MB per request, but the real
binding limit is the context window — at the documented ~1,500-3,000
tokens/page, 600 pages would exceed even a 1M-token window. Chunks are
packed greedily (whole files first, an oversized file split by page ranges
with pypdf) under three caps: the API page ceiling, a raw-byte ceiling that
keeps the base64 payload under the request cap, and a page cap derived from
the model's context window.
"""
from __future__ import annotations

import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..core.api_config import (
    DRAWING_DIGEST_MODEL_DEFAULT,
    PHASE_DRAWING_DIGEST,
    apply_effort_config,
    apply_thinking_config,
    drawing_digest_max_tokens,
    extract_cache_usage,
    model_capabilities,
    system_prompt_with_cache,
)
from ..core.pricing import estimate_request_cost, friendly_model_name
from ..core.tokenizer import count_tokens, count_tokens_via_api
from ..gui.context_attachment import wrap_attachment
from ..verification.retry_policy import (
    DEFAULT_REALTIME_RETRY_POLICY,
    classify_exception,
    compute_backoff_seconds,
    is_retryable_failure_class,
)

LogFn = Callable[..., None]
ProgressFn = Callable[..., None]


def _noop_log(_msg: str, **_kwargs: object) -> None:
    return


def _noop_progress(_pct: float, _msg: str, **_kwargs: object) -> None:
    return


class DrawingDigestError(RuntimeError):
    """No usable digest could be produced (no valid files / every chunk failed)."""


# Attachment label for the merged digest block inside Project Context. Part
# of the stable delimiter shape — treat like a schema string.
DIGEST_ATTACHMENT_LABEL = "Construction Drawing Digest"

# The Messages API accepts at most 600 PDF pages per request (total across
# every document block). Mirrors ``resend_sanitizer.MAX_RESEND_PDF_PAGES`` —
# same API limit, opposite direction (that module elides *fetched* PDFs on
# continuation resends; this one packs *inbound* drawing sets). Not imported
# from there because that constant's name and docs are resend-specific.
API_MAX_PDF_PAGES_PER_REQUEST = 600

# Raw (pre-base64) byte budget per request. 20 MiB raw inflates to ~26.7 MB
# base64, leaving >5 MB headroom under the API's 32 MB request cap for the
# prompt text and envelope.
MAX_RAW_PDF_BYTES_PER_REQUEST = 20 * 1024 * 1024

# Conservative top of the documented ~1,500-3,000 tokens/page band (each PDF
# page bills as extracted text + a rasterized image). Used for chunk packing
# and for the local cost estimate when the exact count_tokens preflight is
# unavailable.
DIGEST_TOKENS_PER_PAGE_ESTIMATE = 3_000

# Headroom reserved for the system prompt + user text + envelope when
# deriving the per-chunk page cap from the model's context window.
_DIGEST_PROMPT_OVERHEAD_TOKENS = 5_000

# Fan-out width — research precedent (long-lived streaming calls; four in
# flight stays inside per-account concurrency limits).
_DIGEST_MAX_WORKERS = 4

_TRUNCATION_WARNING = (
    "[WARNING: digest output hit the token cap; content below may be "
    "incomplete]"
)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawingFile:
    """One validated drawing PDF, loaded into memory."""

    path: Path
    name: str
    data: bytes
    page_count: int | None  # None: pypdf could not count (still sendable)


def _count_pdf_pages(data: bytes) -> int | None:
    """Page count of a raw PDF, or ``None`` when pypdf can't parse it.

    Adaptation of ``resend_sanitizer._pdf_page_count`` for raw bytes. Never
    raises — an uncountable file is still sendable (the API does its own
    validation); it just can't be split or packed against the page caps.
    """
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None


def validate_drawing_files(
    paths: Sequence[Path],
) -> tuple[list[DrawingFile], list[str]]:
    """Validate drawing PDFs; return ``(valid_files, per_file_errors)``.

    Checks mirror ``extractor._extract_pdf_text`` (parseable, not
    encrypted) with the same ``"{name}: {reason}"`` error style the context
    attach flow uses, so the GUI can surface both the same way.
    """
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - pypdf is a pinned dep
        raise DrawingDigestError(
            "PDF support requires the 'pypdf' package. Install with: pip install pypdf"
        ) from exc

    files: list[DrawingFile] = []
    errors: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        name = path.name
        if not path.exists():
            errors.append(f"{name}: file not found")
            continue
        if path.suffix.lower() != ".pdf":
            errors.append(f"{name}: not a PDF (drawing sets must be .pdf)")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            errors.append(f"{name}: could not read file — {exc}")
            continue
        try:
            reader = PdfReader(io.BytesIO(data))
        except PdfReadError as exc:
            errors.append(f"{name}: invalid or corrupted PDF — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — mirror extractor's guard
            errors.append(f"{name}: could not read PDF — {exc}")
            continue
        if getattr(reader, "is_encrypted", False):
            errors.append(
                f"{name}: PDF is encrypted and cannot be analyzed — remove "
                "the document security and re-attach"
            )
            continue
        page_count: int | None
        try:
            page_count = len(reader.pages)
        except Exception:  # noqa: BLE001 — uncountable is not fatal
            page_count = None
        files.append(DrawingFile(path=path, name=name, data=data, page_count=page_count))
    return files, errors


# ---------------------------------------------------------------------------
# Chunk packing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestChunkPart:
    """One document block: a whole file or a page range split out of one."""

    label: str  # "plans.pdf" or "plans.pdf (pages 301-600)"
    data: bytes
    page_count: int | None


@dataclass(frozen=True)
class DigestChunk:
    """One API request's worth of drawing content."""

    index: int  # 0-based position in the digest
    parts: tuple[DigestChunkPart, ...]
    known_page_count: int  # sum of countable parts' pages
    has_uncountable: bool
    raw_bytes: int

    @property
    def labels(self) -> list[str]:
        return [part.label for part in self.parts]

    def label_summary(self) -> str:
        return ", ".join(self.labels)


def effective_page_cap(
    *,
    model: str = DRAWING_DIGEST_MODEL_DEFAULT,
    max_pages_per_request: int = API_MAX_PDF_PAGES_PER_REQUEST,
    tokens_per_page: int = DIGEST_TOKENS_PER_PAGE_ESTIMATE,
) -> int:
    """Per-chunk page cap: the API ceiling bounded by the context window.

    600 pages x ~3k tokens/page = 1.8M tokens — over even a 1M context
    window, so the window (minus the output cap and prompt overhead) is
    the binding constraint on the current models (~320 pages/chunk at the
    conservative per-page estimate).
    """
    caps = model_capabilities(model)
    usable = (
        caps.context_window
        - drawing_digest_max_tokens(model=model)
        - _DIGEST_PROMPT_OVERHEAD_TOKENS
    )
    window_pages = max(1, usable // tokens_per_page)
    return max(1, min(max_pages_per_request, window_pages))


def _split_pdf_by_pages(
    file: DrawingFile, *, max_pages: int, max_bytes: int
) -> list[DigestChunkPart]:
    """Split an oversized countable PDF into page-range parts.

    Each part is written with ``pypdf.PdfWriter`` and respects both caps;
    a written range that still exceeds the byte cap is halved recursively
    (single-page parts pass through even when oversized — the API is the
    final validator for a pathological single page).
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(file.data))
    total_pages = len(reader.pages)

    def _write_range(start: int, end: int) -> bytes:
        writer = PdfWriter()
        for page_index in range(start, end):
            writer.add_page(reader.pages[page_index])
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    def _emit(start: int, end: int, out: list[DigestChunkPart]) -> None:
        data = _write_range(start, end)
        if len(data) > max_bytes and end - start > 1:
            middle = (start + end) // 2
            _emit(start, middle, out)
            _emit(middle, end, out)
            return
        out.append(
            DigestChunkPart(
                label=f"{file.name} (pages {start + 1}-{end})",
                data=data,
                page_count=end - start,
            )
        )

    parts: list[DigestChunkPart] = []
    for start in range(0, total_pages, max_pages):
        _emit(start, min(start + max_pages, total_pages), parts)
    return parts


def build_digest_chunks(
    files: Sequence[DrawingFile],
    *,
    model: str = DRAWING_DIGEST_MODEL_DEFAULT,
    max_pages_per_request: int = API_MAX_PDF_PAGES_PER_REQUEST,
    max_bytes_per_request: int = MAX_RAW_PDF_BYTES_PER_REQUEST,
    tokens_per_page: int = DIGEST_TOKENS_PER_PAGE_ESTIMATE,
) -> list[DigestChunk]:
    """Greedily pack drawing files into per-request chunks.

    Whole files pack in input order under the page and byte caps; a file
    that exceeds either cap on its own is split by page ranges first. An
    uncountable file (pypdf can't parse the page tree) is unsplittable and
    becomes its own single-file chunk — the API validates it. The union of
    chunk parts always equals the input set; nothing is dropped.
    """
    page_cap = effective_page_cap(
        model=model,
        max_pages_per_request=max_pages_per_request,
        tokens_per_page=tokens_per_page,
    )

    # Normalize files into atomic packable parts.
    atoms: list[DigestChunkPart] = []
    for file in files:
        if file.page_count is None:
            atoms.append(
                DigestChunkPart(label=file.name, data=file.data, page_count=None)
            )
            continue
        if file.page_count > page_cap or len(file.data) > max_bytes_per_request:
            atoms.extend(
                _split_pdf_by_pages(
                    file, max_pages=page_cap, max_bytes=max_bytes_per_request
                )
            )
            continue
        atoms.append(
            DigestChunkPart(
                label=file.name, data=file.data, page_count=file.page_count
            )
        )

    chunks: list[DigestChunk] = []
    current: list[DigestChunkPart] = []
    current_pages = 0
    current_bytes = 0

    def _flush() -> None:
        nonlocal current, current_pages, current_bytes
        if not current:
            return
        chunks.append(
            DigestChunk(
                index=len(chunks),
                parts=tuple(current),
                known_page_count=sum(p.page_count or 0 for p in current),
                has_uncountable=any(p.page_count is None for p in current),
                raw_bytes=sum(len(p.data) for p in current),
            )
        )
        current = []
        current_pages = 0
        current_bytes = 0

    for atom in atoms:
        if atom.page_count is None:
            # Unsplittable and unaccountable against the page cap: isolate it
            # so a bad page estimate can never sink a sibling file's chunk.
            _flush()
            current = [atom]
            _flush()
            continue
        fits = (
            current_pages + atom.page_count <= page_cap
            and current_bytes + len(atom.data) <= max_bytes_per_request
        )
        if current and not fits:
            _flush()
        current.append(atom)
        current_pages += atom.page_count
        current_bytes += len(atom.data)
    _flush()
    return chunks


# ---------------------------------------------------------------------------
# Prompt + message builders
# ---------------------------------------------------------------------------


def build_digest_system_prompt() -> str:
    """Protocol/format contract for the digest. Engine-owned, domain-neutral.

    Byte-identical across runs, chunks, and modules — the per-run bits
    (chunk manifest, module display name) go in the user text so the
    system-prompt cache breakpoint holds across every chunk in a run.
    """
    return (
        "You are analyzing a set of construction drawings (plan sheets) to "
        "produce a plain-text digest for a specification review team. The "
        "digest becomes reference context for reviewers checking the "
        "project's written specifications against the drawings, so accuracy "
        "and traceability matter more than prose.\n"
        "\n"
        "Produce the digest under exactly these section headings, in this "
        "order, each heading on its own line:\n"
        "\n"
        "PROJECT IDENTITY & OVERVIEW\n"
        "SHEET INDEX\n"
        "GENERAL NOTES\n"
        "SCHEDULES\n"
        "COORDINATION OBSERVATIONS\n"
        "\n"
        "Section contents:\n"
        "- PROJECT IDENTITY & OVERVIEW: project name, address/location, "
        "owner/client, design team, building type and approximate size, and "
        "a short overview of the systems shown (mechanical, plumbing, fire "
        "protection, electrical, and so on).\n"
        "- SHEET INDEX: one line per sheet: sheet number, sheet title.\n"
        "- GENERAL NOTES: transcribe the general notes, verbatim where "
        "legible, prioritizing requirements-bearing notes (codes, standards, "
        "performance requirements, submittal and coordination obligations) "
        "over boilerplate.\n"
        "- SCHEDULES: transcribe equipment/fixture/finish schedules as "
        "compact plain-text tables (pipe-separated columns are fine).\n"
        "- COORDINATION OBSERVATIONS: cross-sheet observations useful to a "
        "spec reviewer — conflicts, ambiguities, and items the written "
        "specifications will need to cover.\n"
        "\n"
        "Rules:\n"
        "- Cite the source of every extracted fact as [<file> p.N], where N "
        "is the PDF page number within the named file.\n"
        "- Transcribe faithfully. Mark anything you cannot read as "
        "[ILLEGIBLE]; never guess values, model numbers, or code "
        "references.\n"
        "- If a section has no content in these sheets, write exactly "
        '"None found in this portion." under its heading.\n'
        "- Plain text only: no markdown emphasis, no images, no code "
        "fences.\n"
        "- Length: aim for at most about 12,000 tokens for this portion of "
        "the drawing set. If the sheets are dense, prioritize GENERAL NOTES "
        "and SCHEDULES over exhaustive sheet-by-sheet narration."
    )


def build_chunk_user_text(
    chunk: DigestChunk, *, total_chunks: int, module_display_name: str = ""
) -> str:
    """Per-chunk user instruction: manifest + optional module focus line."""
    lines = [
        "Analyze the attached construction-drawing PDF(s) and produce the "
        "digest described in your instructions.",
        "",
        f"This is chunk {chunk.index + 1} of {total_chunks} of the drawing "
        "set. Files in this chunk:",
    ]
    for part in chunk.parts:
        pages = (
            f"{part.page_count} page(s)"
            if part.page_count is not None
            else "page count unknown"
        )
        lines.append(f"- {part.label}: {pages}")
    if module_display_name:
        lines.extend(
            [
                "",
                f"This digest supports a {module_display_name} specification "
                "review — prioritize content relevant to that discipline.",
            ]
        )
    if total_chunks > 1:
        lines.extend(
            [
                "",
                "Other chunks of this drawing set are analyzed separately; "
                "describe only the sheets attached here and do not speculate "
                "about sheets that are not included.",
            ]
        )
    return "\n".join(lines)


def build_chunk_messages(
    chunk: DigestChunk, *, total_chunks: int, module_display_name: str = ""
) -> list[dict]:
    """One user message: document blocks first, instruction text last.

    The document-block shape is the GA native PDF input — **no beta
    header** (cautionary precedent: the retired web-fetch beta header
    crashed every fetch-eligible verification request; see CLAUDE.md).
    ``base64.b64encode`` emits no newlines, which the API requires.
    """
    content: list[dict] = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(part.data).decode("ascii"),
            },
        }
        for part in chunk.parts
    ]
    content.append(
        {
            "type": "text",
            "text": build_chunk_user_text(
                chunk,
                total_chunks=total_chunks,
                module_display_name=module_display_name,
            ),
        }
    )
    return [{"role": "user", "content": content}]


def _build_request_kwargs(*, model: str) -> dict:
    """Shared request shape for the real call and the count_tokens preflight."""
    request_kwargs: dict = {
        "model": model,
        "max_tokens": drawing_digest_max_tokens(model=model),
        "system": system_prompt_with_cache(
            build_digest_system_prompt(), phase=PHASE_DRAWING_DIGEST
        ),
        # No ``tools`` key at all: the digest transcribes documents it was
        # handed. No tools also means no server pauses — an unexpected
        # ``pause_turn`` is treated as a chunk failure rather than resumed.
    }
    apply_thinking_config(request_kwargs, model=model, phase=PHASE_DRAWING_DIGEST)
    apply_effort_config(request_kwargs, model=model, phase=PHASE_DRAWING_DIGEST)
    return request_kwargs


# ---------------------------------------------------------------------------
# Cost preflight
# ---------------------------------------------------------------------------


@dataclass
class DigestPreflight:
    """Input-token / cost forecast for a chunked digest."""

    per_chunk_input_tokens: list[int]
    total_input_tokens: int
    exact: bool  # False: at least one chunk used the local pages-based estimate
    max_output_tokens: int  # output cap x chunk count (the cost ceiling side)
    estimated_max_cost_usd: float | None  # None: model unknown to the pricing table
    over_window_chunk_indices: list[int] = field(default_factory=list)


def preflight_digest_cost(
    chunks: Sequence[DigestChunk],
    *,
    model: str = DRAWING_DIGEST_MODEL_DEFAULT,
    module_display_name: str = "",
    client: Any = None,
) -> DigestPreflight:
    """Forecast the digest's input tokens and worst-case cost.

    Uses the exact ``count_tokens`` endpoint per chunk (free; accepts
    document blocks) — the full base64 payload is already built for the
    real call, and exact beats the 2x error band of the per-page estimate.
    Any chunk whose exact count fails falls back to the local estimate
    (``pages x DIGEST_TOKENS_PER_PAGE_ESTIMATE`` + prompt text) and flips
    ``exact`` off. A chunk whose exact count cannot fit the model's context
    window (input + output cap) is recorded in
    ``over_window_chunk_indices`` so the caller can refuse before the API
    400s mid-run.
    """
    system_prompt = build_digest_system_prompt()
    output_cap = drawing_digest_max_tokens(model=model)
    context_window = model_capabilities(model).context_window

    per_chunk: list[int] = []
    exact = True
    over_window: list[int] = []
    for chunk in chunks:
        messages = build_chunk_messages(
            chunk, total_chunks=len(chunks), module_display_name=module_display_name
        )
        counted = count_tokens_via_api(
            model=model, system=system_prompt, messages=messages, client=client
        )
        if counted is None:
            exact = False
            prompt_text = system_prompt + "\n" + build_chunk_user_text(
                chunk,
                total_chunks=len(chunks),
                module_display_name=module_display_name,
            )
            counted = (
                chunk.known_page_count * DIGEST_TOKENS_PER_PAGE_ESTIMATE
                + count_tokens(prompt_text)
            )
        else:
            if counted + output_cap > context_window:
                over_window.append(chunk.index)
        per_chunk.append(int(counted))

    total_input = sum(per_chunk)
    max_output = output_cap * len(chunks)
    return DigestPreflight(
        per_chunk_input_tokens=per_chunk,
        total_input_tokens=total_input,
        exact=exact,
        max_output_tokens=max_output,
        estimated_max_cost_usd=estimate_request_cost(
            total_input, max_output, model=model, batch=False
        ),
        over_window_chunk_indices=over_window,
    )


def format_digest_confirm_message(
    preflight: DigestPreflight,
    *,
    chunks: Sequence[DigestChunk],
    model: str,
) -> str:
    """Confirm-dialog body for the GUI (pure so it is hermetically testable)."""
    file_labels: list[str] = []
    for chunk in chunks:
        file_labels.extend(chunk.labels)
    total_pages = sum(chunk.known_page_count for chunk in chunks)
    any_uncountable = any(chunk.has_uncountable for chunk in chunks)

    pages_line = f"{total_pages:,} page(s)"
    if any_uncountable:
        pages_line += " (some files' page counts could not be determined)"
    tokens_line = (
        f"{preflight.total_input_tokens:,} input tokens"
        + ("" if preflight.exact else " (estimated)")
    )
    lines = [
        "Analyze these construction drawings with "
        f"{friendly_model_name(model)}?",
        "",
        f"Files: {len(file_labels)} document(s) — {pages_line}",
        f"Requests: {len(chunks)}",
        f"Size: {tokens_line}",
    ]
    if preflight.estimated_max_cost_usd is not None:
        lines.append(f"Cost: up to ${preflight.estimated_max_cost_usd:,.2f}")
    else:
        lines.append("Cost: unknown (model not in the pricing table)")
    lines.extend(
        [
            "",
            "The result is an editable text digest added to Project Context — "
            "review it there before submitting a spec review.",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class ChunkStatus:
    """One chunk's terminal state + billing telemetry."""

    chunk_index: int
    file_labels: list[str]
    page_count: int
    status: str  # "completed" | "truncated" | "failed"
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _ChunkOutcome:
    status: ChunkStatus
    text: str = ""


@dataclass
class DrawingDigestResult:
    """The merged digest plus per-chunk statuses and usage totals."""

    digest_text: str  # UNwrapped; callers apply :func:`wrapped_digest_block`
    chunk_statuses: list[ChunkStatus]
    model: str

    @property
    def completed_chunks(self) -> int:
        return sum(1 for s in self.chunk_statuses if s.status in ("completed", "truncated"))

    @property
    def truncated_chunks(self) -> int:
        return sum(1 for s in self.chunk_statuses if s.status == "truncated")

    @property
    def failed_chunks(self) -> int:
        return sum(1 for s in self.chunk_statuses if s.status == "failed")

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.chunk_statuses)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.chunk_statuses)

    def actual_cost_usd(self) -> float | None:
        """Post-hoc cost of the run from summed usage (None: unknown model)."""
        return estimate_request_cost(
            self.total_input_tokens,
            self.total_output_tokens,
            model=self.model,
            batch=False,
        )


def _collect_response_text(response: Any) -> str:
    """Concatenate a response's text blocks (attribute or dict shaped)."""
    chunks: list[str] = []
    for block in getattr(response, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            chunks.append(str(text))
    return "\n".join(chunks)


def _apply_usage(status: ChunkStatus, responses: list[Any]) -> None:
    """Sum token/cache usage across a chunk's responses onto its status."""
    for response in responses:
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        status.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        status.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        cache = extract_cache_usage(usage)
        status.cache_creation_input_tokens += cache["cache_creation_input_tokens"]
        status.cache_read_input_tokens += cache["cache_read_input_tokens"]


def _run_digest_chunk(
    client: Any,
    chunk: DigestChunk,
    *,
    total_chunks: int,
    model: str,
    module_display_name: str,
) -> _ChunkOutcome:
    """One chunk's lifecycle: request -> retries -> text.

    Never raises (KeyboardInterrupt/SystemExit excepted): every failure
    path returns a ``failed`` outcome so the fan-out's partial-failure
    policy is enforced in one place. Runs on a worker thread — no ``log``
    or ``diag`` calls here; telemetry rides the outcome back.
    """
    request_kwargs = _build_request_kwargs(model=model)
    messages = build_chunk_messages(
        chunk, total_chunks=total_chunks, module_display_name=module_display_name
    )

    def _status(status: str, *, error: str = "") -> ChunkStatus:
        return ChunkStatus(
            chunk_index=chunk.index,
            file_labels=chunk.labels,
            page_count=chunk.known_page_count,
            status=status,
            error=error,
        )

    policy = DEFAULT_REALTIME_RETRY_POLICY
    attempts_planned = max(1, policy.max_attempts)

    # Responses billed by earlier, retried attempts — a failed chunk must
    # never read as cheaper than it actually was (research convention).
    billed_responses: list[Any] = []

    for attempt in range(attempts_planned):
        is_last_attempt = attempt == attempts_planned - 1
        response: Any = None
        try:
            with client.messages.stream(messages=messages, **request_kwargs) as stream:
                response = stream.get_final_message()
            stop_reason = getattr(response, "stop_reason", None)
            responses = [*billed_responses, response]
            if stop_reason in ("end_turn", "stop_sequence"):
                text = _collect_response_text(response).strip()
                if not text:
                    status = _status(
                        "failed", error="Digest response contained no text."
                    )
                    _apply_usage(status, responses)
                    return _ChunkOutcome(status=status)
                status = _status("completed")
                _apply_usage(status, responses)
                return _ChunkOutcome(status=status, text=text)
            if stop_reason == "max_tokens":
                # Keep the paid-for text; make the truncation visible in the
                # digest itself rather than silently shipping a partial.
                text = _collect_response_text(response).strip()
                status = _status(
                    "truncated",
                    error="Digest output hit the max_tokens cap.",
                )
                _apply_usage(status, responses)
                return _ChunkOutcome(
                    status=status,
                    text=f"{_TRUNCATION_WARNING}\n{text}".strip(),
                )
            # No tools are attached, so pause_turn (a server-tool pause)
            # should be impossible; anything else here is equally terminal.
            status = _status(
                "failed",
                error=f"Digest response incomplete (stop_reason: {stop_reason}).",
            )
            _apply_usage(status, responses)
            return _ChunkOutcome(status=status)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 — classified below
            attempt_responses = [response] if response is not None else []
            failure_class = classify_exception(exc)
            if not is_retryable_failure_class(failure_class) or is_last_attempt:
                status = _status("failed", error=f"{type(exc).__name__}: {exc}")
                _apply_usage(status, [*billed_responses, *attempt_responses])
                return _ChunkOutcome(status=status)
            billed_responses.extend(attempt_responses)
            backoff = compute_backoff_seconds(
                policy, attempt=attempt, failure_class=failure_class
            )
            time.sleep(backoff)
    status = _status("failed", error=f"Digest failed after {attempts_planned} attempts.")
    _apply_usage(status, billed_responses)
    return _ChunkOutcome(status=status)


def _merge_digest_text(
    outcomes: dict[int, _ChunkOutcome],
    chunks: Sequence[DigestChunk],
    *,
    model: str,
) -> str:
    """Deterministic merge in chunk-index order, failures inlined visibly."""
    total_files = sum(len(chunk.parts) for chunk in chunks)
    total_pages = sum(chunk.known_page_count for chunk in chunks)
    header = [
        "CONSTRUCTION DRAWING DIGEST",
        f"Produced by {friendly_model_name(model)} from {total_files} "
        f"document(s), {total_pages:,} page(s), across {len(chunks)} "
        "request(s).",
        "Page references use [<file> p.N]. Content marked [ILLEGIBLE] could "
        "not be read from the drawings — verify visually.",
    ]
    sections: list[str] = ["\n".join(header)]
    for chunk in chunks:
        outcome = outcomes[chunk.index]
        position = f"Chunk {chunk.index + 1} of {len(chunks)}"
        if outcome.status.status == "failed":
            sections.append(
                f"[{position} ({chunk.label_summary()}) FAILED: "
                f"{outcome.status.error}]"
            )
            continue
        if len(chunks) > 1:
            sections.append(
                f"--- {position}: {chunk.label_summary()} ---\n{outcome.text}"
            )
        else:
            sections.append(outcome.text)
    return "\n\n".join(sections)


def run_drawing_digest(
    chunks: Sequence[DigestChunk],
    *,
    model: str = DRAWING_DIGEST_MODEL_DEFAULT,
    module_display_name: str = "",
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    diag: Any = None,
    client: Any = None,
) -> DrawingDigestResult:
    """Run every chunk in parallel; merge into one digest.

    Failure policy: per-chunk failures are inlined into the digest text and
    recorded in ``chunk_statuses``; if EVERY chunk fails this raises
    :exc:`DrawingDigestError` — there is nothing worth merging into
    Project Context.
    """
    if not chunks:
        raise DrawingDigestError("No drawing content to digest.")
    if client is None:
        # Lazy import (tokenizer.py precedent) keeps module import light and
        # avoids a hard SDK dependency for the pure helpers above.
        from ..review.reviewer import _get_client

        client = _get_client()

    total = len(chunks)
    progress(0.0, f"Analyzing drawings (0/{total} request(s))...")

    outcomes: dict[int, _ChunkOutcome] = {}
    with ThreadPoolExecutor(max_workers=min(_DIGEST_MAX_WORKERS, total)) as pool:
        futures = {
            pool.submit(
                _run_digest_chunk,
                client,
                chunk,
                total_chunks=total,
                model=model,
                module_display_name=module_display_name,
            ): chunk
            for chunk in chunks
        }
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # noqa: BLE001 — one chunk never kills the fan-out
                outcome = _ChunkOutcome(
                    status=ChunkStatus(
                        chunk_index=chunk.index,
                        file_labels=chunk.labels,
                        page_count=chunk.known_page_count,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            outcomes[chunk.index] = outcome
            status = outcome.status
            if status.status == "failed":
                log(
                    f"Drawing chunk {chunk.index + 1}/{total} "
                    f"({chunk.label_summary()}) FAILED: {status.error}",
                    level="warning",
                )
            else:
                suffix = " (output truncated)" if status.status == "truncated" else ""
                log(
                    f"Drawing chunk {chunk.index + 1}/{total} "
                    f"({chunk.label_summary()}) analyzed: "
                    f"{status.output_tokens:,} output tokens{suffix}.",
                    level="info",
                )
            _record_chunk_diag(diag, status, model)
            progress(
                0.0, f"Analyzing drawings ({len(outcomes)}/{total} request(s))..."
            )

    statuses = [outcomes[chunk.index].status for chunk in chunks]
    completed = sum(1 for s in statuses if s.status != "failed")
    if completed == 0:
        errors = "; ".join(
            f"chunk {s.chunk_index + 1} ({', '.join(s.file_labels)}): {s.error}"
            for s in statuses
        )
        raise DrawingDigestError(
            f"All {len(statuses)} drawing-digest request(s) failed — no digest "
            f"was produced. {errors}"
        )

    return DrawingDigestResult(
        digest_text=_merge_digest_text(outcomes, chunks, model=model),
        chunk_statuses=statuses,
        model=model,
    )


def _record_chunk_diag(diag: Any, status: ChunkStatus, model: str) -> None:
    """Defensive diagnostics hook (duck-typed; absence/failure never sinks a run)."""
    if diag is None:
        return
    try:
        diag.record_api_call(
            phase="drawing_digest",
            mode="realtime",
            model=model,
            input_tokens=status.input_tokens,
            output_tokens=status.output_tokens,
            cache_creation_input_tokens=status.cache_creation_input_tokens,
            cache_read_input_tokens=status.cache_read_input_tokens,
            error=status.error or None,
        )
    except Exception:  # noqa: BLE001 — diagnostics must never sink the digest
        return


# ---------------------------------------------------------------------------
# Convenience entry points
# ---------------------------------------------------------------------------


def digest_drawing_files(
    paths: Sequence[Path],
    *,
    model: str = DRAWING_DIGEST_MODEL_DEFAULT,
    module_display_name: str = "",
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    diag: Any = None,
    client: Any = None,
) -> DrawingDigestResult:
    """Headless one-shot: validate -> chunk -> digest.

    Raises :exc:`DrawingDigestError` when no file validates (the per-file
    errors are joined into the message); per-file errors alongside valid
    files are logged as warnings and the run continues without them.
    """
    files, errors = validate_drawing_files(paths)
    for error in errors:
        log(f"Drawing file skipped — {error}", level="warning")
    if not files:
        raise DrawingDigestError(
            "No usable drawing PDFs: " + "; ".join(errors or ["no files given"])
        )
    chunks = build_digest_chunks(files, model=model)
    return run_drawing_digest(
        chunks,
        model=model,
        module_display_name=module_display_name,
        log=log,
        progress=progress,
        diag=diag,
        client=client,
    )


def wrapped_digest_block(result: DrawingDigestResult) -> str:
    """The digest as a Project Context attachment block."""
    return wrap_attachment(DIGEST_ATTACHMENT_LABEL, result.digest_text)
