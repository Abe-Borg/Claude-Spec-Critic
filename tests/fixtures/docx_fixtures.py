"""Tiny in-memory DOCX builders for edit-safety / locator tests.

Chunk A baseline: Chunk F (edit precondition offset safety) and later
edit-related work need cheap DOCX fixtures so they can exercise the real
``ExtractedSpec`` paragraph_map without parsing large real specs.

These helpers are intentionally minimal:
- ``make_paragraph_spec`` builds a single-section spec from a list of strings.
- ``make_table_spec`` builds a one-table spec with the given cell values.
- ``make_real_world_section_spec`` returns a spec that looks like a
  realistic CSI-section snippet (PART / 1.01 / paragraph), good enough
  for testing locator behavior under anchor matching.

Callers receive the on-disk ``Path`` so they can feed it to
``extract_text_from_docx`` and get a populated ``paragraph_map`` back.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document


def make_paragraph_spec(tmp_path: Path, paragraphs: list[str], *, filename: str = "spec.docx") -> Path:
    """Save a docx with one paragraph per element of ``paragraphs``."""
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    out = tmp_path / filename
    doc.save(out)
    return out


def make_table_spec(
    tmp_path: Path,
    rows: list[list[str]],
    *,
    filename: str = "table_spec.docx",
    leading_paragraph: str = "PART 2 PRODUCTS",
) -> Path:
    """Save a docx with a single table built from ``rows``."""
    doc = Document()
    doc.add_paragraph(leading_paragraph)
    if not rows:
        raise ValueError("rows must not be empty")
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    for r_idx, row in enumerate(rows):
        for c_idx in range(cols):
            cell_text = row[c_idx] if c_idx < len(row) else ""
            table.cell(r_idx, c_idx).text = cell_text
    out = tmp_path / filename
    doc.save(out)
    return out


def make_real_world_section_spec(
    tmp_path: Path,
    *,
    filename: str = "23 21 13 - Hydronic.docx",
    code_year: str = "2022",
) -> Path:
    """Return a small spec that mirrors common CSI-section structure."""
    paragraphs = [
        f"SECTION 23 21 13 - HYDRONIC PIPING",
        "PART 1 GENERAL",
        "1.01 SUMMARY",
        f"A. Comply with California Plumbing Code {code_year} requirements.",
        "1.02 REFERENCES",
        f"A. CBC {code_year} - California Building Code.",
        "PART 2 PRODUCTS",
        "2.01 GENERAL",
        "A. Provide hydronic piping as scheduled.",
        "PART 3 EXECUTION",
        "3.01 INSTALLATION",
        "A. Install per manufacturer's written instructions.",
    ]
    return make_paragraph_spec(tmp_path, paragraphs, filename=filename)
