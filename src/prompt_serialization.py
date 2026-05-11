"""Safe serialization helpers for embedding untrusted content in prompts.

Spec content, finding text, project context, and other reviewer- or
document-supplied strings are wrapped in pseudo-XML blocks in our prompts
to make boundaries clear to the model. Without escaping, document text
containing literal ``</spec>``, attribute-breaking quotes, or instruction-
like strings could close or redefine those wrappers — a prompt-injection
boundary problem regardless of whether the document was hostile or just
contained the wrong characters by accident.

Chunk G chose "escaped text inside explicit content blocks" over full JSON
serialization because:

* it preserves the readable, model-trained prompt shape, so model behavior
  is unchanged for well-formed input;
* it keeps the stable instruction prefix separate from the variable
  document payload, so prompt-caching breakpoints stay where they are;
* it makes the boundary obvious in transcripts and debug output without
  needing a JSON pretty-printer.

The helpers in this module are the *single* place to:

* escape strings used as element content (:func:`escape_text`);
* escape strings used as attribute values (:func:`escape_attr`);
* render a ``<tag attr="...">body</tag>`` data block (:func:`wrap_data_block`);
* and render a multi-line data block where the body is a document body —
  with newlines preserved (:func:`wrap_document_block`).

The module also exposes constants for the tag names callers wrap content
in, so a consistency sweep (or a future schema change) only has to touch
this file.
"""

from __future__ import annotations

from typing import Iterable, Mapping


# Tag names used as wrappers across the codebase. Centralized so a future
# rename is one edit and so tests can assert "this content lands inside
# the canonical block" without hard-coding the string everywhere.
TAG_SPEC = "spec"
TAG_PROJECT_CONTEXT = "project_context"
TAG_CORPUS = "corpus"
TAG_ALREADY_IDENTIFIED = "already_identified"
TAG_PRIOR_FINDING = "prior"
TAG_FINDING = "finding"
TAG_FINDINGS = "findings"
TAG_CHUNK_FINDINGS = "chunk_findings"
TAG_CHUNK = "chunk"


def escape_text(value: str | None) -> str:
    """Escape a string for use as XML/HTML element content.

    Escapes the three reserved characters (``&``, ``<``, ``>``) so the
    body of a ``<tag>...</tag>`` block cannot prematurely close the tag,
    open a sibling tag, or be misread as an entity reference.

    ``None`` and empty strings round-trip as ``""`` so callers don't need
    to special-case missing values.
    """
    if not value:
        return ""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def escape_attr(value: str | None) -> str:
    """Escape a string for use as a double-quoted XML attribute value.

    In addition to the three reserved characters, also escapes ``"`` and
    ``'`` so the attribute-value quoting cannot be broken from inside the
    value. The previous ``_xml_escape`` helpers in :mod:`prompts`,
    :mod:`cross_checker`, and :mod:`verifier` only handled the element-
    content set; a filename like ``weird".docx`` would have broken the
    attribute quoting silently. This helper is the safe one for any
    ``key="..."`` slot.
    """
    if not value:
        return ""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&apos;")
    )


def _render_attrs(attrs: Mapping[str, str | None] | None) -> str:
    if not attrs:
        return ""
    parts: list[str] = []
    for key, value in attrs.items():
        # Skip blanks rather than emitting ``key=""`` everywhere — the
        # existing prompts treated missing attributes as absent and we
        # want the rendered shape to stay the same for callers that
        # provide every attribute today.
        if value is None:
            continue
        parts.append(f'{key}="{escape_attr(value)}"')
    if not parts:
        return ""
    return " " + " ".join(parts)


def wrap_data_block(
    tag: str,
    content: str | None,
    *,
    attrs: Mapping[str, str | None] | None = None,
) -> str:
    """Render ``<tag attr="...">escaped content</tag>`` as one line.

    Use this for short, single-line data fields (severity, file, section,
    inline issue summaries). For multi-line document bodies — anything
    that should keep its newline structure intact — use
    :func:`wrap_document_block` instead so the surrounding tags land on
    their own lines.
    """
    attr_str = _render_attrs(attrs)
    body = escape_text(content)
    return f"<{tag}{attr_str}>{body}</{tag}>"


def wrap_document_block(
    tag: str,
    content: str | None,
    *,
    attrs: Mapping[str, str | None] | None = None,
) -> str:
    """Render a multi-line document block with the wrapper tags on their own lines.

    Used for spec bodies and project-context blocks where preserving the
    interior newline layout matters for the model's readability. The body
    is escaped via :func:`escape_text` so any ``<spec>`` / ``</spec>`` (or
    similar) literals inside a document cannot prematurely close or
    redefine the wrapper.
    """
    attr_str = _render_attrs(attrs)
    body = escape_text(content or "")
    return f"<{tag}{attr_str}>\n{body}\n</{tag}>"


def render_blocks(blocks: Iterable[str]) -> str:
    """Join rendered blocks with newlines, dropping empties.

    Lets call sites compose multi-element prompts (e.g. a ``<corpus>`` that
    contains many ``<spec>`` children) without each one having to manage
    its own ``"\\n".join`` and falsy-check.
    """
    return "\n".join(block for block in blocks if block)
