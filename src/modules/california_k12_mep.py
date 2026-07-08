"""The California K-12 DSA mechanical/plumbing module.

The original — and for now only — domain configuration: mechanical and
plumbing specs for California K-12 education facilities under DSA
jurisdiction, reviewed against the California 2025 code cycle
(:data:`src.core.code_cycles.CALIFORNIA_2025`).

Phase 1 carries identity + the code cycle only. The domain content that
still lives hardcoded in the engine (reviewer/cross-check/verifier prompt
framing, detector vocabulary, profile keywords, chunk map) moves onto this
object in later extraction phases; the goldens in
``tests/test_golden_domain_surfaces.py`` pin that content byte-exactly
through the move.
"""
from __future__ import annotations

from ..core.code_cycles import CALIFORNIA_2025
from .base import ReviewModule

CALIFORNIA_K12_MEP = ReviewModule(
    module_id="california_k12_mep",
    display_name="California K-12 (DSA) — Mechanical & Plumbing",
    description=(
        "Mechanical and plumbing specs for California K-12 education "
        "facilities under DSA jurisdiction (California 2025 code cycle)."
    ),
    cycle=CALIFORNIA_2025,
)
