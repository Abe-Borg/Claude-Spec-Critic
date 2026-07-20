"""Jurisdiction-generic content shared by the hyperscale data-center modules.

The data-center modules (``datacenter_fire``, ``datacenter_architectural``)
review the same *kind* of project — hyperscale data centers in the US and
Canada under the I-codes as base model codes — through different discipline
lenses. The constants here are the facts that depend only on that shared
jurisdiction posture, never on the discipline:

- the I-code publishing cadence (which editions exist / are anticipated),
- the ASCE 7 edition recognition whitelist,
- the wrong-polity token rules whose suspiciousness is a pure function of
  the project country (``NEC`` is US law, ``ULC`` is Canadian, ...),
- the owner-document vocabulary the corpus-signal scrape looks for.

Keeping them in one file means a triennial I-code bump or a new polity rule
is one edit, and the two modules cannot drift apart. This is **module
data**, not engine code: nothing outside ``src/modules/`` imports it, it is
deliberately NOT exported from ``src.modules.__init__`` (private module,
leading underscore), and each module still assembles and registers its own
frozen tuples — registry validation runs per module, unchanged.

Discipline-flavored rules stay module-local by design: ``datacenter_fire``
keeps its DOT-cylinder and CRN pressure-vessel polity rules (fire-suppression
gas/nitrogen vessels) and its fire-specific corpus pattern; a module adds its
own local rules alongside the shared ones.
"""
from __future__ import annotations

from .base import PolityTokenRule

# ---------------------------------------------------------------------------
# I-code cycle vocabulary (deterministic stale/invalid-cycle detectors)
# ---------------------------------------------------------------------------
# Real, published I-code editions in the recent window the stale detector
# flags (a found year in this set that differs from the module cycle's
# primary year alerts). These are historical publishing facts — the tuples
# only ever GROW (append the next edition when ICC publishes it).
#
# NBC/NFC (Canadian) years are intentionally NOT added — one shared
# cycle-year set can't hold both I-code and NBC year families without the
# stale/invalid detectors misfiring (documented v1 limitation,
# ``hyperscale_datacenter_module_plan.md`` D-10); Canadian deterministic
# coverage comes from the profile-gated wrong-polity token rules below.
DC_ICODE_PLAUSIBLE_YEARS: tuple[str, ...] = (
    "2009", "2012", "2015", "2018", "2021", "2024",
)

# Every published cycle plus the next anticipated one (2027); a year/code
# citation outside this set is a typo or fabrication ("2019 IBC").
DC_ICODE_VALID_YEARS: tuple[str, ...] = (
    "2009", "2012", "2015", "2018", "2021", "2024", "2027",
)

# Real, published ASCE 7 editions (recognition whitelist for the dedicated
# ASCE 7 stale detector).
DC_ASCE7_PLAUSIBLE_EDITIONS: tuple[str, ...] = (
    "88", "93", "95", "98", "02", "05", "10", "16", "22",
)

# Long-form citations ("2015 International Building Code"); the year must be
# capture group 1 (engine contract for ``stale_cycle_extra_patterns``).
# Modules append their own discipline-relevant I-code long forms (e.g. the
# architectural module adds Energy Conservation / Existing Building).
DC_STALE_LONGFORM_BUILDING_FIRE: str = (
    r"\b(20\d{2})\s+International\s+(?:Building|Fire)\s+Code\b"
)


# ---------------------------------------------------------------------------
# Wrong-polity token rules (profile-gated deterministic detector, D-15)
# ---------------------------------------------------------------------------
# Each rule is a pure function of the run's project country. The engine
# compiles polity patterns with NO flags, so case-blind phrase families use
# scoped ``(?i:...)`` while acronyms stay case-sensitive. Notes render into
# the alert so the operator sees WHY the token is suspicious.
#
# Exported as individually named constants — NOT one grouped tuple — so each
# module can assemble its ``polity_suspect_tokens`` tuple in its own order,
# interleaving module-local rules (fire keeps DOT-cylinder and CRN) without
# reordering a shipped tuple.

# --- country=CA: flag US-only vocabulary on a Canadian project --------------
POLITY_CA_NEC = PolityTokenRule(
    country="CA",
    pattern=r"\bNFPA\s*70\b|\bNEC\b",
    note=(
        "NFPA 70 / NEC is the US National Electrical Code; Canadian "
        "electrical work is governed by CSA C22.1 (Canadian Electrical "
        "Code)."
    ),
)

POLITY_CA_OSHA = PolityTokenRule(
    country="CA",
    pattern=r"\bOSHA\b",
    note=(
        "OSHA is a US federal safety agency; Canadian occupational safety "
        "is provincially regulated."
    ),
)

POLITY_CA_LIFE_SAFETY_CODE = PolityTokenRule(
    country="CA",
    # Allow the hyphenated compound "life-safety code" as well as the
    # spaced form.
    pattern=r"(?i:\blife[- ]safety code\b)",
    note=(
        "The Life Safety Code (NFPA 101) is a US code; Canadian life "
        "safety is governed by the National / provincial Building Code."
    ),
)

POLITY_CA_UL_LISTED = PolityTokenRule(
    country="CA",
    # Case-insensitive on "listed" so "UL Listed" / "U.L. Listed" (the
    # common title-case forms) are caught — the engine compiles polity
    # patterns with NO flags, so the scoped ``(?i:…)`` is what makes it
    # case-blind. ``UL`` stays uppercase-required, and the word boundary
    # before ``U`` still excludes ``cULus``/``ULC`` (no boundary there).
    pattern=r"\bU\.?L\.?[- ](?i:listed)\b",
    note=(
        "A bare UL listing may not be recognized in Canada; "
        "fire-protection and electrical components generally require "
        "cULus or ULC listing."
    ),
)

POLITY_CA_MADE_IN_USA = PolityTokenRule(
    country="CA",
    pattern=r"(?i:\bmade in (?:the )?usa\b|\bdomestically made\b)",
    note=(
        "US-origin / domestic-sourcing language is a US procurement clause; "
        "on a Canadian project it may be non-compliant or tariff-exposed — "
        "revise to a listing/standard-based basis."
    ),
)

POLITY_CA_SEISMIC_NOTATION = PolityTokenRule(
    country="CA",
    # Match the ASCE 7 seismic *notation* (S_DS / S_D1 design spectral
    # accelerations, SDC) and the phrase — NOT bare "SDS", which collides
    # with the ubiquitous "Safety Data Sheets (SDS)" submittal requirement
    # and would fire a spurious seismic alert on every submittal section.
    pattern=r"\bS_DS\b|\bS_D1\b|\bSDC\b|(?i:\bseismic design category\b)",
    note=(
        "S_DS / S_D1 / SDC / Seismic Design Category are the ASCE 7 / IBC "
        "seismic parameters; Canadian projects use the NBC seismic-hazard "
        "framework instead."
    ),
)

POLITY_CA_IBC_IFC = PolityTokenRule(
    country="CA",
    pattern=r"\bIBC\b|\bIFC\b",
    note=(
        "This project's governing codes are the NBC/NFC family per the "
        "requirements profile; a bare IBC/IFC citation is likely a US "
        "master-spec remnant unless the profile confirms I-code adoption."
    ),
)

POLITY_CA_115V = PolityTokenRule(
    country="CA",
    pattern=r"\b115[- ]?V(?:AC)?\b",
    note=(
        "115 V is a US nominal-voltage convention; Canadian systems are "
        "specified at 120 / 208 / 347 / 600 V."
    ),
)

# --- country=US: flag Canada-only vocabulary on a US project ----------------
POLITY_US_NBC = PolityTokenRule(
    country="US",
    pattern=r"\bNBC\b|(?i:\bnational building code of canada\b)",
    note=(
        "The NBC / National Building Code of Canada is a Canadian model "
        "code; a US project is governed by the IBC/IFC family."
    ),
)

POLITY_US_ULC = PolityTokenRule(
    country="US",
    pattern=r"\bULC\b",
    note=(
        "A ULC (Underwriters Laboratories of Canada) listing is Canadian; "
        "a US project generally requires a UL or cULus listing."
    ),
)

POLITY_US_OREG = PolityTokenRule(
    country="US",
    pattern=r"O\. ?Reg\.",
    note=(
        "'O. Reg.' cites an Ontario regulation; a US project is not "
        "governed by Ontario law."
    ),
)

POLITY_US_CSA_C221 = PolityTokenRule(
    country="US",
    pattern=r"\bCSA\s*C22\.1\b",
    note=(
        "CSA C22.1 (Canadian Electrical Code) governs Canadian electrical "
        "work; a US project's electrical code is NFPA 70 (NEC)."
    ),
)


# ---------------------------------------------------------------------------
# Corpus-signal patterns (family (a), D-3) — owner-document vocabulary
# ---------------------------------------------------------------------------
# Document-name vocabulary the deterministic scrape looks for so research
# searches with the project's own terms. Compiled case-insensitive at scrape
# time. Discipline-specific document names (e.g. fire's "fire protection
# design guide") are appended module-locally AFTER these.
SHARED_DC_CORPUS_SIGNAL_PATTERNS: tuple[str, ...] = (
    r"\bbasis of design\b",
    r"\bBoD\b",
    r"\bowner'?s? project requirements\b",
    r"\bOPR\b",
    r"\bdesign (?:basis|criteria|guide(?:lines)?|standard)s?\b",
    r"\b(?:master|guide)[ -]?spec(?:ification)?s?\b",
)
