"""Core :class:`ReviewModule` type and registry validation.

A **module** is one reviewable domain configuration — "California K-12 DSA
mechanical/plumbing" is the first; a future "hyperscale data-center fire
suppression" is the motivating second. The module is deliberately a single
atomic selection (one frozen object picked from a registry), not a set of
independent runtime knobs, so incoherent combinations (one domain's severity
anchors with another domain's code basis) are unrepresentable.

Phase 1 scope: the module carries *identity* (``module_id`` + display
strings) and the *code basis* (the existing :class:`CodeCycle`, untouched).
Later phases move the remaining domain content onto this object — prompt
slots (persona, severity anchors, categories, few-shot examples), the
deterministic-detector vocabulary, the verification-profile keywords and
source tiers, and the cross-check chunk map. Engine code holds the behavior;
modules hold the domain data.

Invariants:

- ``module_id`` is the stable registry key. It is persisted into the
  pending-batch resume state and stamped into trace run metadata, so treat a
  rename like a schema change (legacy ids must keep resolving).
- Cycle labels are **globally unique across modules** (enforced by
  :func:`validate_module_registry`). The verification cache keys its entries
  by cycle label, so uniqueness is what keeps two modules' cached verdicts
  from colliding without changing the cache key shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..core.code_cycles import CodeCycle


@dataclass(frozen=True)
class ReviewModule:
    """One reviewable domain configuration.

    Attributes:
        module_id: Stable registry key (e.g. ``"california_k12_mep"``).
            Persisted into resume state and trace metadata — never rendered
            into a prompt.
        display_name: Human-readable name for GUI / report surfaces.
        description: One-line summary for GUI / About surfaces.
        cycle: The code basis this module reviews against. Phase 3
            generalizes :class:`CodeCycle` beyond its California-shaped
            fields; until then the module simply carries the existing object.
    """

    module_id: str
    display_name: str
    description: str
    cycle: CodeCycle


def validate_module_registry(modules: Iterable[ReviewModule]) -> None:
    """Fail fast (``ValueError``) on an inconsistent module registry.

    Runs at import time in :mod:`registry` so a bad module definition breaks
    app startup, not a batch three hours in. Checks:

    - every ``module_id`` / ``display_name`` is non-empty and stripped;
    - ``module_id`` values are unique;
    - every module pins a cycle with a non-empty label;
    - cycle labels are unique across modules (the verification-cache
      namespace rule — see the module docstring).
    """
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    for module in modules:
        if not module.module_id or module.module_id != module.module_id.strip():
            raise ValueError(
                f"ReviewModule has an empty or unstripped module_id: {module.module_id!r}"
            )
        if not module.display_name or not module.display_name.strip():
            raise ValueError(
                f"ReviewModule {module.module_id!r} has an empty display_name"
            )
        if module.module_id in seen_ids:
            raise ValueError(f"Duplicate module_id in registry: {module.module_id!r}")
        seen_ids.add(module.module_id)

        label = (module.cycle.label or "").strip() if module.cycle else ""
        if not label:
            raise ValueError(
                f"ReviewModule {module.module_id!r} pins no code cycle label"
            )
        if label in seen_labels:
            raise ValueError(
                f"Duplicate cycle label {label!r} across modules — cycle labels "
                "namespace the verification cache and must be registry-unique"
            )
        seen_labels.add(label)
