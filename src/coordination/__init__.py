"""Cross-chunk and cross-module coordination experiment (plan EX-06).

Default off (``SPEC_CRITIC_CROSS_COORDINATION``). Observation only: the pass
reads the specifications no cross-check request compared, pairs what look
like two incompatible statements of one requirement, and — in ``observe``
mode — asks the cross-check model about each pair with both passages. It
records what it saw in diagnostics and changes nothing else. See
``plans/experiments/EX-06-cross-coordination.md``.

- ``facts`` — the deterministic, source-anchored fact reader (stdlib only);
- ``candidates`` — which pairs of facts are candidates, and the bounds;
- ``adjudication`` — the one model request shape, and its validation;
- ``runner`` — the pass over a collected run, and its diagnostics record.
"""
from __future__ import annotations

from .candidates import (
    KIND_CROSS_CHUNK,
    KIND_CROSS_MODULE,
    SCOPE_MODULE,
    SCOPE_PROGRAM,
    Candidate,
    CandidateSelection,
    SpecUnit,
    chunk_groups_from,
    select_candidates,
)
from .facts import POLICY_VERSION, CoordinationFact, extract_facts
from .runner import (
    CoordinationResult,
    ModuleInput,
    module_input_from_result,
    record_coordination,
    run_coordination,
)

__all__ = [
    "Candidate",
    "CandidateSelection",
    "CoordinationFact",
    "CoordinationResult",
    "KIND_CROSS_CHUNK",
    "KIND_CROSS_MODULE",
    "ModuleInput",
    "POLICY_VERSION",
    "SCOPE_MODULE",
    "SCOPE_PROGRAM",
    "SpecUnit",
    "chunk_groups_from",
    "extract_facts",
    "module_input_from_result",
    "record_coordination",
    "run_coordination",
    "select_candidates",
]
