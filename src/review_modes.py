"""Review mode definitions (Phase 8 / plan section 12.1).

The review prompt previously asked one model call to be a strict reviewer,
a deep AEC reviewer, and an edit generator at the same time. Phase 8 splits
those concerns into three explicit modes so the GUI / pipeline can pick the
behavior that matches the workflow without overloading the prompt.

Modes
-----
STRICT
    Evidence-backed contradictions, code-cycle issues, and invalid
    references only. The output is the most conservative — fewer findings,
    higher precision, no speculative editorial calls.

COMPREHENSIVE
    Strict scope plus AEC constructability, coordination, missing
    requirements, and the broader practical-quality issues listed in plan
    section 12.2 (TAB/commissioning, equipment schedule alignment,
    Division 01 coordination, warranty, basis-of-design, controls
    sequences, DSA/HCAI/Title 24 closeout, seismic restraints,
    fire/smoke damper access, sprinkler hydraulics, pipe/duct material
    coordination, submittal and O&M conflicts). Default for the GUI.

SAFE_EDIT
    Restricts findings to those with exact editable anchors and low-risk
    replacements. Useful when the user wants only auto-applicable
    suggestions in the output. Edit-safety classification still happens
    downstream; this mode just biases the model toward proposing edits
    only when an unambiguous anchor exists.

Each mode keeps the same JSON output schema so downstream parsing,
deduplication, verification, and edit-planning code is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReviewMode(str, Enum):
    STRICT = "strict"
    COMPREHENSIVE = "comprehensive"
    SAFE_EDIT = "safe_edit"


@dataclass(frozen=True)
class ReviewModeProfile:
    """Display + prompt metadata for a review mode."""

    mode: ReviewMode
    label: str
    short_description: str

    @property
    def value(self) -> str:
        return self.mode.value


REVIEW_MODE_PROFILES: dict[ReviewMode, ReviewModeProfile] = {
    ReviewMode.STRICT: ReviewModeProfile(
        mode=ReviewMode.STRICT,
        label="Strict",
        short_description="Evidence-backed contradictions, code-cycle issues, invalid references only.",
    ),
    ReviewMode.COMPREHENSIVE: ReviewModeProfile(
        mode=ReviewMode.COMPREHENSIVE,
        label="Comprehensive",
        short_description="Strict scope plus AEC constructability, coordination, and practical quality issues.",
    ),
    ReviewMode.SAFE_EDIT: ReviewModeProfile(
        mode=ReviewMode.SAFE_EDIT,
        label="Safe edit",
        short_description="Only findings with exact editable anchors and low-risk replacements.",
    ),
}

DEFAULT_REVIEW_MODE = ReviewMode.COMPREHENSIVE


def coerce_review_mode(value: str | ReviewMode | None) -> ReviewMode:
    """Best-effort coercion of GUI labels / env strings into ``ReviewMode``."""
    if value is None:
        return DEFAULT_REVIEW_MODE
    if isinstance(value, ReviewMode):
        return value
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_REVIEW_MODE
    # Accept enum values, labels, and a few common aliases.
    aliases: dict[str, ReviewMode] = {
        "strict": ReviewMode.STRICT,
        "comprehensive": ReviewMode.COMPREHENSIVE,
        "safe_edit": ReviewMode.SAFE_EDIT,
        "safe-edit": ReviewMode.SAFE_EDIT,
        "safe edit": ReviewMode.SAFE_EDIT,
        "edit": ReviewMode.SAFE_EDIT,
        "default": DEFAULT_REVIEW_MODE,
    }
    return aliases.get(text, DEFAULT_REVIEW_MODE)
