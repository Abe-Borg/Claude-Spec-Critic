"""Code cycle definitions for California K-12 DSA projects.

This module is the single source of truth for *which* California code cycle a
review runs against. It is consumed in four places:

1. ``preprocessor.detect_stale_code_cycle_references`` reads the plain code-year
   fields (``cbc`` / ``asce7``) to flag stale references **with no API call**.
2. The reviewer system prompt (``review/prompts.py``) injects the cycle so the
   model knows the current editions and can flag superseded ones.
3. The verifier system prompt (``verification/verifier.py``) renders the pinned
   standards block and treats those editions as authoritative.
4. The verification cache key (``verification/verification_cache.py``) uses
   ``label`` as its first segment, so bumping the cycle invalidates old verdicts.

Why a curated table instead of letting the verifier web-search every edition:
California frequently adopts an edition that diverges from the "latest" national
one (e.g. NFPA 25 is the *2013 California Edition*, not the current 2023 NFPA 25).
A naive web search would flag a correct California reference as stale. The pinned
table encodes that jurisdiction-specific fact precisely.

Maintenance: every edition string should reflect the actual published California
adoption (CBC Ch. 35, CFC Ch. 80, CMC Ch. 18, and the Title 24 Part 6 standards
table). Each :class:`StandardEdition` carries a ``source`` noting where the
edition was confirmed; entries whose ``source`` begins with ``UNVERIFIED`` have
*not* been confirmed against the published code and should be checked before they
are relied on.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StandardEdition:
    """One referenced standard and the edition California adopted for a cycle.

    The wording is deliberately one-directional: California *adopts and amends*
    a national standard, never the reverse. ``edition_phrase`` renders as
    ``"2025, as amended by California"`` — i.e. the 2025 edition of the standard,
    as amended by California — so the relationship can't be misread.

    Attributes:
        name: The standard designation, e.g. ``"NFPA 13"`` or ``"ASHRAE 90.1"``.
        edition: The base edition California adopted, e.g. ``"2025"``. This is the
            edition of the *national* standard, not a California version number.
        ca_amended: True when California adopts the edition *with amendments*
            (State Fire Marshal / DGS amendments). Purely descriptive — it
            changes only the rendered phrasing, not any logic.
        note: A short edition descriptor that *is* shown to the model, e.g.
            ``"California Edition"`` for the NFPA 25 2013 California Edition.
            Leave empty for ordinary editions.
        source: Provenance for the maintainer. NOT rendered into any prompt.
            Document where the edition was confirmed; prefix with ``UNVERIFIED``
            when it has not been checked against the published code.
    """

    name: str
    edition: str
    ca_amended: bool = False
    note: str = ""
    source: str = ""

    @property
    def edition_phrase(self) -> str:
        """The edition descriptor without the standard name.

        Examples: ``"2025"``, ``"2025, as amended by California"``,
        ``"2013 California Edition"``.
        """
        if self.ca_amended and self.note:
            return f"{self.edition} {self.note}"
        if self.ca_amended:
            return f"{self.edition}, as amended by California"
        if self.note:
            return f"{self.edition} ({self.note})"
        return self.edition

    @property
    def description(self) -> str:
        """Full one-line description, e.g. ``"NFPA 13 2025, as amended by California"``."""
        return f"{self.name} {self.edition_phrase}"

    @property
    def is_verified(self) -> bool:
        """True when ``source`` documents a confirmed adoption (not ``UNVERIFIED``)."""
        return bool(self.source) and not self.source.upper().startswith("UNVERIFIED")


@dataclass(frozen=True)
class CodeCycle:
    """A California code cycle: the plain code years plus the pinned standards.

    The plain code-year fields (``cbc`` … ``asce7_previous``) are unambiguous and
    load-bearing for the deterministic stale-cycle detector and the cache key.
    The referenced standards (NFPA / ASHRAE / IAPMO / UL) live in ``standards``
    as an ordered tuple of :class:`StandardEdition` — a single collection rather
    than ~15 flat string fields, so the verifier prompt, the reviewer prompt, and
    the report methodology note can all render from one source instead of three
    hand-maintained copies.

    ``standards`` is a ``tuple`` (not ``list``) so the dataclass stays hashable
    under ``frozen=True``; :class:`StandardEdition` is likewise frozen.
    """

    label: str
    cbc: str
    cmc: str
    cpc: str
    energy_code: str
    calgreen: str
    asce7: str
    asce7_previous: str
    standards: tuple[StandardEdition, ...] = field(default_factory=tuple)

    def standard(self, name: str) -> StandardEdition | None:
        """Return the pinned :class:`StandardEdition` for ``name`` or ``None``."""
        for std in self.standards:
            if std.name == name:
                return std
        return None

    def edition_phrase(self, name: str) -> str:
        """Rendered edition for ``name`` (e.g. ``"2025, as amended by California"``).

        Returns ``""`` when the cycle does not pin that standard, so callers can
        fall back to a generic phrase like ``"current edition"``.
        """
        std = self.standard(name)
        return std.edition_phrase if std else ""

    def unverified_standards(self) -> tuple[StandardEdition, ...]:
        """Standards whose edition has not been confirmed against the published code."""
        return tuple(std for std in self.standards if not std.is_verified)

    def edition_summary_lines(self) -> list[str]:
        """One ``"- NFPA 13: 2025, as amended by California"`` bullet per standard.

        Emits a line for each pinned standard with a non-empty edition, in
        declaration order. This is the colon/bullet rendering used by the
        verifier prompt's "Pinned standards editions" block. Stable per cycle
        (no per-spec input), so it is safe inside a cached prefix.
        """
        return [
            f"- {std.name}: {std.edition_phrase}"
            for std in self.standards
            if std.edition
        ]

    def edition_inline_phrase(self) -> str:
        """Comma-joined ``"NFPA 13 2025, as amended by California, ASHRAE 15 2022"``.

        Renders every pinned standard with a non-empty edition as one inline
        phrase (each entry is :attr:`StandardEdition.description`). Used by the
        reviewer prompt, where editions are named inside running prose rather
        than a bullet list — the space form keeps ``"<name> <edition_phrase>"``
        intact. Returns ``""`` when the cycle pins no editions.
        """
        return ", ".join(
            std.description for std in self.standards if std.edition
        )


# ---------------------------------------------------------------------------
# California 2025 cycle (Title 24, effective January 1, 2026)
# ---------------------------------------------------------------------------
#
# Code years verified against the California Building Standards Commission 2025
# (2024 Triennial) adoption — CBC/CMC/CPC/Energy/CALGreen 2025, ASCE 7-22
# (CBC 2025 Ch. 16), prior cycle ASCE 7-16.
CALIFORNIA_2025 = CodeCycle(
    label="2025",
    cbc="2025",
    cmc="2025",
    cpc="2025",
    energy_code="2025",
    calgreen="2025",
    asce7="7-22",
    asce7_previous="7-16",
    standards=(
        # --- Fire protection ---------------------------------------------
        # Verified against the California Fire Code 2025, Chapter 80
        # (Referenced Standards) adoption table.
        StandardEdition(
            "NFPA 13", "2025", ca_amended=True,
            source="California Fire Code 2025, Ch. 80",
        ),
        StandardEdition(
            "NFPA 14", "2024",
            source="California Fire Code 2025, Ch. 80",
        ),
        StandardEdition(
            "NFPA 20", "2025", ca_amended=True,
            source="California Fire Code 2025, Ch. 80",
        ),
        StandardEdition(
            "NFPA 24", "2025",
            source="California Fire Code 2025, Ch. 80",
        ),
        StandardEdition(
            "NFPA 25", "2013", ca_amended=True, note="California Edition",
            source="California Fire Code 2025, Ch. 80 (NFPA 25-2011 as amended, published as the 2013 California Edition; Title 19 CCR Sec. 904)",
        ),
        StandardEdition(
            "NFPA 72", "2025", ca_amended=True,
            source="California Fire Code 2025, Ch. 80 / OSFM IB 26-002",
        ),
        # --- Mechanical / energy -----------------------------------------
        StandardEdition(
            "ASHRAE 15", "2022",
            source="California Mechanical Code 2025 (A2L refrigerant provisions)",
        ),
        StandardEdition(
            "ASHRAE 62.1", "2019",
            source=(
                "UNVERIFIED: the 2025 Title 24 Part 6 draft referenced 62.1-2019, "
                "but some sources cite 62.1-2022 as adopted. Confirm against the "
                "published Title 24 Part 6 referenced-standards table."
            ),
        ),
        StandardEdition(
            "ASHRAE 90.1", "2022",
            source=(
                "UNVERIFIED: multiple sources indicate the 2025 Title 24 Part 6 "
                "references 90.1-2019, not 90.1-2022. Confirm against the published "
                "Title 24 Part 6 referenced-standards table."
            ),
        ),
        # --- Plumbing ----------------------------------------------------
        StandardEdition(
            "IAPMO Uniform Plumbing TSC", "2024",
            source=(
                "UNVERIFIED: CPC 2025 incorporates UPC 2024; the IAPMO installation "
                "trade-standards companion edition was not independently confirmed."
            ),
        ),
        # --- UL listings -------------------------------------------------
        # Common fire / smoke / firestop listings referenced in M&P specs.
        # Legacy values retained; CBC 2025 Ch. 35 uses a different edition
        # notation (e.g. "UL 268A-09") and these were not confirmed there.
        StandardEdition("UL 300", "2005", note="revised",
                        source="UNVERIFIED: legacy value; confirm against CBC 2025 Ch. 35"),
        StandardEdition("UL 555", "2006", note="revised",
                        source="UNVERIFIED: legacy value; confirm against CBC 2025 Ch. 35"),
        StandardEdition("UL 555S", "2014", note="revised",
                        source="UNVERIFIED: legacy value; confirm against CBC 2025 Ch. 35"),
        StandardEdition("UL 268", "2016", note="revised",
                        source="UNVERIFIED: legacy value; confirm against CBC 2025 Ch. 35"),
        StandardEdition("UL 1479", "2015", note="revised",
                        source="UNVERIFIED: legacy value; confirm against CBC 2025 Ch. 35"),
    ),
)

AVAILABLE_CYCLES = {
    "2025": CALIFORNIA_2025,
}

DEFAULT_CYCLE = CALIFORNIA_2025
