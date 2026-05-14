"""Safe serialization helpers for embedding untrusted content in prompts.

Chunk K2 adds an opt-in id-tagged document rendering so findings can cite a
stable :attr:`ParagraphMapping.element_id` alongside the exact quote. The
id-tagged path lives in this module so the wrapper escaping rules from
Chunk G stay in one place; the system-prompt prefix is unchanged byte-for-
byte, so prompt-caching breakpoints continue to land where they did. The
opt-in is exposed via :func:`element_ids_enabled` so a future regression
can be rolled back without redeploying — set
``SPEC_CRITIC_ELEMENT_IDS=0`` to revert to the legacy plain-body rendering.


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

import os
from typing import Iterable, Mapping, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .input.extractor import ParagraphMapping


# Canonical "disable" tokens for boolean env-var flags. Any of these (case-
# insensitive) flips the corresponding feature off; anything else — including
# an unset variable — leaves the default-enabled behavior in place.
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def _env_flag_disabled(name: str) -> bool:
    """Return True when the env var ``name`` is set to a recognized disable token."""
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _DISABLE_TOKENS


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
# Chunk K2: element-level wrappers used when the id-tagged rendering is on.
# The model receives one ``<para id="...">…</para>`` (or ``<row …>``) per
# extracted element so it can cite ``evidenceElementId`` precisely.
TAG_PARA = "para"
TAG_ROW = "row"
TAG_HEADING = "heading"
# Chunk D4.1: wrapper for the per-spec list of items the deterministic
# preprocessor already detected. The block sits at the *end* of the user
# message so the cached system prompt prefix is unchanged and the
# instruction-prefix invariant tested by ``TestPromptCacheBreakpointSafety``
# still holds for the spec wrapper.
TAG_PRE_DETECTED = "pre_detected"


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


# ---------------------------------------------------------------------------
# Chunk K2: id-tagged document rendering
# ---------------------------------------------------------------------------


def element_ids_enabled() -> bool:
    """Whether prompt builders should emit element ids alongside spec text.

    Enabled by default. Set ``SPEC_CRITIC_ELEMENT_IDS=0`` to revert to the
    legacy plain-body rendering. The rendering only changes the body of the
    ``<spec>`` block, not the surrounding instruction prefix — so prompt-
    cache breakpoints stay where they were either way.
    """
    return not _env_flag_disabled("SPEC_CRITIC_ELEMENT_IDS")


def _element_tag(mapping: "ParagraphMapping") -> str:
    """Pick the wrapper tag that matches an element's role.

    Headings get their own tag so the model can spot section boundaries
    without having to parse the body text. Table-cell rows are flattened
    into a single ``<row>`` per row (cells are joined with ``" | "`` in the
    extractor, so any finer-grained tagging would be misleading). Header
    / footer / meta entries pass through as ``<para>`` so the model still
    sees the marker text alongside an id, but they are clearly excluded
    from edit-eligible content by the surrounding text.
    """
    if mapping.element_type == "table_cell":
        return TAG_ROW
    # Best-effort heading detection: the extractor stamps section_id on
    # every paragraph, but only the heading paragraph's section_id equals
    # its own text. Using that equality (after a strip+casefold) keeps
    # the rule trivial and avoids re-importing _is_heading_paragraph here.
    if (
        mapping.element_type == "paragraph"
        and mapping.section_id
        and mapping.section_id.strip().casefold()
        == (mapping.text or "").strip().casefold()
    ):
        return TAG_HEADING
    return TAG_PARA


def render_spec_with_ids(
    spec_content: str,
    paragraph_map: Sequence["ParagraphMapping"] | None,
    *,
    filename: str | None = None,
) -> str:
    """Render an extracted spec as id-tagged elements inside ``<spec>``.

    Each element gets one wrapper line of the form
    ``<para id="p7" section="1.01 SUMMARY">…</para>`` (or ``<row …>`` /
    ``<heading …>``) so a finding can cite the id alongside the exact
    quoted text. When the paragraph map is missing — for example, when a
    legacy resume payload feeds a string body without a map — this falls
    back to :func:`wrap_document_block`. That keeps existing callers
    correct and avoids a hard dependency on the K1 metadata.

    Filenames flow through :func:`escape_attr` so any reserved character
    in a filename cannot break the opening tag.
    """
    attrs: dict[str, str | None] = {}
    if filename:
        attrs["filename"] = filename

    if not paragraph_map:
        return wrap_document_block(TAG_SPEC, spec_content, attrs=attrs)

    body_lines: list[str] = []
    for mapping in paragraph_map:
        eid = (getattr(mapping, "element_id", "") or "").strip()
        if not eid:
            # Mapping predates Chunk K1 — fall back to a plain ``<para>``
            # without an id so the model still sees the body text.
            body_lines.append(wrap_data_block(TAG_PARA, mapping.text))
            continue
        tag = _element_tag(mapping)
        attr_block: dict[str, str | None] = {"id": eid}
        section = (getattr(mapping, "section_id", "") or "").strip()
        # Don't repeat the heading text in its own ``section`` attribute —
        # that wastes tokens for no information gain.
        if section and tag != TAG_HEADING:
            attr_block["section"] = section
        body_lines.append(wrap_data_block(tag, mapping.text, attrs=attr_block))

    body = "\n".join(body_lines)
    attr_str = _render_attrs(attrs)
    return f"<{TAG_SPEC}{attr_str}>\n{body}\n</{TAG_SPEC}>"


# ---------------------------------------------------------------------------
# Chunk D4.1: pre-detected deterministic-alerts block
# ---------------------------------------------------------------------------


# Per-rule cap on how many example matches we surface inside the block. Three
# is enough to give the model a sense of what the deterministic detector
# found without exploding the input-token budget on, say, a 50-placeholder
# spec. The full alert set still lands in the final report's Alerts section.
_PRE_DETECTED_EXAMPLES_PER_RULE: int = 3
# Per-example match-text cap. Long placeholder bodies ("[INSERT lengthy
# editorial note here ...]") would dominate the block; truncating keeps
# the rule + matched-text pair short while still showing what was flagged.
_PRE_DETECTED_MATCH_PREVIEW_CHARS: int = 60


def pre_detected_alerts_enabled() -> bool:
    """Whether the prompt builder should inject the ``<pre_detected>`` block.

    Always True. Deterministic alerts are fed into the LLM context so the
    model knows what the local detector already found and does not waste
    output budget reporting duplicates.
    """
    return True


def _truncate_example(text: str, *, limit: int = _PRE_DETECTED_MATCH_PREVIEW_CHARS) -> str:
    """Trim a single example match so the block stays compact.

    Whitespace is collapsed so a multi-line placeholder body renders on one
    line; the trailing ellipsis is the standard "…" character (one codepoint,
    one token in most tokenizers) so the truncation marker does not balloon
    the per-example cost.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)] + "…"


def _alerts_for_spec(
    alerts: Iterable[Mapping[str, object]] | None,
    filename: str | None,
) -> list[Mapping[str, object]]:
    """Return only the alerts whose ``filename`` matches the spec under review.

    Callers may pass a single spec's alerts directly, in which case the
    filter is a no-op. The helper exists so the prompt builder cannot
    accidentally leak alerts from another spec when the caller hands it a
    cross-spec list (e.g. the flat alert lists already collected on
    ``PipelineResult``).
    """
    if not alerts:
        return []
    if not filename:
        return [a for a in alerts if a]
    target = filename.strip().lower()
    out: list[Mapping[str, object]] = []
    for a in alerts:
        if not a:
            continue
        af = (a.get("filename") if isinstance(a, Mapping) else "") or ""
        if not af or af.strip().lower() == target:
            out.append(a)
    return out


def render_pre_detected_block(
    alerts: Sequence[Mapping[str, object]] | None,
    *,
    filename: str | None = None,
) -> str:
    """Render a compact ``<pre_detected>`` block summarising deterministic alerts.

    Chunk D4.1: the block lists each detected ``deterministic_rule`` once
    with its count and up to ``_PRE_DETECTED_EXAMPLES_PER_RULE`` example
    matches, plus a one-line instruction telling the model not to surface
    the same items as new findings. Returns ``""`` when there are no
    alerts so the caller can append the block unconditionally and not
    perturb the message shape for clean specs.

    Both the example text and every rule id are routed through
    :func:`escape_text` so a hostile match payload (``LEED</pre_detected>``
    for example) cannot close the wrapper.
    """
    if not alerts:
        return ""
    filtered = _alerts_for_spec(alerts, filename)
    if not filtered:
        return ""

    # Preserve first-seen rule order so the block is deterministic — the
    # rule order is part of the prompt and shuffling it would silently
    # invalidate any prompt-level cache hits in callers that bypass the
    # phase-aware cache machinery.
    by_rule: dict[str, list[str]] = {}
    order: list[str] = []
    for alert in filtered:
        if not isinstance(alert, Mapping):
            continue
        rule = str(alert.get("deterministic_rule") or "").strip()
        if not rule:
            # No stable rule id (very old payload). Fall back to the
            # human-readable ``type`` so the block still says something
            # informative — but do not let an empty string collapse rules.
            rule = str(alert.get("type") or "other").strip() or "other"
        match_text = str(alert.get("match") or "").strip()
        if rule not in by_rule:
            order.append(rule)
            by_rule[rule] = []
        by_rule[rule].append(match_text)

    if not order:
        return ""

    lines = [
        "The following items have already been detected by deterministic local "
        "rules. Do not duplicate them as new findings — focus on issues beyond "
        "this list. The final report already records every item below."
    ]
    for rule in order:
        matches = by_rule[rule]
        examples = [
            _truncate_example(m)
            for m in matches[:_PRE_DETECTED_EXAMPLES_PER_RULE]
            if m
        ]
        # When matches are all empty (rule fired without a quotable span,
        # e.g. ``inconsistent_filename`` whose match is the filename), still
        # render the rule + count so the model knows the rule fired.
        examples_str = ", ".join(escape_text(e) for e in examples if e)
        suffix = f": {examples_str}" if examples_str else ""
        lines.append(f"- {escape_text(rule)} (count={len(matches)}){suffix}")

    body = "\n".join(lines)
    return f"<{TAG_PRE_DETECTED}>\n{body}\n</{TAG_PRE_DETECTED}>"
