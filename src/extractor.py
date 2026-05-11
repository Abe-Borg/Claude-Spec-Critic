"""Text extraction module for Spec Critic (DOCX-only)."""

from pathlib import Path
from dataclasses import dataclass
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

SUPPORTED_EXTENSIONS = {".docx"}

# Project-context attachments are reviewed as background reference material
# (not edited by the spec pipeline), so PDFs are accepted in addition to DOCX.
CONTEXT_ATTACHMENT_EXTENSIONS = {".docx", ".pdf"}


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
    # Chunk K1: stable, deterministic element identifier scoped to a single
    # extracted document. The format is human-readable so a finding that
    # cites it can be debugged at a glance: ``p<body_index>`` for body
    # paragraphs, ``t<table>r<row>`` for table-cell rows, ``s<n>h<i>`` /
    # ``s<n>f<i>`` for section header / footer paragraphs, and ``meta<n>``
    # for the synthetic header/footer delimiter. The id is stable within a
    # single extraction run; for cross-run stability the document_id of the
    # owning ``ExtractedSpec`` should also be checked. Empty string for
    # legacy mappings constructed by tests that predate Chunk K.
    element_id: str = ""
    # Section heading text the element belongs to (best-effort). Surfacing
    # this lets the locator disambiguate identical text in different
    # sections without re-scanning the paragraph map.
    section_id: str = ""


@dataclass
class ExtractedSpec:
    filename: str
    content: str
    word_count: int
    source_path: str = ""
    source_format: str = ""
    paragraph_map: list[ParagraphMapping] | None = None
    # Chunk K1: stable, human-debuggable document identifier. Defaults to
    # the filename without extension; when filenames could collide the
    # caller can override. Element ids are only unique inside a single
    # document, so the locator pairs ``(document_id, element_id)`` when it
    # disambiguates findings that cite an id.
    document_id: str = ""


def _derive_document_id(filename: str) -> str:
    """Return a stable, human-readable document id for ``filename``.

    Chunk K1 keeps ids debuggable: the filename without its extension is
    enough for a per-run locator and reads cleanly in logs. Callers that
    expect cross-run stability across renames should override
    ``ExtractedSpec.document_id`` themselves.
    """
    if not filename:
        return ""
    return Path(filename).stem or filename


def _is_heading_paragraph(text: str) -> bool:
    """Heuristic match for a CSI / DSA spec heading paragraph.

    Chunk K1 needs a cheap, deterministic section attribution so the
    paragraph map can carry a ``section_id`` without re-walking the doc.
    The locator already has a more elaborate header detector
    (``edit_locator._header_level``); we deliberately don't import it here
    to avoid a circular dependency, and the heuristic only has to be
    close enough that downstream prompts and reports can group paragraphs
    by section. False positives are harmless — they shift the section
    boundary by one paragraph.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 80:
        return False
    # "PART 1 GENERAL" / "SECTION 23 05 23" — explicit headings.
    upper = stripped.upper()
    if upper.startswith("PART ") or upper.startswith("SECTION "):
        return True
    # "1.01 SUMMARY" / "2.3.A …" — numbered CSI subheadings.
    first_token = stripped.split(maxsplit=1)[0]
    if first_token and first_token[0].isdigit() and any(
        ch == "." for ch in first_token
    ):
        return True
    return False


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
    # Chunk K1: track the most recently seen heading paragraph so each
    # element below it can carry a ``section_id``. Reset to empty when the
    # extractor crosses a top-level "PART ..." boundary so subsequent
    # subheadings nest under the right ancestor.
    current_section: str = ""

    for body_index, child in enumerate(doc.element.body):
        if child.tag.endswith("}p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if text:
                paragraphs.append(text)
                run_count, distinct_fmt = _summarize_paragraph_formatting(para)
                if _is_heading_paragraph(text):
                    current_section = text
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
                        element_id=f"p{body_index}",
                        section_id=current_section,
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
                            element_id=f"t{table_counter}r{row_index}",
                            section_id=current_section,
                        )
                    )
            table_counter += 1

    header_footer_entries: list[ParagraphMapping] = []
    for section_index, section in enumerate(doc.sections):
        for container_name, container in (("header", section.header), ("footer", section.footer)):
            for para_index, para in enumerate(container.paragraphs):
                text = para.text.strip()
                if not text:
                    continue
                prefixed = f"[{container_name.title()}] {text}"
                container_tag = "h" if container_name == "header" else "f"
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
                        element_id=f"s{section_index}{container_tag}{para_index}",
                        section_id="",
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
                element_id="meta:hf",
                section_id="",
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
        document_id=_derive_document_id(filepath.name),
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


def _extract_pdf_text(filepath: Path) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise ValueError(
            "PDF support requires the 'pypdf' package. Install with: pip install pypdf"
        ) from exc
    try:
        reader = PdfReader(str(filepath))
    except PdfReadError as exc:
        raise ValueError(f"Invalid or corrupted PDF: {filepath} — {exc}")
    except Exception as exc:
        raise ValueError(f"Could not read PDF: {filepath} — {exc}")
    if getattr(reader, "is_encrypted", False):
        raise ValueError(f"PDF is encrypted and cannot be read: {filepath}")
    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


def extract_context_text(filepath: Path) -> str:
    """Extract plain text from a Project Context attachment (.docx or .pdf).

    Returns a plain string suitable for splicing into the project_context
    prompt block. Unlike ``extract_text``, this does not build a paragraph
    map — the result is reference material, not an editable spec.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    ext = filepath.suffix.lower()
    if ext == ".docx":
        return extract_text_from_docx(filepath).content
    if ext == ".pdf":
        return _extract_pdf_text(filepath)
    raise ValueError(
        f"Unsupported context attachment format: '{ext}'. "
        f"Supported: {', '.join(sorted(CONTEXT_ATTACHMENT_EXTENSIONS))}"
    )
