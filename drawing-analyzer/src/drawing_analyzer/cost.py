"""Pre-run cost estimate for a drawing set (feeds the cost-confirm dialog).

Reading drawings is the app's most expensive action — one Opus 4.8 vision call
per sheet, each carrying the overview image plus every grid tile. This estimates
the spend *before* the run so the GUI can surface it and let the operator
confirm or cancel. It is deliberately a rough, slightly-high estimate (the image
-token figure is the per-model worst case, and per-sheet output/prompt sizes are
fixed assumptions) — the goal is an honest order-of-magnitude heads-up, not an
invoice. Pure + hermetic (no PyMuPDF, no network): the caller supplies the sheet
count (cheap to obtain via ``render.list_sheets``).
"""
from __future__ import annotations

from dataclasses import dataclass

from .core.api_config import REVIEW_MODEL_DEFAULT
from .core.pricing import estimate_request_cost, friendly_model_name
from . import tiling
from .pipeline import estimate_image_tokens_for_set

# Per-sheet text overhead of the digest prompt (system + user instruction); the
# images dominate, so a fixed estimate is fine.
_ASSUMED_PROMPT_TOKENS_PER_SHEET = 800
# Typical structured digest output per sheet — well under the 16k cap; real
# digests rarely approach it, so using the cap would wildly overstate cost.
_ASSUMED_OUTPUT_TOKENS_PER_SHEET = 2_000
# The synthesis pass emits one set-level overview.
_ASSUMED_SYNTHESIS_OUTPUT_TOKENS = 2_000


@dataclass(frozen=True)
class DrawingCostEstimate:
    sheet_count: int
    file_count: int
    model: str
    image_tokens: int
    input_tokens: int
    output_tokens: int
    total_cost: float | None  # None when the model's pricing is unknown
    batch: bool = False  # estimate reflects the 50% Batch-API discount


def estimate_drawing_set_cost(
    sheet_count: int,
    *,
    file_count: int = 0,
    model: str = REVIEW_MODEL_DEFAULT,
    rows: int = tiling.DEFAULT_GRID_ROWS,
    cols: int = tiling.DEFAULT_GRID_COLS,
    synthesize: bool = True,
    batch: bool = False,
) -> DrawingCostEstimate:
    """Estimate the cost of digesting ``sheet_count`` sheets.

    ``synthesize`` mirrors the run: when on (and ≥2 sheets) it adds the
    text-only cross-sheet pass, whose input is roughly the per-sheet digests fed
    back in. Image tokens are folded into ``input_tokens`` (vision is billed as
    input). ``batch=True`` applies the 50% Message Batches discount to the
    per-sheet digest spend (the dominant cost); the synthesis pass runs
    synchronously, but folding it in at the batch rate too keeps the estimate
    deliberately slightly-high rather than under-stated.
    """
    image_tokens = estimate_image_tokens_for_set(
        sheet_count, rows=rows, cols=cols, model=model
    )
    digest_output = sheet_count * _ASSUMED_OUTPUT_TOKENS_PER_SHEET
    input_tokens = image_tokens + sheet_count * _ASSUMED_PROMPT_TOKENS_PER_SHEET
    output_tokens = digest_output

    if synthesize and sheet_count >= 2:
        # Synthesis re-reads the per-sheet digests (≈ digest_output) as text.
        input_tokens += digest_output + _ASSUMED_PROMPT_TOKENS_PER_SHEET
        output_tokens += _ASSUMED_SYNTHESIS_OUTPUT_TOKENS

    total_cost = estimate_request_cost(
        input_tokens, output_tokens, model=model, batch=batch
    )
    return DrawingCostEstimate(
        sheet_count=sheet_count,
        file_count=file_count,
        model=model,
        image_tokens=image_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost=total_cost,
        batch=batch,
    )


def format_drawing_cost_prompt(est: DrawingCostEstimate) -> str:
    """Human-readable confirmation message for the cost-confirm dialog."""
    where = f" from {est.file_count} file(s)" if est.file_count else ""
    how = (
        "submitted as one Message Batch (≈50% cheaper than real-time)"
        if est.batch
        else "one vision call per sheet"
    )
    lines = [
        f"About to analyze {est.sheet_count} drawing sheet(s){where} with "
        f"{friendly_model_name(est.model)} vision — {how}.",
        "",
        f"Estimated usage: ~{est.input_tokens:,} input tokens "
        f"(~{est.image_tokens:,} from images) / ~{est.output_tokens:,} output.",
    ]
    if est.total_cost is not None:
        batch_note = " (Batch rate)" if est.batch else ""
        lines.append(
            f"Estimated cost: ~${est.total_cost:,.2f}{batch_note} — a rough, "
            "slightly-high estimate; actual cost varies with sheet complexity, "
            "and cached sheets cost nothing."
        )
    else:
        lines.append("Estimated cost: unavailable for this model.")
    if est.batch:
        lines += [
            "",
            "Nothing is sent until you confirm. The batch runs in the "
            "background — usually a few minutes, but it can take up to an hour "
            "(occasionally longer) before the digest is ready to attach.",
        ]
    lines += ["", "Proceed with the analysis?"]
    return "\n".join(lines)
