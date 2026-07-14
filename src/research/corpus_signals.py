"""Deterministic corpus-signal scrape that seeds the research fan-out.

Design D-3 [FT] of ``docs/hyperscale_datacenter_module_plan.md``: research is
profile-driven but corpus-informed. Before any research call fires, this
no-API scrape walks the already-extracted spec texts (extraction is
LRU-cached by mtime + content fingerprint, so scraping early costs nothing)
and collects four signal families the field trial showed live *only* in the
corpus:

(a) client/owner document names — matched from the module-data pattern set
    (``ReviewModule.corpus_signal_patterns``, e.g. "Basis of Design"
    headers, master-spec lineage/revision lines);
(b) named risk consultant / insurer mentions (engine-owned patterns);
(c) edition-governance sentences ("the {code}-referenced edition governs…",
    engine-owned ``edition…(govern|adopt|reference)`` family);
(d) standards cited with edition years ("NFPA 13-2022", "CSA B51, 2019").

The rendered block ships to every research call inside a
``<corpus_signals>`` data-not-instructions wrapper so the researcher
searches with the project's own vocabulary. An empty scrape produces no
block at all — research then runs profile-only, and the failure posture is
unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..core.tokenizer import count_tokens
from ..input.extractor import ExtractedSpec
from ..modules import ReviewModule

# ---------------------------------------------------------------------------
# Bounds. The block must stay a small, cheap seed — the plan caps it at
# roughly 2k tokens so it can ride every research user message without
# meaningfully moving the token needle.
# ---------------------------------------------------------------------------

# Hard token ceiling on the rendered block (plan §WS-3 2b: "~2k tokens").
CORPUS_SIGNALS_MAX_TOKENS = 2_000

# Per-category item cap. Applied at scrape time so one pathological spec
# (say, a standards index page) cannot crowd out the other families.
_MAX_ITEMS_PER_CATEGORY = 12

# Per-item character cap. Signals are sentence-scale evidence, not payloads.
_MAX_ITEM_CHARS = 240


# ---------------------------------------------------------------------------
# Engine-owned patterns (families b–d). The module owns only family (a).
# ---------------------------------------------------------------------------

# (b) Named risk consultant / insurer mentions. The identity of who reviews
# risk decides how the client dimension must be framed (FM data sheets
# mandatory vs benchmark-only), and the field trial found it only in the
# corpus. Sentence-level capture around these anchors.
_CONSULTANT_INSURER_PATTERN = re.compile(
    r"(?:risk\s+consultant|risk\s+engineer(?:ing)?|insurer|insurance\s+"
    r"(?:carrier|underwriter|company)|underwriter|FM\s+Global|Factory\s+Mutual|"
    r"loss\s+prevention\s+consultant)",
    re.IGNORECASE,
)

# (c) Edition-governance sentences. The regex family from the plan:
# ``edition`` within a sentence that also carries a governance verb.
_EDITION_GOVERNANCE_PATTERN = re.compile(
    r"edition[^.;\n]{0,120}?\b(?:govern|adopt|referenc)\w*"
    r"|\b(?:govern|adopt|referenc)\w*[^.;\n]{0,120}?edition",
    re.IGNORECASE,
)

# (d) Standards cited WITH an edition year. Body abbreviation + designation
# + 4-digit year (either "NFPA 13-2022" or "NFPA 13, 2022 edition" shapes).
_STANDARD_WITH_EDITION_PATTERN = re.compile(
    r"\b(NFPA|CAN/ULC|ULC|UL|CSA|ASME|ASTM|ASHRAE|ANSI|IEEE|AWWA|NEMA|SMACNA|IAPMO|FM)\b"
    r"[\s-]*([A-Z]?\d[\dA-Za-z.\-/]*)"
    r"(?:[\s,–—-]|\()*((?:19|20)\d{2})\b",
)

# Sentence boundary for the sentence-scale families (b) and (c). Newlines
# terminate a sentence too — spec paragraphs are line-oriented.
_SENTENCE_SPLIT = re.compile(r"[.!?;\n]")


@dataclass
class CorpusSignals:
    """The scrape result: four signal families, each deduped and bounded."""

    document_names: list[str] = field(default_factory=list)
    consultant_insurer_mentions: list[str] = field(default_factory=list)
    edition_governance_sentences: list[str] = field(default_factory=list)
    standards_with_editions: list[str] = field(default_factory=list)

    @property
    def has_signals(self) -> bool:
        return bool(
            self.document_names
            or self.consultant_insurer_mentions
            or self.edition_governance_sentences
            or self.standards_with_editions
        )

    def render_block(self) -> str:
        """Deterministic plain-text body for the ``<corpus_signals>`` wrapper.

        Category headers are fixed; empty categories render ``(none
        detected)`` so the shape stays stable for the model. Returns ``""``
        when NO category found anything — callers then omit the wrapper
        entirely (research runs profile-only, per D-3).
        """
        if not self.has_signals:
            return ""

        def _section(title: str, items: list[str]) -> str:
            if not items:
                return f"{title}:\n(none detected)"
            return f"{title}:\n" + "\n".join(f"- {i}" for i in items)

        block = "\n\n".join(
            [
                _section(
                    "Client/owner documents named in the specifications",
                    self.document_names,
                ),
                _section(
                    "Risk consultant / insurer mentions",
                    self.consultant_insurer_mentions,
                ),
                _section(
                    "Edition-governance language",
                    self.edition_governance_sentences,
                ),
                _section(
                    "Standards cited with edition years",
                    self.standards_with_editions,
                ),
            ]
        )
        return _trim_block_to_token_cap(block)


def _trim_block_to_token_cap(block: str) -> str:
    """Drop trailing lines until the block fits the token ceiling.

    The per-category and per-item caps make an over-cap block rare; this is
    the deterministic backstop (whole lines from the end, never mid-line).
    """
    if count_tokens(block) <= CORPUS_SIGNALS_MAX_TOKENS:
        return block
    lines = block.splitlines()
    while len(lines) > 1 and count_tokens("\n".join(lines)) > CORPUS_SIGNALS_MAX_TOKENS:
        lines.pop()
    return "\n".join(lines)


def _clean_signal(text: str) -> str:
    """Collapse whitespace and cap length for one captured signal."""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) > _MAX_ITEM_CHARS:
        collapsed = collapsed[: _MAX_ITEM_CHARS - 1].rstrip() + "…"
    return collapsed


def _append_unique(bucket: list[str], item: str, seen: set[str]) -> None:
    if not item or len(bucket) >= _MAX_ITEMS_PER_CATEGORY:
        return
    key = item.casefold()
    if key in seen:
        return
    seen.add(key)
    bucket.append(item)


def _sentence_around(text: str, start: int, end: int) -> str:
    """Return the sentence containing ``text[start:end]``."""
    left = 0
    for m in _SENTENCE_SPLIT.finditer(text, 0, start):
        left = m.end()
    right_match = _SENTENCE_SPLIT.search(text, end)
    right = right_match.start() if right_match else len(text)
    return text[left:right]


def scrape_corpus_signals(
    specs: Iterable[ExtractedSpec],
    *,
    module: ReviewModule,
) -> CorpusSignals:
    """Scrape the four signal families from the extracted spec texts.

    Deterministic, no API, bounded output. The module contributes only the
    document-name patterns (family a); the other three families are
    engine-owned. Compiled defensively: a module pattern that fails to
    compile was already rejected at registration, so no re-validation here.
    """
    signals = CorpusSignals()
    seen: dict[str, set[str]] = {
        "documents": set(),
        "consultants": set(),
        "editions": set(),
        "standards": set(),
    }
    document_patterns = [
        re.compile(src, flags=re.IGNORECASE)
        for src in module.corpus_signal_patterns
    ]

    for spec in specs:
        content = spec.content or ""
        if not content:
            continue
        for pattern in document_patterns:
            for m in pattern.finditer(content):
                sentence = _clean_signal(_sentence_around(content, m.start(), m.end()))
                _append_unique(signals.document_names, sentence, seen["documents"])
        for m in _CONSULTANT_INSURER_PATTERN.finditer(content):
            sentence = _clean_signal(_sentence_around(content, m.start(), m.end()))
            _append_unique(
                signals.consultant_insurer_mentions, sentence, seen["consultants"]
            )
        for m in _EDITION_GOVERNANCE_PATTERN.finditer(content):
            sentence = _clean_signal(_sentence_around(content, m.start(), m.end()))
            _append_unique(
                signals.edition_governance_sentences, sentence, seen["editions"]
            )
        for m in _STANDARD_WITH_EDITION_PATTERN.finditer(content):
            # The greedy designation class can absorb the separator hyphen
            # in "NFPA 13-2022"; trailing punctuation is never part of a
            # designation, so strip it.
            body, designation, year = m.group(1), m.group(2).rstrip(".-/"), m.group(3)
            _append_unique(
                signals.standards_with_editions,
                f"{body} {designation} ({year})",
                seen["standards"],
            )
    return signals
