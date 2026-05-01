"""Backward-compatible verification config.

Phase 2 moved the canonical configuration to :mod:`api_config`. This module
re-exports the names other modules (and tests) imported historically so the
rename does not require touching every call site.

New code should prefer :mod:`api_config` directly.
"""
from __future__ import annotations

from .api_config import (
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_OUTPUT_BETA,
    VERIFICATION_MODEL_DEFAULT as VERIFICATION_MODEL,
    WEB_SEARCH_TOOL,
    verification_max_tokens,
)

# Verification max_tokens stays a constant for callers that read it directly
# (verifier.py, batch.py). The cap now flows from the dynamic helper in
# api_config so changes propagate everywhere.
VERIFICATION_MAX_TOKENS = verification_max_tokens()

__all__ = [
    "BATCH_MAX_OUTPUT_TOKENS",
    "BATCH_OUTPUT_BETA",
    "VERIFICATION_MAX_TOKENS",
    "VERIFICATION_MODEL",
    "WEB_SEARCH_TOOL",
]
