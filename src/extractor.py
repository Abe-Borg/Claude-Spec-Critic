"""
DOCX text extraction module.

Extracts text content from Word documents (.docx) for specification review.
Handles both paragraph text and table content, which is important because
MEP specifications frequently use tables for equipment schedules, pipe
sizing charts, and product data.

Design notes:
    - Only supports .docx (Office Open XML), not legacy .doc format
    - Preserves paragraph structure with double-newline separation
    - Tables are flattened to pipe-delimited rows (loses formatting but
      retains content for LLM analysis)
    - Does NOT extract headers/footers, comments, or tracked changes
    - Does NOT preserve formatting (bold, italic, etc.) — plain text only

Usage:
    from extractor import extract_text_from_docx, ExtractedSpec
    
    spec = extract_text_from_docx(Path("23 21 13 - Hydronic Piping.docx"))
    print(spec.filename)    # "23 21 13 - Hydronic Piping.docx"
    print(spec.word_count)  # 4523
    print(spec.content)     # Full text content
"""


from pathlib import Path
from dataclasses import dataclass
from docx import Document
from docx.opc.exceptions import PackageNotFoundError


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
    
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)
    
    # Also extract text from tables (specs often have tables)
    for table in doc.tables:
        for row in table.rows:
            row_text = []
            for cell in row.cells:
                cell_text = cell.text.strip()
                if cell_text:
                    row_text.append(cell_text)
            if row_text:
                paragraphs.append(" | ".join(row_text))
    
    content = "\n\n".join(paragraphs)
    word_count = len(content.split())
    
    return ExtractedSpec(
        filename=filepath.name,
        content=content,
        word_count=word_count
    )


def extract_multiple_specs(filepaths: list[Path]) -> list[ExtractedSpec]:
    """
    Extract text from multiple .docx files.
    
    Convenience wrapper around extract_text_from_docx for batch processing.
    Raises on first failure — does not continue past errors.
    
    Args:
        filepaths: List of paths to .docx files
        
    Returns:
        List of ExtractedSpec objects in same order as input paths
        
    Raises:
        FileNotFoundError: If any file doesn't exist
        ValueError: If any file is not a valid .docx
        
    Example:
        >>> paths = list(Path("specs").glob("*.docx"))
        >>> specs = extract_multiple_specs(paths)
        >>> total_words = sum(s.word_count for s in specs)
    """
    return [extract_text_from_docx(fp) for fp in filepaths]



