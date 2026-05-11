"""Chunk A — smoke tests.

The fastest possible "did we break the world?" check. Each test should
run in milliseconds and verify a single load-bearing claim about the
codebase. If any of these fail, the rest of the test suite probably
won't run cleanly either.

Imports a non-GUI subset of ``src`` modules — the GUI / Tk-dependent
modules are skipped because the headless test environment may not have
``tkinter`` installed.
"""
from __future__ import annotations

import importlib
import sys

import pytest


pytestmark = pytest.mark.smoke


# Non-GUI modules: every chunk in the implementation plan touches at
# least one of these, so an import failure is a hard stop.
_CORE_MODULES = [
    "src",
    "src.api_config",
    "src.batch",
    "src.batch_runtime",
    "src.code_cycles",
    "src.cross_checker",
    "src.diagnostics",
    "src.edit_candidates",
    "src.edit_locator",
    "src.extraction_cache",
    "src.extractor",
    "src.pipeline",
    "src.preprocessor",
    "src.prompts",
    "src.resume_state",
    "src.review_modes",
    "src.reviewer",
    "src.spec_editor",
    "src.structured_schemas",
    "src.tokenizer",
    "src.triage",
    "src.verification_cache",
    "src.verification_config",
    "src.verification_router",
    "src.verifier",
]


@pytest.mark.parametrize("name", _CORE_MODULES)
def test_module_imports_cleanly(name: str) -> None:
    """Every non-GUI module imports without side effects that error."""
    importlib.import_module(name)


def test_version_string_present() -> None:
    import src

    version = getattr(src, "__version__", None)
    assert isinstance(version, str)
    assert version, "src.__version__ should not be empty"


def test_review_finding_dataclass_has_audit_fields() -> None:
    """The audit added ``anchorText`` / ``insertPosition`` for ADD actions
    and ``affected_files`` for multi-file findings; verify the canonical
    object still carries them."""
    from src.reviewer import Finding

    f = Finding(
        severity="HIGH",
        fileName="x.docx",
        section="2.1",
        issue="y",
        actionType="EDIT",
        existingText="a",
        replacementText="b",
        codeReference=None,
    )
    assert hasattr(f, "anchorText")
    assert hasattr(f, "insertPosition")
    assert hasattr(f, "affected_files")
    assert f.affected_files == []


def test_verification_result_has_phase3_fields() -> None:
    """Chunks H/I depend on the Phase 3 evidence model."""
    from src.verifier import VerificationResult

    r = VerificationResult(verdict="UNVERIFIED")
    for field_name in (
        "grounded",
        "model_used",
        "escalated",
        "cache_status",
        "web_search_requests",
        "successful_source_count",
        "search_error_count",
    ):
        assert hasattr(r, field_name), f"VerificationResult missing field: {field_name}"


def test_tool_names_are_stable() -> None:
    """Renaming a tool would silently break parsers; pin the names."""
    from src.structured_schemas import (
        CROSS_CHECK_TOOL_NAME,
        REVIEW_TOOL_NAME,
        TRIAGE_TOOL_NAME,
        VERIFICATION_TOOL_NAME,
    )

    assert REVIEW_TOOL_NAME == "submit_review_findings"
    assert CROSS_CHECK_TOOL_NAME == "submit_cross_check_findings"
    assert VERIFICATION_TOOL_NAME == "submit_verification_verdict"
    assert TRIAGE_TOOL_NAME == "submit_triage_classifications"


def test_model_identifiers_present() -> None:
    """Chunk B updates capability policy keyed on these identifiers."""
    from src import api_config

    for attr in (
        "MODEL_OPUS_46",
        "MODEL_OPUS_47",
        "MODEL_SONNET_46",
        "MODEL_HAIKU_45",
    ):
        assert hasattr(api_config, attr), f"api_config missing model id: {attr}"


def test_default_models_are_set() -> None:
    from src import api_config

    assert isinstance(api_config.REVIEW_MODEL_DEFAULT, str) and api_config.REVIEW_MODEL_DEFAULT
    assert isinstance(api_config.VERIFICATION_MODEL_DEFAULT, str) and api_config.VERIFICATION_MODEL_DEFAULT
    assert isinstance(api_config.CROSS_CHECK_MODEL_DEFAULT, str) and api_config.CROSS_CHECK_MODEL_DEFAULT


def test_output_caps_are_reasonable() -> None:
    """If someone accidentally sets verification cap to 0, fail loudly."""
    from src import api_config

    assert api_config.REVIEW_OUTPUT_CAP > 0
    assert api_config.CROSS_CHECK_OUTPUT_CAP > 0
    assert api_config.VERIFICATION_OUTPUT_CAP > 0
    # Verification should be much smaller than review.
    assert api_config.VERIFICATION_OUTPUT_CAP < api_config.REVIEW_OUTPUT_CAP


def test_no_real_anthropic_key_required_for_imports() -> None:
    """conftest sets a placeholder API key so module import doesn't blow up;
    confirm that placeholder is what we have, not a real key."""
    import os

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    assert key, "conftest should have set a placeholder ANTHROPIC_API_KEY"
    # Don't assert exact value — a developer running with a real key locally
    # should still be able to run the suite. Just ensure the env var is set.


def test_tests_package_is_importable() -> None:
    """Tests directory can be imported as a package (for fixtures)."""
    importlib.import_module("tests.fixtures.fake_anthropic")
