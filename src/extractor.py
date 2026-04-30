"""Text extraction module for Spec Critic (DOCX-only)."""

from pathlib import Path
from dataclasses import dataclass, field
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

SUPPORTED_EXTENSIONS = {".docx"}


@dataclass
class RunFormatting:
    """Captures inline formatting for a single run, used to detect rich content."""
    start: int
    end: int
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_name: str | None = None
    font_size: float | None = None
    color: str | None = None

    def style_signature(self) -> tuple:
        return (
            bool(self.bold),
            bool(self.italic),
            bool(self.underline),
            self.font_name or "",
            float(self.font_size) if self.font_size is not None else 0.0,
            self.color or "",
        )


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
    runs: list[RunFormatting] = field(default_factory=list)
    has_rich_formatting: bool = False


def _color_hex(color_obj) -> str | None:
    if color_obj is None:
        return None
    rgb = getattr(color_obj, "rgb", None)
    if rgb is None:
        return None
    try:
        return str(rgb)
    except Exception:
        return None


def _capture_run_formatting(paragraph: Paragraph) -> tuple[list[RunFormatting], bool]:
    """Capture per-run formatting for a paragraph; flag whether formatting is rich.

    A paragraph counts as rich-formatted if it has more than one non-empty run with
    distinct style signatures (bold/italic/underline/font/color differ).
    """
    runs: list[RunFormatting] = []
    cursor = 0
    for run in paragraph.runs:
        text = run.text or ""
        font = getattr(run, "font", None)
        size = getattr(font, "size", None) if font is not None else None
        size_pt = float(size.pt) if size is not None and hasattr(size, "pt") else None
        runs.append(
            RunFormatting(
                start=cursor,
                end=cursor + len(text),
                bold=bool(run.bold) if run.bold is not None else False,
                italic=bool(run.italic) if run.italic is not None else False,
                underline=bool(run.underline) if run.underline is not None else False,
                font_name=(getattr(font, "name", None) if font is not None else None),
                font_size=size_pt,
                color=_color_hex(getattr(font, "color", None) if font is not None else None),
            )
        )
        cursor += len(text)

    non_empty = [r for r in runs if r.end > r.start]
    if len(non_empty) <= 1:
        return runs, False
    signatures = {r.style_signature() for r in non_empty}
    return runs, len(signatures) > 1


@dataclass
class ExtractedSpec:
    filename: str
    content: str
    word_count: int
    source_path: str = ""
    source_format: str = ""
    paragraph_map: list[ParagraphMapping] | None = None


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
                runs, is_rich = _capture_run_formatting(para)
                paragraphs.append(text)
                paragraph_map.append(
                    ParagraphMapping(
                        body_index=body_index,
                        element_type="paragraph",
                        text=text,
                        table_index=None,
                        row_index=None,
                        cell_index=None,
                        runs=runs,
                        has_rich_formatting=is_rich,
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
    assert "\n\n".join(m.text for m in paragraph_map) == content, "Paragraph map text does not reconstruct content"

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


def extract_multiple_specs(filepaths: list[Path], *, max_workers: int = 4) -> list[ExtractedSpec]:
    """Extract specs in parallel, preserving the input order in the output list."""
    if not filepaths:
        return []
    if max_workers <= 1 or len(filepaths) <= 1:
        return [extract_text(fp) for fp in filepaths]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(max_workers, len(filepaths))) as pool:
        return list(pool.map(extract_text, filepaths))
