"""DOCX text extraction module."""
import re
from pathlib import Path
from dataclasses import dataclass
from docx import Document
from docx.opc.exceptions import PackageNotFoundError


@dataclass
class ExtractedSpec:
    """Container for extracted specification content."""
    filename: str
    content: str
    word_count: int
    
    
def extract_text_from_docx(filepath: Path) -> ExtractedSpec:
    """
    Extract text content from a .docx file.
    
    Preserves paragraph structure and attempts to maintain
    CSI section organization (Part 1, Part 2, Part 3).
    
    Args:
        filepath: Path to the .docx file
        
    Returns:
        ExtractedSpec with filename, content, and word count
        
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a valid .docx
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
    
    Args:
        filepaths: List of paths to .docx files
        
    Returns:
        List of ExtractedSpec objects
    """
    return [extract_text_from_docx(fp) for fp in filepaths]


def combine_specs_for_review(specs: list[ExtractedSpec]) -> str:
    """
    Combine multiple extracted specs into a single string for API submission.
    
    Args:
        specs: List of ExtractedSpec objects
        
    Returns:
        Combined string with clear file delimiters
    """
    sections = []
    for spec in specs:
        sections.append(f"=== FILE: {spec.filename} ===\n{spec.content}")
    
    return "\n\n".join(sections)
