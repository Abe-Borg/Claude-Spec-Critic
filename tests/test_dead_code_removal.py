"""B-35: symbols removed as dead code must not quietly return.

Each was verified unreferenced across ``src`` / ``tests`` / ``scripts`` /
``evals`` / ``packaging`` before removal; these pins keep the removal honest.
"""
from __future__ import annotations

import inspect

from src.core import api_config
from src.orchestration import batch_resume
from src.verification import source_grounding, verifier


def test_verifier_has_no_import_time_max_tokens_constant():
    assert not hasattr(verifier, "VERIFICATION_MAX_TOKENS")
    # The dynamic helper still owns the request shape (via verification_routing).
    assert hasattr(api_config, "verification_max_tokens")


def test_verifier_has_no_dead_continuation_total_counter():
    src = inspect.getsource(verifier)
    assert "continuation_total" not in src
    # The misleading "no-progress guard" description went with it.
    assert "without making\n    # progress goes terminal-unverified" not in src


def test_api_config_has_no_haiku_models_set():
    assert not hasattr(api_config, "HAIKU_MODELS")
    assert hasattr(api_config, "OPUS_MODELS")  # still drives the effort bump


def test_source_grounding_has_no_unused_boolean_wrapper():
    assert not hasattr(source_grounding, "is_grounded_against_search_results")
    assert "is_grounded_against_search_results" not in (source_grounding.__doc__ or "")
    assert "the single boolean used" not in (source_grounding.__doc__ or "")


def test_batch_resume_has_no_dispatching_saver():
    assert not hasattr(batch_resume, "save_pending_run")
    assert callable(batch_resume.save_pending_batch)
    assert callable(batch_resume.save_pending_program_run)
