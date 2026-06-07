"""Tests for ``drawing_export`` — serializing a drawing digest to a folder.

Fully hermetic: no tkinter, no PyMuPDF, no network. The context and its sheets
are duck-typed fakes exposing only the attributes ``drawing_export`` reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.gui import drawing_export as dx

SRC = "Weld_County_Mechanical_Permit_Set.pdf"
NOW = datetime(2026, 6, 7, 7, 2, 0)


@dataclass
class _Ref:
    source_name: str
    page_index: int
    page_count: int

    @property
    def display_label(self) -> str:
        return f"{self.source_name} (page {self.page_index + 1}/{self.page_count})"


@dataclass
class _Sheet:
    ref: _Ref
    text: str = ""
    error: str | None = None
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


@dataclass
class _Ctx:
    sheets: list
    synthesis_text: str = ""
    combined_text: str = ""
    file_count: int = 1
    errors: list = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def ok_sheet_count(self) -> int:
        return sum(1 for s in self.sheets if s.ok)

    @property
    def cached_sheet_count(self) -> int:
        return sum(1 for s in self.sheets if s.cached)


def _make_ctx() -> _Ctx:
    sheets = [
        _Sheet(_Ref(SRC, 0, 3), text="VAV-3 serves Rm 120", input_tokens=100, output_tokens=50),
        _Sheet(_Ref(SRC, 1, 3), text="WH-1 schedule transcribed", cached=True),
        _Sheet(_Ref(SRC, 2, 3), error="api_error: Internal Server Error"),
    ]
    return _Ctx(
        sheets=sheets,
        synthesis_text="# Overview\n\nSet-level reconciliation across sheets.",
        combined_text="# Drawing Set Context Digest\n\n## Sheet 1/3\nVAV-3 serves Rm 120",
        file_count=1,
        errors=["Weld_County_Mechanical_Permit_Set.pdf (page 3/3): api_error: Internal Server Error"],
        total_input_tokens=100,
        total_output_tokens=50,
    )


# --------------------------------------------------------------------------- #
# export_folder_name
# --------------------------------------------------------------------------- #


def test_export_folder_name_uses_first_stem_and_timestamp():
    assert (
        dx.export_folder_name([SRC], now=NOW)
        == "Weld_County_Mechanical_Permit_Set_drawings_2026-06-07_070200"
    )


def test_export_folder_name_no_sources_falls_back():
    assert dx.export_folder_name([], now=NOW) == "drawings_2026-06-07_070200"


def test_export_folder_name_is_filesystem_safe():
    name = dx.export_folder_name(["M&P / set: rev#2.pdf"], now=NOW)
    assert "/" not in name and ":" not in name and "&" not in name and "#" not in name


# --------------------------------------------------------------------------- #
# build_export_documents
# --------------------------------------------------------------------------- #


def test_build_export_documents_order_and_filenames():
    docs = dx.build_export_documents(_make_ctx(), source_names=[SRC], now=NOW)
    names = [n for n, _ in docs]

    assert names[0] == "00_index.md"
    assert names[1] == "00_synthesis.md"
    assert names[-1] == "combined.md"
    middle = names[2:-1]
    assert len(middle) == 3
    # Per-sheet files are in page order with a global NN prefix and p<page> suffix.
    assert middle[0].startswith("01_") and middle[0].endswith("_p1.md")
    assert middle[1].startswith("02_") and middle[1].endswith("_p2.md")
    assert middle[2].startswith("03_") and middle[2].endswith("_p3.md")
    # Filenames are unique.
    assert len(set(names)) == len(names)


def test_build_export_documents_per_sheet_bodies():
    docs = dict(dx.build_export_documents(_make_ctx(), source_names=[SRC], now=NOW))

    ok = docs["01_Weld_County_Mechanical_Permit_Set_p1.md"]
    assert "VAV-3 serves Rm 120" in ok
    assert "**Status:** OK" in ok
    assert "100 in / 50 out" in ok

    cached = docs["02_Weld_County_Mechanical_Permit_Set_p2.md"]
    assert "served from cache" in cached
    assert "WH-1 schedule transcribed" in cached

    failed = docs["03_Weld_County_Mechanical_Permit_Set_p3.md"]
    assert "FAILED" in failed
    assert "api_error: Internal Server Error" in failed  # error becomes the body


def test_build_export_documents_synthesis_and_combined():
    docs = dict(dx.build_export_documents(_make_ctx(), source_names=[SRC], now=NOW))
    assert "Set-level reconciliation across sheets." in docs["00_synthesis.md"]
    assert "Drawing Set Context Digest" in docs["combined.md"]


def test_build_export_documents_synthesis_fallback_when_absent():
    ctx = _make_ctx()
    ctx.synthesis_text = ""
    docs = dict(dx.build_export_documents(ctx, source_names=[SRC], now=NOW))
    assert "No cross-sheet synthesis was produced" in docs["00_synthesis.md"]


def test_index_lists_counts_errors_and_files():
    docs = dict(dx.build_export_documents(_make_ctx(), source_names=[SRC], now=NOW))
    index = docs["00_index.md"]
    assert SRC in index
    assert "2/3" in index  # ok/total (1 cached counts as ok)
    assert "## Errors" in index
    assert "api_error: Internal Server Error" in index
    assert "combined.md" in index and "00_synthesis.md" in index


# --------------------------------------------------------------------------- #
# write_drawing_export
# --------------------------------------------------------------------------- #


def test_write_drawing_export_creates_folder_and_all_files(tmp_path):
    folder = dx.write_drawing_export(_make_ctx(), tmp_path, source_names=[SRC], now=NOW)

    assert folder.parent == tmp_path
    assert folder.name == "Weld_County_Mechanical_Permit_Set_drawings_2026-06-07_070200"
    written = sorted(p.name for p in folder.iterdir())
    assert written == sorted(
        [
            "00_index.md",
            "00_synthesis.md",
            "01_Weld_County_Mechanical_Permit_Set_p1.md",
            "02_Weld_County_Mechanical_Permit_Set_p2.md",
            "03_Weld_County_Mechanical_Permit_Set_p3.md",
            "combined.md",
        ]
    )
    # A failed sheet still produced a real file carrying its error.
    assert "api_error" in (folder / "03_Weld_County_Mechanical_Permit_Set_p3.md").read_text(
        encoding="utf-8"
    )


def test_write_drawing_export_unique_on_collision(tmp_path):
    first = dx.write_drawing_export(_make_ctx(), tmp_path, source_names=[SRC], now=NOW)
    second = dx.write_drawing_export(_make_ctx(), tmp_path, source_names=[SRC], now=NOW)
    assert first != second
    assert second.name.endswith("_2")
    assert first.exists() and second.exists()
