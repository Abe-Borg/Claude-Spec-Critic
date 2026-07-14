"""Local-code compliance pass (WS-4 of the hyperscale data-center plan).

Evaluates whether a specification package correctly represents the
project-specific requirements the WS-3 research phase grounded — the
governing codes, local amendments, AHJ requirements, and client standards in
the :class:`~src.research.RequirementsProfile`. Modeled on the cross-check
pass: synchronous streaming call(s), chunked by the module's CSI chunk
groups when the corpus is oversize, producing ordinary :class:`Finding`
objects (stamped ``lc-`` ids by the pipeline) plus a coverage matrix.

Public surface:

- :func:`run_compliance_check` — single-pass evaluation.
- :func:`run_chunked_compliance_check` — the size-aware entry point the
  pipeline calls (delegates to the single pass when the corpus fits).
- :data:`COMPLIANCE_MODEL_DEFAULT` re-exported for convenience.
"""
from ..core.api_config import COMPLIANCE_MODEL_DEFAULT
from .compliance_checker import run_chunked_compliance_check, run_compliance_check

__all__ = [
    "COMPLIANCE_MODEL_DEFAULT",
    "run_chunked_compliance_check",
    "run_compliance_check",
]
