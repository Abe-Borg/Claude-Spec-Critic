"""Text extraction module for Spec Critic (DOCX-only)."""

from pathlib import Path
from dataclasses import dataclass
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

SUPPORTED_EXTENSIONS = {".docx"}


@dataclass
class ParagraphMapping:
    body_index: int
    element_type: str
    text: str
    table_index: int | None
    row_index: int | None
    cell_index: int | None
    section_index: int | None = None
    container_type: str | None = None
    # Phase 4 (audit Section 8.5): rich-formatting downgrade. ``run_count``
    # is the number of non-empty runs in the source paragraph;
    # ``distinct_formatting_runs`` is the count of distinct character-format
    # signatures across those runs (bold/italic/underline/font/size/color).
    # Both are 0 for non-paragraph mappings (table cells flatten multiple
    # paragraphs and runs; treat them via the table-cell caution path).
    run_count: int = 0
    distinct_formatting_runs: int = 0


@dataclass
class ExtractedSpec:
    filename: str
    content: str
    word_count: int
    source_path: str = ""
    source_format: str = ""
    paragraph_map: list[ParagraphMapping] | None = None


def _summarize_paragraph_formatting(paragraph: Paragraph) -> tuple[int, int]:
    """Return (run_count, distinct_formatting_runs) for a paragraph.

    Phase 4 (audit Section 8.5): callers downgrade auto-edit safety when a
    paragraph has multiple runs with distinct character formatting, because
    run-level replacement collapses non-matching formatting into the first
    run and silently destroys inline emphasis/font choices.
    """
    runs = list(paragraph.runs)
    if not runs:
        return 0, 0
    signatures: set[tuple] = set()
    non_empty = 0
    for run in runs:
        if not run.text:
            continue
        non_empty += 1
        font = run.font
        signatures.add(
            (
                bool(run.bold),
                bool(run.italic),
                bool(run.underline),
                getattr(font, "name", None),
                float(font.size.pt) if getattr(font, "size", None) is not None else None,
                str(getattr(getattr(font, "color", None), "rgb", None) or ""),
            )
        )
    return non_empty, len(signatures)


def extract_text_from_docx(filepath: Path) -> ExtractedSpec:
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    if filepath.suffix.lower() != ".docx":
        raise ValueError(f"Not a .docx file: {filepath}")
    try:
        doc = Document(filepath)
    except PackageNotFoundError:
        raise ValueError(f"Invalid or corrupted .docx file: {filepath}")
    except Exception as e:
        raise ValueError(f"Could not read .docx file: {filepath} — {e}")

    paragraphs: list[str] = []
    paragraph_map: list[ParagraphMapping] = []
    table_counter = 0

    for body_index, child in enumerate(doc.element.body):
        if child.tag.endswith("}p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if text:
                paragraphs.append(text)
                run_count, distinct_fmt = _summarize_paragraph_formatting(para)
                paragraph_map.append(
                    ParagraphMapping(
                        body_index=body_index,
                        element_type="paragraph",
                        text=text,
                        table_index=None,
                        row_index=None,
                        cell_index=None,
                        run_count=run_count,
                        distinct_formatting_runs=distinct_fmt,
                    )
                )
        elif child.tag.endswith("}tbl"):
            table = DocxTable(child, doc)
            for row_index, row in enumerate(table.rows):
                row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_text:
                    joined_text = " | ".join(row_text)
                    paragraphs.append(joined_text)
                    paragraph_map.append(
                        ParagraphMapping(
                            body_index=body_index,
                            element_type="table_cell",
                            text=joined_text,
                            table_index=table_counter,
                            row_index=row_index,
                            cell_index=None,
                        )
                    )
            table_counter += 1

    header_footer_entries: list[ParagraphMapping] = []
    for section_index, section in enumerate(doc.sections):
        for container_name, container in (("header", section.header), ("footer", section.footer)):
            for para in container.paragraphs:
                text = para.text.strip()
                if not text:
                    continue
                prefixed = f"[{container_name.title()}] {text}"
                header_footer_entries.append(
                    ParagraphMapping(
                        body_index=-1,
                        element_type=container_name,
                        text=prefixed,
                        table_index=None,
                        row_index=None,
                        cell_index=None,
                        section_index=section_index,
                        container_type=container_name,
                    )
                )

    if header_footer_entries:
        delimiter = "===== HEADER/FOOTER CONTENT ====="
        paragraph_map.append(
            ParagraphMapping(
                body_index=-1,
                element_type="meta",
                text=delimiter,
                table_index=None,
                row_index=None,
                cell_index=None,
                section_index=None,
                container_type="header_footer",
            )
        )
        paragraph_map.extend(header_footer_entries)
        paragraphs.append(delimiter)
        paragraphs.extend(entry.text for entry in header_footer_entries)

    content = "\n\n".join(paragraphs)
    reconstructed = "\n\n".join(m.text for m in paragraph_map)
    if reconstructed != content:
        # Controlled error preserves context (audit Issue 10). The raw assert
        # version was stripped under -O and produced an opaque AssertionError.
        raise ValueError(
            f"Paragraph map for '{filepath.name}' does not reconstruct extracted content "
            f"(map_chars={len(reconstructed)}, content_chars={len(content)})."
        )

    return ExtractedSpec(
        filename=filepath.name,
        content=content,
        word_count=len(content.split()),
        source_path=str(filepath),
        source_format="docx",
        paragraph_map=paragraph_map,
    )


def extract_text(filepath: Path) -> ExtractedSpec:
    filepath = Path(filepath)
    ext = filepath.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format: '{ext}'. Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    return extract_text_from_docx(filepath)


def extract_multiple_specs(
    filepaths: list[Path],
    *,
    max_workers: int | None = None,
) -> list[ExtractedSpec]:
    """Extract a list of specs in parallel.

    Phase 5.2 (audit Section 9.2): bounded thread pool for I/O-bound DOCX
    parsing. Result order is preserved to match ``filepaths`` so downstream
    deterministic ordering (filenames, dedup keys, request maps) does not
    change. ``max_workers=1`` (or a single file) runs sequentially.
    """
    if not filepaths:
        return []
    paths = [Path(fp) for fp in filepaths]
    if len(paths) == 1:
        return [extract_text(paths[0])]
    workers = max_workers if max_workers is not None else min(8, len(paths))
    workers = max(1, workers)
    if workers == 1:
        return [extract_text(p) for p in paths]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(extract_text, paths))
