"""The release artifact must describe exactly the version being published."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src import __version__

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "make_release_notes", _ROOT / "packaging/windows/make_release_notes.py"
)
_NOTES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_NOTES)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_notes_keep_subheadings_and_exclude_other_releases(newline: str) -> None:
    readme = newline.join([
        "### Unreleased", "Future change.", "### v3.11.0", "Current change.",
        "#### Compatibility", "Compatible.", "### v3.10.0", "Previous change.",
    ])
    notes = _NOTES.release_notes(readme, "3.11.0")
    assert "Current change." in notes
    assert "#### Compatibility" in notes
    assert "Compatible." in notes
    assert "Future change." not in notes
    assert "Previous change." not in notes
    assert "SpecCriticSetup.exe" in notes


def test_release_candidate_does_not_match_final_version() -> None:
    notes = _NOTES.release_notes(
        "### v3.11.0rc1\nCandidate.\n### v3.11.0\nFinal.\n", "3.11.0rc1"
    )
    assert "Candidate." in notes
    assert "Final." not in notes


@pytest.mark.parametrize("readme", [
    "### v3.11.1\nWrong version.\n",
    "### v3.11.0\n\n### v3.10.0\nPrevious.\n",
    "### v3.11.0\nOne.\n### v3.11.0\nTwo.\n",
    "### v3.11.0\n  \n",
])
def test_missing_empty_or_ambiguous_notes_fail(readme: str) -> None:
    with pytest.raises(ValueError, match="one nonempty"):
        _NOTES.release_notes(readme, "3.11.0")


def test_current_version_notes_can_be_written(tmp_path: Path) -> None:
    out = tmp_path / "release-notes.md"
    assert _NOTES.main([
        "--version", __version__, "--readme", str(_ROOT / "README.md"),
        "--out", str(out),
    ]) == 0
    notes = out.read_text(encoding="utf-8")
    assert f"Spec Critic v{__version__}." in notes
    assert "### v3.10.0" not in notes


def test_invalid_version_fails() -> None:
    with pytest.raises(ValueError, match="Invalid release version"):
        _NOTES.release_notes("### v3x11x0\nWrong.\n", "3x11x0")
