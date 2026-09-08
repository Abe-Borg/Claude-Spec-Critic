"""Headless spec discovery (``pipeline._get_spec_files``) is case-insensitive.

The GUI picker already lower-cases suffixes; this is the directory path the
headless driver and ``scripts/recover_batch.py`` take. A literal ``*.docx``
glob skipped ``SPEC.DOCX`` on Linux/macOS, so a resumed run could silently
drop a spec the original run reviewed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.orchestration.pipeline import _get_spec_files


def _touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"PK\x03\x04")
    return path


def test_upper_and_mixed_case_extensions_are_discovered(tmp_path):
    _touch(tmp_path, "b.docx")
    _touch(tmp_path, "SPEC.DOCX")
    _touch(tmp_path, "Mixed.Docx")
    _touch(tmp_path, "notes.txt")
    _touch(tmp_path, "~$lock.docx")
    (tmp_path / "folder.docx").mkdir()

    names = [p.name for p in _get_spec_files(tmp_path)]

    assert names == ["b.docx", "Mixed.Docx", "SPEC.DOCX"]


def test_sort_order_is_case_folded_then_exact_name(tmp_path):
    for name in ("delta.docx", "Alpha.docx", "charlie.DOCX", "alpha.docx", "Bravo.docx"):
        _touch(tmp_path, name)

    names = [p.name for p in _get_spec_files(tmp_path)]

    # Case-folded order first; ``Alpha`` before ``alpha`` on the exact-name
    # tie-break (deterministic on every platform).
    assert names == ["Alpha.docx", "alpha.docx", "Bravo.docx", "charlie.DOCX", "delta.docx"]
    assert names == [p.name for p in _get_spec_files(tmp_path)]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_entries_resolving_to_the_same_file_are_deduplicated(tmp_path):
    """A case-insensitive filesystem presents one file under both spellings;
    a symlink alias models that on POSIX. Exactly one entry survives — the
    first in sort order — so the dedup is deterministic too."""
    real = _touch(tmp_path, "spec.docx")
    alias = tmp_path / "SPEC.DOCX"
    try:
        os.symlink(real.name, alias)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")

    found = _get_spec_files(tmp_path)

    assert len(found) == 1
    assert found[0].name == "SPEC.DOCX"  # sorts before ``spec.docx``
    assert found[0].resolve() == real.resolve()


def test_empty_directory_yields_nothing(tmp_path):
    assert _get_spec_files(tmp_path) == []
