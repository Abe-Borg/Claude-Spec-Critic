"""Requirements-research fan-out (WS-3 of the hyperscale data-center plan).

The engine half of the location/client-aware review pipeline: a deterministic
corpus-signal scrape plus a parallel per-dimension web-search fan-out that
builds a :class:`~src.research.requirements_research.RequirementsProfile`.
The *dimensions* (what to research, per-dimension budgets) are module data
(``ReviewModule.research_dimensions``); everything here is domain-neutral.

Public surface — import from this package, not the submodules:

- :func:`run_requirements_research` — the fan-out runner.
- :class:`RequirementsProfile` / :class:`ResearchItem` /
  :class:`DimensionStatus` — the structured result.
- :exc:`ResearchFanoutError` — raised when EVERY dimension fails (the run
  must abort before review submission; nothing has been billed).
- :func:`splice_profile_into_context` — merge the rendered profile into
  Project Context under the token cap.
- :func:`scrape_corpus_signals` / :class:`CorpusSignals` — the deterministic
  no-API pre-research scrape.
"""
from .corpus_signals import CorpusSignals, scrape_corpus_signals
from .requirements_research import (
    PROFILE_CATEGORY_SECTIONS,
    PROFILE_SECTION_ORDER,
    DimensionStatus,
    RequirementsProfile,
    ResearchFanoutError,
    ResearchItem,
    run_requirements_research,
    splice_profile_into_context,
)

__all__ = [
    "PROFILE_CATEGORY_SECTIONS",
    "PROFILE_SECTION_ORDER",
    "CorpusSignals",
    "DimensionStatus",
    "RequirementsProfile",
    "ResearchFanoutError",
    "ResearchItem",
    "run_requirements_research",
    "scrape_corpus_signals",
    "splice_profile_into_context",
]
