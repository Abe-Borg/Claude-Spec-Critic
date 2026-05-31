"""Code cycle definitions for California K-12 DSA projects."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeCycle:
    """A California code cycle with edition references used in prompts.

    The pinned standards editions (NFPA, ASHRAE, UL, IAPMO) are part of
    the verifier and reviewer system prompts so the model verifies claims
    against the editions the California Building Standards Commission
    actually adopted for the cycle. The cycle label is part of the
    verification cache key, so a cycle bump naturally invalidates prior
    entries — operators do not need to clear the cache manually when
    moving from one cycle to the next.

    Edition strings should reflect the actual published adoption matrix
    (verify against the California Building Standards Commission's
    matrix before changing). Edition strings are free-form (e.g.,
    "2022 with California Amendments") so a single field can carry both
    the base edition and the amendment provenance.
    """

    label: str
    cbc: str
    cmc: str
    cpc: str
    energy_code: str
    calgreen: str
    asce7: str
    asce7_previous: str
    # NFPA editions adopted for this cycle. Verify each against the
    # California Building Standards Commission published matrix.
    nfpa13: str = ""
    nfpa14: str = ""
    nfpa20: str = ""
    nfpa24: str = ""
    nfpa25: str = ""
    nfpa72: str = ""
    # ASHRAE editions referenced by California Title 24 (Part 6 Energy /
    # Part 4 Mechanical) and CMC.
    ashrae_62_1: str = ""
    ashrae_90_1: str = ""
    ashrae_15: str = ""
    # IAPMO Uniform Plumbing trade-standard companion to the CPC.
    iapmo_tsc: str = ""
    # UL listing editions for the most common fire / smoke / fixture
    # listings referenced in M&P specs. Stored as an ordered tuple of
    # ``(standard_number, edition)`` pairs (rather than a dict) so the
    # dataclass remains hashable under ``frozen=True``.
    ul_listing_editions: tuple[tuple[str, str], ...] = ()


CALIFORNIA_2025 = CodeCycle(
    label="2025",
    cbc="2025",
    cmc="2025",
    cpc="2025",
    energy_code="2025",
    calgreen="2025",
    asce7="7-22",
    asce7_previous="7-16",
    # NFPA editions adopted by the California State Fire Marshal and
    # incorporated by reference in the 2025 cycle. Source: California
    # Building Standards Commission adoption matrix for the 2025
    # Triennial / Intervening cycle. Verify these strings against the
    # currently published matrix before relying on them; they are a
    # best-effort snapshot at the time the cycle was integrated.
    nfpa13="2025 with California Amendments",
    nfpa14="2019",
    nfpa20="2022 with California Amendments",
    nfpa24="2022",
    nfpa25="2020 with California Amendments",
    nfpa72="2025 with California Amendments",
    # ASHRAE editions referenced by Title 24 Part 6 (Energy) and CMC for
    # ventilation and refrigeration safety.
    ashrae_62_1="2019",
    ashrae_90_1="2022",
    ashrae_15="2022",
    # Uniform Plumbing Code trade-standards companion (IAPMO).
    iapmo_tsc="2024",
    # Common UL listing editions for fire dampers, smoke detectors,
    # and through-penetration firestop systems.
    ul_listing_editions=(
        ("UL 300", "2005 (revised)"),
        ("UL 555", "2006 (revised)"),
        ("UL 555S", "2014 (revised)"),
        ("UL 268", "2016 (revised)"),
        ("UL 1479", "2015 (revised)"),
    ),
)

AVAILABLE_CYCLES = {
    "2025": CALIFORNIA_2025,
}

DEFAULT_CYCLE = CALIFORNIA_2025
