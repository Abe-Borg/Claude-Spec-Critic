"""Operator-supplied drawing analysis: a text file merged into Project Context.

Spec Critic does not read construction drawings. The operator runs a separate
drawing-analyzer program over the drawing set and attaches that program's
**text output** here. The text is wrapped as the ``Construction Drawing
Digest`` attachment block (:data:`DIGEST_ATTACHMENT_LABEL`) and merged into
Project Context, so:

* every review, cross-check, and compliance call sees it as ordinary text
  (Project Context does not reach verification);
* the post-review drawing-impact synthesis finds it by that exact block
  (``drawing_impact.extract_drawing_digest``);
* pending-batch resume carries it in the persisted ``project_context`` with
  nothing extra to store.

No API call is made at attach time and no drawing file is ever uploaded. The
only cost is the tokens the text adds to later requests, which is why the
attach flow counts them with the local tokenizer and shows the count: in the
FILES panel readout, in the activity log, and in the refusal when the merge
would exceed ``PROJECT_CONTEXT_MAX_TOKENS``.

The block's first line names the analyzer output file it came from
(:data:`SOURCE_LINE_PREFIX`). That line is how the FILES-panel readout keeps
one row per attached file — and its live token count — in sync with whatever
the operator later edits or deletes in the Project Context textbox. The
readout is a pure function of the textbox contents; nothing else remembers
what was attached.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..core.tokenizer import count_tokens
from ..gui.context_attachment import escape_attachment_markers, wrap_attachment

# Attachment label for the merged block inside Project Context. The exact
# ``--- BEGIN ATTACHMENT: <label> ---`` marker gates the drawing-impact
# synthesis and drives the FILES-panel readout. A pending-batch record saved
# by an earlier build (whose block came from the retired vision digest)
# carries this same label, so a resumed run still gets its drawing-impact
# pass. Treat like a schema string.
DIGEST_ATTACHMENT_LABEL = "Construction Drawing Digest"

# First line inside the block: the analyzer output file the text came from.
# Parsed back out by :func:`drawing_analysis_blocks`; a block without it (a
# hand-pasted block, or one from an earlier build) is still a valid digest
# and reads out under :data:`UNNAMED_BLOCK_LABEL`.
SOURCE_LINE_PREFIX = "Drawing analysis file: "
UNNAMED_BLOCK_LABEL = "(drawing digest without a file name)"

# The analyzer's output is a text file and is read verbatim. Word / PDF
# reference material goes through Attach Files…, which extracts text.
DRAWING_ANALYSIS_EXTENSIONS = frozenset({".txt", ".md", ".json"})

# Refused before reading: Project Context is capped at 100k tokens (roughly
# 400 KB of English text), so a file this large can never be attached, and
# tokenizing it on the worker thread would only delay that answer.
MAX_DRAWING_ANALYSIS_BYTES = 8 * 1024 * 1024

_BEGIN = f"--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---"
_END = f"--- END ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---"
_BLOCK_RE = re.compile(re.escape(_BEGIN) + r"\n(.*?)\n" + re.escape(_END), re.S)


class DrawingAnalysisError(ValueError):
    """The analyzer output file could not be used; the message names why."""


@dataclass(frozen=True)
class DrawingAnalysis:
    """One analyzer output file, read and counted.

    ``tokens`` is the local tokenizer's count of ``text`` — the same count
    the Project Context label uses, an estimate rather than the provider's
    billed figure.
    """

    name: str
    text: str
    tokens: int


@dataclass(frozen=True)
class DrawingAnalysisBlock:
    """One digest block as it currently stands in Project Context."""

    name: str  # source file name, or "" when the block has no source line
    text: str  # block body without the source line

    @property
    def display_name(self) -> str:
        return self.name or UNNAMED_BLOCK_LABEL


def read_drawing_analysis(path: Path | str) -> DrawingAnalysis:
    """Read one analyzer output file verbatim and count its tokens.

    Raises :class:`DrawingAnalysisError` for a missing file, an unsupported
    extension, a file over :data:`MAX_DRAWING_ANALYSIS_BYTES`, an unreadable
    file, or one with no text. Undecodable bytes are replaced rather than
    refused (the extractor's rule for ``.md`` / ``.txt`` attachments): one
    stray byte never sinks the whole analysis.

    The one edit to the text: a line that would read as an attachment
    delimiter is escaped with a leading backslash
    (``context_attachment.escape_attachment_markers``), so the block cannot
    be closed early by its own content. ``text`` and ``tokens`` are the
    escaped form — exactly what lands in Project Context, so the count shown
    at attach time is the count the readout shows afterwards.
    """
    path = Path(path)
    name = path.name
    if not path.is_file():
        raise DrawingAnalysisError(f"{name}: file not found")
    ext = path.suffix.lower()
    if ext not in DRAWING_ANALYSIS_EXTENSIONS:
        supported = ", ".join(sorted(DRAWING_ANALYSIS_EXTENSIONS))
        raise DrawingAnalysisError(
            f"{name}: not a text analysis file (expected {supported})"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DrawingAnalysisError(f"{name}: could not read file — {exc}") from exc
    if size > MAX_DRAWING_ANALYSIS_BYTES:
        limit_mib = MAX_DRAWING_ANALYSIS_BYTES // (1024 * 1024)
        raise DrawingAnalysisError(
            f"{name}: {size / (1024 * 1024):,.1f} MiB is over the "
            f"{limit_mib} MiB limit for a drawing analysis file and could "
            "never fit Project Context"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise DrawingAnalysisError(f"{name}: could not read file — {exc}") from exc
    if not text:
        raise DrawingAnalysisError(f"{name}: the file has no text")
    text = escape_attachment_markers(text)
    return DrawingAnalysis(name=name, text=text, tokens=count_tokens(text))


def load_drawing_analyses(
    paths: Sequence[Path | str],
) -> tuple[list[DrawingAnalysis], list[str]]:
    """Read every path; return ``(analyses, per_file_errors)``.

    One bad file does not block the others. Errors use the same
    ``"{name}: {reason}"`` style as the context-attachment flow so the GUI can
    show both the same way.
    """
    analyses: list[DrawingAnalysis] = []
    errors: list[str] = []
    for path in paths:
        try:
            analyses.append(read_drawing_analysis(path))
        except DrawingAnalysisError as exc:
            errors.append(str(exc))
    return analyses, errors


def wrapped_drawing_analysis_block(analysis: DrawingAnalysis) -> str:
    """The analysis as a Project Context attachment block.

    First line names the source file; the body is the file's text as read
    (delimiter-shaped lines already escaped, and escaped again harmlessly by
    ``wrap_attachment``). The wrapper is the ordinary attachment delimiter so
    the downstream prompt-cache prefix sees the same shape as every other
    attachment.
    """
    return wrap_attachment(
        DIGEST_ATTACHMENT_LABEL, f"{SOURCE_LINE_PREFIX}{analysis.name}\n{analysis.text}"
    )


def drawing_analysis_blocks(context: str | None) -> list[DrawingAnalysisBlock]:
    """Every digest block in ``context``, in order, as it stands right now.

    Matches only the exact BEGIN/END marker lines, so a context *file* the
    operator happened to name like the digest (its label carries the file
    extension) is never mistaken for one. A block whose first line is the
    source line gets that file name; any other block reads out unnamed.
    Blank blocks are skipped: they carry nothing into a request.
    """
    if not context:
        return []
    blocks: list[DrawingAnalysisBlock] = []
    for match in _BLOCK_RE.finditer(context):
        body = match.group(1).strip()
        if not body:
            continue
        first, _, rest = body.partition("\n")
        if first.startswith(SOURCE_LINE_PREFIX):
            name = first[len(SOURCE_LINE_PREFIX):].strip()
            text = rest.strip()
            if not text:
                continue
            blocks.append(DrawingAnalysisBlock(name=name, text=text))
        else:
            blocks.append(DrawingAnalysisBlock(name="", text=body))
    return blocks
