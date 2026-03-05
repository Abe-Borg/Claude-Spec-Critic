"""
Text extraction module for Spec Critic.

Extracts text content from Word documents (.docx) and native PDF files for
specification review. DOCX extraction handles both paragraph text and table
content (tables flattened to pipe-delimited rows). PDF extraction uses
pymupdf's page text extraction.

Supported formats:
    - .docx (Office Open XML) via python-docx
    - .pdf (native/text-selectable) via pymupdf

The public API is format-agnostic: call extract_text() with any supported
file and get back an ExtractedSpec. The format-specific extractors are also
available for direct use.

Design notes:
    - Only native (text-selectable) PDFs are supported. Scanned/image-only
      PDFs will produce little or no text — a warning is logged.
    - Preserves paragraph structure with double-newline separation
    - DOCX tables are flattened to pipe-delimited rows (loses formatting but
      retains content for LLM analysis)
    - Does NOT extract headers/footers, comments, or tracked changes
    - Does NOT preserve formatting (bold, italic, etc.) — plain text only

v1.9.0 — Added PDF support via pymupdf and format-agnostic extract_text()
    dispatcher. DOCX extraction unchanged.

Usage:
    from extractor import extract_text, ExtractedSpec

    # Format-agnostic (recommended)
    spec = extract_text(Path("23 21 13 - Hydronic Piping.docx"))
    spec = extract_text(Path("23 21 13 - Hydronic Piping.pdf"))

    # Format-specific
    spec = extract_text_from_docx(Path("spec.docx"))
    spec = extract_text_from_pdf(Path("spec.pdf"))
"""


from pathlib import Path
from dataclasses import dataclass
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph


# Minimum words per page to consider a PDF page as having usable text.
# Pages below this threshold are likely scanned images.
_MIN_WORDS_PER_PAGE = 10

# Supported file extensions (lowercase, with dot)
SUPPORTED_EXTENSIONS = {".docx", ".pdf"}


@dataclass
class ExtractedSpec:
    """
    Container for extracted specification content.

    Attributes:
        filename: Original filename (e.g., "23 21 13 - Hydronic Piping.docx")
        content: Full extracted text with paragraphs separated by double newlines
        word_count: Approximate word count (split on whitespace)
    """
    filename: str
    content: str
    word_count: int


# ---------------------------------------------------------------------------
# DOCX extraction
# ---------------------------------------------------------------------------

def extract_text_from_docx(filepath: Path) -> ExtractedSpec:
    """
    Extract text content from a .docx file.

    Extracts all paragraph text and table cell contents. Paragraphs are
    joined with double newlines. Table rows are flattened to single lines
    with cells separated by " | ".

    Args:
        filepath: Path to the .docx file

    Returns:
        ExtractedSpec containing filename, full text content, and word count

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a .docx or is corrupted/invalid

    Example:
        >>> spec = extract_text_from_docx(Path("specs/23 05 00.docx"))
        >>> print(f"{spec.filename}: {spec.word_count} words")
        23 05 00.docx: 3842 words
    """
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    if filepath.suffix.lower() != '.docx':
        raise ValueError(f"Not a .docx file: {filepath}")

    try:
        doc = Document(filepath)
    except PackageNotFoundError:
        raise ValueError(f"Invalid or corrupted .docx file: {filepath}")
    except Exception as e:
        raise ValueError(f"Could not read .docx file: {filepath} — {e}")

    paragraphs = []
    for child in doc.element.body:
        if child.tag.endswith('}p'):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if text:
                paragraphs.append(text)
        elif child.tag.endswith('}tbl'):
            table = DocxTable(child, doc)
            for row in table.rows:
                row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_text:
                    paragraphs.append(" | ".join(row_text))

    content = "\n\n".join(paragraphs)
    word_count = len(content.split())

    return ExtractedSpec(
        filename=filepath.name,
        content=content,
        word_count=word_count
    )


# ---------------------------------------------------------------------------
# PDF extraction (v1.9.0)
# ---------------------------------------------------------------------------

def extract_text_from_pdf(filepath: Path) -> ExtractedSpec:
    """
    Extract text content from a native (text-selectable) PDF file.

    Uses pymupdf for text extraction with reading-order preservation.

    Only native PDFs are supported. If a PDF yields very few words
    relative to its page count, a warning is included at the top of
    the extracted content indicating the document may be scanned.

    Args:
        filepath: Path to the .pdf file

    Returns:
        ExtractedSpec containing filename, full text content, and word count

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a .pdf or cannot be opened

    Example:
        >>> spec = extract_text_from_pdf(Path("specs/23 05 00.pdf"))
        >>> print(f"{spec.filename}: {spec.word_count} words")
        23 05 00.pdf: 4120 words
    """
    import pymupdf

    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    if filepath.suffix.lower() != '.pdf':
        raise ValueError(f"Not a .pdf file: {filepath}")

    try:
        doc = pymupdf.open(str(filepath))
    except Exception as e:
        raise ValueError(f"Could not open PDF file: {filepath} — {e}")

    paragraphs: list[str] = []
    total_pages = len(doc)
    low_text_pages = 0

    try:
        for page in doc:
            page_text = page.get_text("text").strip()

            # Check if this page has enough text to be considered native
            page_words = len(page_text.split()) if page_text else 0
            if page_words < _MIN_WORDS_PER_PAGE:
                low_text_pages += 1

            if page_text:
                paragraphs.append(page_text)
    except Exception as e:
        raise ValueError(f"Error reading PDF content: {filepath} — {e}")
    finally:
        doc.close()

    # Build content
    content = "\n\n".join(paragraphs)

    # Warn if most pages had very little text (likely scanned)
    if total_pages > 0 and low_text_pages > (total_pages * 0.5):
        warning = (
            f"[WARNING: {low_text_pages} of {total_pages} pages in this PDF "
            f"yielded very little text. This document may be scanned or "
            f"image-based. Extraction quality may be poor — consider using "
            f"a text-selectable PDF or .docx version instead.]\n\n"
        )
        content = warning + content

    word_count = len(content.split())

    return ExtractedSpec(
        filename=filepath.name,
        content=content,
        word_count=word_count
    )


# ---------------------------------------------------------------------------
# Format-agnostic dispatcher (v1.9.0)
# ---------------------------------------------------------------------------

def extract_text(filepath: Path) -> ExtractedSpec:
    """
    Extract text from a .docx or .pdf file (format-agnostic dispatcher).

    Routes to the appropriate format-specific extractor based on file
    extension. This is the recommended public API for callers that
    don't need to care about the source format.

    Args:
        filepath: Path to a .docx or .pdf file

    Returns:
        ExtractedSpec containing filename, full text content, and word count

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file format is unsupported or file is corrupted

    Example:
        >>> spec = extract_text(Path("specs/23 05 00.docx"))
        >>> spec = extract_text(Path("specs/23 05 00.pdf"))
    """
    filepath = Path(filepath)
    ext = filepath.suffix.lower()

    if ext == ".docx":
        return extract_text_from_docx(filepath)
    elif ext == ".pdf":
        return extract_text_from_pdf(filepath)
    else:
        raise ValueError(
            f"Unsupported file format: '{ext}'. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )


def extract_multiple_specs(filepaths: list[Path]) -> list[ExtractedSpec]:
    """
    Extract text from multiple .docx and/or .pdf files.

    Convenience wrapper around extract_text() for batch processing.
    Raises on first failure — does not continue past errors.

    Args:
        filepaths: List of paths to .docx or .pdf files

    Returns:
        List of ExtractedSpec objects in same order as input paths

    Raises:
        FileNotFoundError: If any file doesn't exist
        ValueError: If any file is not a valid .docx or .pdf

    Example:
        >>> paths = list(Path("specs").glob("*.docx")) + list(Path("specs").glob("*.pdf"))
        >>> specs = extract_multiple_specs(paths)
        >>> total_words = sum(s.word_count for s in specs)
    """
    return [extract_text(fp) for fp in filepaths]