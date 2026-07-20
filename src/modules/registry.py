"""Module registry: the single source of truth for selectable modules.

Mirrors the ``AVAILABLE_CYCLES`` / ``DEFAULT_CYCLE`` pattern in
:mod:`src.core.code_cycles`, which is threaded end-to-end (GUI selection →
``BatchSubmission`` → the persisted pending-batch state → resume/recovery).
Module identity rides the same rails: :func:`get_module` is the one resolver,
and its unknown-id fallback to :data:`DEFAULT_MODULE` is what keeps legacy
resume files (written before ``module_id`` existed) loading cleanly.

The registry is validated at import (:func:`validate_module_registry`) so an
inconsistent module definition fails at startup, never mid-run.
"""
from __future__ import annotations

from .base import ReviewModule, validate_module_registry
from .california_k12_mep import CALIFORNIA_K12_MEP
from .datacenter_architectural import DATACENTER_ARCHITECTURAL
from .datacenter_fire import DATACENTER_FIRE

_ALL_MODULES: tuple[ReviewModule, ...] = (
    CALIFORNIA_K12_MEP,
    DATACENTER_FIRE,
    DATACENTER_ARCHITECTURAL,
)

validate_module_registry(_ALL_MODULES)

AVAILABLE_MODULES: dict[str, ReviewModule] = {
    module.module_id: module for module in _ALL_MODULES
}

DEFAULT_MODULE: ReviewModule = CALIFORNIA_K12_MEP

# Reverse index for the ``module_for_cycle`` bridge. Well-defined because
# ``validate_module_registry`` enforces registry-unique cycle labels.
_MODULES_BY_CYCLE_LABEL: dict[str, ReviewModule] = {
    module.cycle.label: module for module in _ALL_MODULES
}


def get_module(module_id: str | None) -> ReviewModule:
    """Resolve ``module_id`` to a :class:`ReviewModule`, defaulting safely.

    ``None`` / empty / unknown ids resolve to :data:`DEFAULT_MODULE` — the
    same degrade-to-default posture as ``AVAILABLE_CYCLES.get(label,
    DEFAULT_CYCLE)`` and the model-capability whitelist: a stale or missing
    identifier produces the default California behavior, never an error.
    """
    if not module_id:
        return DEFAULT_MODULE
    return AVAILABLE_MODULES.get(module_id.strip(), DEFAULT_MODULE)


def module_for_cycle(cycle) -> ReviewModule:
    """Resolve the module that owns ``cycle`` (by label), defaulting safely.

    The **bridge** for content layers that are still keyed by ``cycle=``
    (prompt builders in ``review/prompts.py``, ``cross_check``, and
    ``verification/verifier.py``): they resolve their module's prompt slots
    from the cycle they were handed, with zero signature churn. Sound
    because cycle labels are registry-unique (enforced at import). Unknown /
    label-less cycles degrade to :data:`DEFAULT_MODULE`, matching
    :func:`get_module`. The bridge retires when later phases thread
    ``module=`` through the content layers explicitly.
    """
    label = (getattr(cycle, "label", "") or "").strip()
    return _MODULES_BY_CYCLE_LABEL.get(label, DEFAULT_MODULE)
