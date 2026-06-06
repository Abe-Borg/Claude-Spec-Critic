"""Per-sheet vision digest: one rendered drawing sheet -> structured text.

Each sheet is sent to Claude Opus 4.8 in a *single* request carrying the
overview image plus all grid tiles, so the model reads the whole sheet at once.
The model auto-detects the sheet number and discipline from the title block and
emits a structured text digest suitable for splicing into the spec reviewer's
Project Context. Output is plain text (markdown) — no tool schema — because the
digest is reference prose for a downstream text-only pipeline.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from ..core.api_config import (
    REVIEW_MODEL_DEFAULT,
    model_supports_adaptive_thinking,
    model_supports_effort,
)
from ..core.tokenizer import estimate_image_tokens_total
from .models import RenderedSheet, SheetRef

# Room for adaptive thinking plus a thorough per-sheet digest; stays at/under the
# ~16k non-streaming-safe ceiling so a single sheet completes well within the
# SDK request timeout.
DEFAULT_DIGEST_MAX_TOKENS = 16_000

# Effort for the read. "high" is intelligence-appropriate for dense drawings and
# is accepted by both Opus and Sonnet (so a model override never 400s on it).
DEFAULT_DIGEST_EFFORT = "high"


DIGEST_SYSTEM_PROMPT = """\
You are a senior MEP (mechanical / plumbing / fire-protection) engineer reading \
California K-12 / community-college DSA construction drawings. Your job is to \
produce a precise, factual TEXT digest of ONE drawing sheet so that a separate \
specification reviewer — who will NOT see the drawings — can check written specs \
against what the drawings actually show.

You are given that single sheet as:
  1. an OVERVIEW image (the entire sheet at lower resolution, for global layout \
and match-lines), followed by
  2. a grid of high-resolution TILES that together cover the same sheet, with \
slight overlap. Each tile is labeled with its grid position.

The tiles and overview are the SAME sheet — synthesize them into one coherent \
understanding. Do not describe them tile-by-tile or repeat content that appears \
in overlapping tiles.

Extract, in this order, only what you can actually read on the sheet:

- **Header line**: `Sheet <number> - <discipline> - <title>` from the title \
block (discipline = Mechanical / Plumbing / Fire Protection / Plumbing-Fire / \
Controls / etc.). If a field is illegible, say so rather than guessing.
- **Scope / systems shown** on this sheet.
- **Equipment & schedules**: transcribe schedule rows that matter — tag/mark, \
type, capacity/size, model or basis-of-design, and any noted standard. Keep tags \
verbatim (e.g. `VAV-3`, `WH-1`, `FP-2`).
- **Plan content**: spaces/rooms shown and what equipment, routing, or risers \
serve them; use the sheet's own column grid bubbles / match-lines / detail \
callouts as the spatial reference frame where possible.
- **Key dimensions, elevations, clearances, slopes, pipe/duct sizes** that a \
spec would need to be consistent with.
- **General notes, keynotes, and callouts** (transcribe the substantive ones).
- **Coordination / cross-discipline items**: penetrations, shared chases, \
equipment served by another discipline, anything that must agree across trades \
or with the specifications.

Rules:
- Report only what is legible on THIS sheet. Never invent values, tags, models, \
or code citations. If something is cut off or unreadable, write \
`[illegible]` / `[partially legible]` rather than guessing.
- Be concise but complete — favor transcribed tags/values over prose.
- Output Markdown. Begin with the header line, then the sections above as they \
apply. Omit a section if the sheet has nothing for it."""


_DIGEST_TASK_INSTRUCTION = (
    "Now produce the structured text digest of this single sheet, following the "
    "format in your instructions. Begin with the `Sheet <number> - <discipline> "
    "- <title>` header line."
)


def _image_block(png_bytes: bytes) -> dict:
    """A base64 PNG image content block."""
    data = base64.standard_b64encode(png_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": data,
        },
    }


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def build_user_content(sheet: RenderedSheet) -> list[dict]:
    """Assemble the user-turn content blocks for one sheet.

    Order: framing text -> overview image -> (label + tile image) per tile ->
    final task instruction. Keeping the task instruction last places the bulk of
    the imagery before the question, per the vision/PDF best practice, while the
    per-tile labels give the model a coarse placement frame for each crop.
    """
    blocks: list[dict] = [
        _text_block(
            f"You are given ONE construction drawing sheet "
            f"({sheet.ref.display_label}), rendered as a low-resolution overview "
            f"followed by a {sheet.rows}x{sheet.cols} grid of overlapping "
            f"high-resolution tiles. Read them together as a single sheet."
        ),
        _text_block("OVERVIEW (entire sheet):"),
        _image_block(sheet.overview.png_bytes),
    ]
    for tile in sheet.tiles:
        blocks.append(
            _text_block(
                f"Tile r{tile.row + 1}c{tile.col + 1} of "
                f"{sheet.rows}x{sheet.cols} ({tile.label}):"
            )
        )
        blocks.append(_image_block(tile.png_bytes))
    blocks.append(_text_block(_DIGEST_TASK_INSTRUCTION))
    return blocks


@dataclass
class SheetDigest:
    """Result of digesting one sheet."""

    ref: SheetRef
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    image_token_estimate: int = 0
    stop_reason: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


# ---------------------------------------------------------------------------
# SDK-shape-tolerant accessors (mirror the reviewer/verifier parsers: handle
# both attribute-style SDK objects and plain dicts).
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _message_text(resp: Any) -> str:
    content = _get(resp, "content", []) or []
    parts: list[str] = []
    for block in content:
        if _get(block, "type") == "text":
            text = _get(block, "text", "") or ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _message_usage(resp: Any) -> tuple[int, int]:
    usage = _get(resp, "usage")
    if usage is None:
        return 0, 0
    return (
        int(_get(usage, "input_tokens", 0) or 0),
        int(_get(usage, "output_tokens", 0) or 0),
    )


def digest_sheet(
    sheet: RenderedSheet,
    *,
    client: Any = None,
    model: str = REVIEW_MODEL_DEFAULT,
    max_tokens: int = DEFAULT_DIGEST_MAX_TOKENS,
    use_thinking: bool = True,
    effort: str | None = DEFAULT_DIGEST_EFFORT,
) -> SheetDigest:
    """Run a single vision request for one sheet and return its text digest.

    ``client`` is injectable for tests; when ``None`` the shared Anthropic client
    factory is used. Any API/parse failure is captured on ``SheetDigest.error``
    (never raised) so a set keeps processing the remaining sheets.
    """
    image_est = estimate_image_tokens_total(sheet.image_sizes, model=model)

    if client is None:
        from ..review.reviewer import _get_client

        client = _get_client()

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": DIGEST_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_content(sheet)}],
    }
    if use_thinking and model_supports_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "adaptive"}
    if effort and model_supports_effort(model):
        kwargs["output_config"] = {"effort": effort}

    try:
        resp = client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - report, don't sink the whole set
        return SheetDigest(
            ref=sheet.ref,
            text="",
            image_token_estimate=image_est,
            error=str(exc),
        )

    text = _message_text(resp)
    in_tok, out_tok = _message_usage(resp)
    stop = _get(resp, "stop_reason")

    error: str | None = None
    if not text:
        error = f"empty digest (stop_reason={stop!r})"

    return SheetDigest(
        ref=sheet.ref,
        text=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        image_token_estimate=image_est,
        stop_reason=stop,
        error=error,
    )
