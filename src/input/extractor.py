"""Text extraction module for Spec Critic (DOCX-only)."""

from pathlib import Path
from dataclasses import dataclass, field
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
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
    # Phase 3 / Step 3.1: per-run character offset + format signature
    # for span-aware formatting-loss detection. Each entry is
    # ``(start_offset, end_offset, signature_tuple)`` in *stripped-text*
    # coordinates (matching ``ParagraphMapping.text``). The locator's
    # downgrade pass walks this list to decide whether a partial
    # replacement actually crosses runs with distinct formatting (in
    # which case formatting is silently destroyed) or sits entirely
    # inside one uniformly-formatted region (in which case AUTO_SAFE is
    # preserved). ``None`` for non-paragraph mappings and for legacy
    # resume-state payloads that predate Step 3.1; downstream code
    # falls back to the coarser ``distinct_formatting_runs`` count when
    # the per-run map is missing.
    run_format_map: list[tuple[int, int, tuple]] | None = None
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
    # Chunk 10 / Trust Upgrade: warnings emitted during text extraction
    # that the report banner surfaces so reviewers can spot specs where
    # text content may not have been fully captured (drawing-heavy
    # documents, embedded objects, etc.). Empty list by default; populated
    # only when the extractor's heuristics fire. Listed per spec — the
    # run-diagnostics banner counts the number of specs with any warnings,
    # not the total warning count, so a single spec with multiple
    # warnings still counts as one affected file.
    extraction_warnings: list[str] = field(default_factory=list)


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


def _summarize_paragraph_formatting(
    paragraph: Paragraph,
) -> tuple[int, int, list[tuple[int, int, tuple]]]:
    """Return ``(run_count, distinct_formatting_runs, run_format_map)`` for a paragraph.

    Phase 4 (audit Section 8.5): callers downgrade auto-edit safety when a
    paragraph has multiple runs with distinct character formatting, because
    run-level replacement collapses non-matching formatting into the first
    run and silently destroys inline emphasis/font choices.

    Phase 3 / Step 3.1: the per-run offset map (third tuple element) is
    in *stripped-text* coordinates — the same coordinate space the
    locator's ``EditLocation.match_start`` / ``match_end`` use. The
    extractor stores ``para.text.strip()`` on each ``ParagraphMapping``,
    so an offset that's relative to ``para.text`` (the unstripped
    concatenation of run texts) would not align with what the locator
    sees. We shift offsets by the leading-whitespace prefix length and
    clamp the trailing edge to the stripped length so runs that span
    pure leading/trailing whitespace are dropped from the map.
    """
    runs = list(paragraph.runs)
    if not runs:
        return 0, 0, []
    raw_text = paragraph.text or ""
    if not raw_text:
        return 0, 0, []
    leading_ws = len(raw_text) - len(raw_text.lstrip())
    stripped_len = len(raw_text.strip())

    signatures: set[tuple] = set()
    non_empty = 0
    run_format_map: list[tuple[int, int, tuple]] = []
    cursor = 0
    for run in runs:
        text = run.text or ""
        if not text:
            continue
        run_start_raw = cursor
        run_end_raw = cursor + len(text)
        cursor = run_end_raw
        non_empty += 1
        font = run.font
        signature = (
            bool(run.bold),
            bool(run.italic),
            bool(run.underline),
            getattr(font, "name", None),
            float(font.size.pt) if getattr(font, "size", None) is not None else None,
            str(getattr(getattr(font, "color", None), "rgb", None) or ""),
        )
        signatures.add(signature)
        # Translate offsets into stripped-text coordinates. Runs that
        # fall entirely inside the stripped leading/trailing whitespace
        # are dropped from the map (they would produce a zero-width
        # entry that no downstream check can use).
        start_stripped = max(0, run_start_raw - leading_ws)
        end_stripped = min(stripped_len, run_end_raw - leading_ws)
        if end_stripped > start_stripped:
            run_format_map.append((start_stripped, end_stripped, signature))
    return non_empty, len(signatures), run_format_map


# Chunk 10 / Trust Upgrade: threshold above which a spec is flagged as
# drawing-heavy. The pipeline writes the raw count and the proportion into
# the warning message so reviewers can see why the spec was flagged
# (drawings, embedded pictures, OLE objects). 20% is conservative; a
# typical drawing-supplemented spec carries figures inline at ~10% of body
# elements. Above that proportion the assumption that text extraction
# captures the reviewable content stops holding and the warning prompts
# a manual visual check.
_CONTENT_LOSS_WARNING_THRESHOLD = 0.20


def _detect_content_loss_warning(body) -> str | None:
    """Return a content-loss warning string for ``body`` or ``None`` if clean.

    Chunk 10 / Trust Upgrade: counts how many direct children of
    ``<w:body>`` (paragraphs and tables) contain at least one descendant
    ``<w:drawing>``, ``<w:pict>``, or ``<w:object>`` element. When that
    proportion exceeds :data:`_CONTENT_LOSS_WARNING_THRESHOLD`, the spec
    is likely drawing-heavy and text-only extraction cannot capture the
    reviewable content. The returned string is appended to
    ``ExtractedSpec.extraction_warnings`` so the report banner surfaces a
    visible count (per-spec, not per-drawing) and the operator knows to
    verify the spec visually.

    The ``<w:sectPr>`` body child (section properties) is metadata and
    not counted as a content element. Returns ``None`` when there are no
    body children, no embedded objects, or the proportion is below the
    threshold so the caller can keep ``extraction_warnings`` empty for
    the common case.
    """
    drawing_qn = qn("w:drawing")
    pict_qn = qn("w:pict")
    object_qn = qn("w:object")
    sect_pr_qn = qn("w:sectPr")

    total_body_elements = 0
    non_text_elements = 0
    drawings = 0
    pictures = 0
    objects = 0
    for child in body:
        if child.tag == sect_pr_qn:
            continue
        total_body_elements += 1
        child_drawings = len(child.findall(".//" + drawing_qn))
        child_pictures = len(child.findall(".//" + pict_qn))
        child_objects = len(child.findall(".//" + object_qn))
        if child_drawings or child_pictures or child_objects:
            non_text_elements += 1
        drawings += child_drawings
        pictures += child_pictures
        objects += child_objects

    if total_body_elements == 0:
        return None
    if non_text_elements == 0:
        return None
    proportion = non_text_elements / total_body_elements
    if proportion <= _CONTENT_LOSS_WARNING_THRESHOLD:
        return None

    percent = round(proportion * 100)
    return (
        f"Spec contains {percent}% non-text elements "
        f"({drawings} drawings, {pictures} pictures, {objects} OLE objects). "
        "Some content may not have been extracted for review. Verify visually."
    )


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
                run_count, distinct_fmt, run_format_map = _summarize_paragraph_formatting(para)
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
                        run_format_map=run_format_map,
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

    # Chunk 10 / Trust Upgrade: scan the body for embedded drawings /
    # pictures / objects. When the proportion of non-text elements
    # exceeds the threshold, the spec is likely drawing-heavy and text-
    # only extraction may have missed reviewable content. The warning
    # rides on ``extraction_warnings`` so the run-diagnostics banner can
    # count affected specs and surface the count to the reviewer.
    extraction_warnings: list[str] = []
    content_loss_warning = _detect_content_loss_warning(doc.element.body)
    if content_loss_warning is not None:
        extraction_warnings.append(content_loss_warning)

    return ExtractedSpec(
        filename=filepath.name,
        content=content,
        word_count=len(content.split()),
        source_path=str(filepath),
        source_format="docx",
        paragraph_map=paragraph_map,
        document_id=_derive_document_id(filepath.name),
        extraction_warnings=extraction_warnings,
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
