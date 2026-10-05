"""One output recovery allowance shared by every request in a package pass."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Sequence

from .api_config import apply_cache_usage, merge_cache_usage
from .chunked_pass import Measure, PlannedChunk, plan_chunks
from ..review.reviewer import ReviewResult

if TYPE_CHECKING:
    from ..modules.base import ChunkGroup


OUTPUT_RECOVERY_NOTE = (
    "Output reached max_tokens; this was the pass's single recovery with "
    "fewer specifications per request. Relationships between specifications "
    "in different chunks were not evaluated together."
)


@dataclass
class PassRecovery:
    """Transport retries are separate; parsing and output splitting share this."""

    used: bool = False

    def claim(self) -> bool:
        if self.used:
            return False
        self.used = True
        return True


def carry_pass_usage(result: ReviewResult, previous: ReviewResult) -> ReviewResult:
    """Keep billed responses when replacing a pass with its recovery result."""
    result.input_tokens += previous.input_tokens
    result.output_tokens += previous.output_tokens
    apply_cache_usage(result, merge_cache_usage(result, previous))
    result.elapsed_seconds += previous.elapsed_seconds
    return result


def recover_truncated_pass(
    result: ReviewResult,
    *,
    spec_count: int,
    min_specs: int,
    recovery: PassRecovery,
    rerun: Callable[[int], ReviewResult],
) -> ReviewResult:
    """Rerun with half the specs per request (rounded up), once for the pass.

    An indivisible package or a plan with no runnable chunks keeps its
    original outcome. No incomplete payload becomes coverage evidence.
    """
    if spec_count <= min_specs or not recovery.claim():
        return result
    recovered = rerun((spec_count + 1) // 2)
    if recovered.cross_check_status == "skipped":
        return carry_pass_usage(result, recovered)
    return carry_pass_usage(recovered, result)


def recover_output_chunk(
    entry: PlannedChunk, result: ReviewResult, *, recovery: PassRecovery,
    groups: Sequence[ChunkGroup], measure: Measure, min_specs: int, pass_name: str,
) -> list[PlannedChunk] | None:
    """Split one truncated chunk without redoing its completed siblings."""
    if (
        result.stop_reason != "max_tokens"
        or len(entry.specs) <= min_specs
        or not recovery.claim()
    ):
        return None
    plan = plan_chunks(
        entry.specs, groups, measure=measure, min_specs=min_specs,
        pass_name=pass_name, max_specs=(len(entry.specs) + 1) // 2,
    )
    if not any(part.runnable and len(part.specs) >= min_specs for part in plan):
        return None
    return [replace(
        part, chunk_id=f"{entry.chunk_id}/recovery/{part.chunk_id}",
        label=f"{entry.label} / {part.label}",
    ) for part in plan]
