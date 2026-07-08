"""Selectable domain modules ("California K-12 DSA M&P" is the first).

Public surface — import from this package, not the submodules:

- :class:`ReviewModule` — the frozen per-domain configuration object.
- :data:`AVAILABLE_MODULES` / :data:`DEFAULT_MODULE` / :func:`get_module`
  — the registry, mirroring the ``AVAILABLE_CYCLES`` pattern.
- :data:`CALIFORNIA_K12_MEP` — the California K-12 module instance.
"""
from .base import (
    DetectorVocabulary,
    ReviewModule,
    code_basis_format_kwargs,
    validate_module_registry,
)
from .california_k12_mep import CALIFORNIA_K12_MEP
from .registry import (
    AVAILABLE_MODULES,
    DEFAULT_MODULE,
    get_module,
    module_for_cycle,
)

__all__ = [
    "DetectorVocabulary",
    "ReviewModule",
    "code_basis_format_kwargs",
    "validate_module_registry",
    "CALIFORNIA_K12_MEP",
    "AVAILABLE_MODULES",
    "DEFAULT_MODULE",
    "get_module",
    "module_for_cycle",
]
