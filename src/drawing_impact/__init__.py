"""Drawing-impact synthesis package.

One post-review pass that explains, for the exported report, how the
attached construction drawings informed the specification review. See
:mod:`.impact_synthesizer` for the full rationale.
"""
from __future__ import annotations

from .impact_synthesizer import (
    DrawingFindingLink,
    DrawingImpactResult,
    build_impact_system_prompt,
    build_impact_user_message,
    extract_drawing_digest,
    render_findings_block,
    run_drawing_impact,
)

__all__ = [
    "DrawingFindingLink",
    "DrawingImpactResult",
    "build_impact_system_prompt",
    "build_impact_user_message",
    "extract_drawing_digest",
    "render_findings_block",
    "run_drawing_impact",
]
