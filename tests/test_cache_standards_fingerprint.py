"""The verification cache key folds in a fingerprint of the cycle's pinned
standard editions (``_standards_fingerprint``).

Before this, the cache key carried only ``cycle.label`` ("2025"), so correcting
an edition string *inside* a cycle — e.g. fixing an UNVERIFIED ASHRAE edition —
left every verdict that had been grounded against the *old* edition silently
cached. These tests lock that an edition change now invalidates the affected
keys, while a provenance-only (``source``) change does not.
"""
from __future__ import annotations

import dataclasses

from src.core.code_cycles import CALIFORNIA_2025, StandardEdition
from src.review.reviewer import Finding
from src.verification.verification_cache import (
    _CACHE_SCHEMA_VERSION,
    _standards_fingerprint,
    make_cache_key,
)


def _finding() -> Finding:
    return Finding(
        severity="HIGH",
        fileName="Section_23_0000.docx",
        section="2.1",
        issue="Cited the wrong ASHRAE 90.1 edition",
        actionType="EDIT",
        existingText="ASHRAE 90.1-2022",
        replacementText="ASHRAE 90.1-2019",
        codeReference="ASHRAE 90.1",
    )


def _with_standards(*stds: StandardEdition):
    return dataclasses.replace(CALIFORNIA_2025, standards=tuple(stds))


def test_schema_version_is_v4():
    # The standards-fingerprint change bumps the on-disk schema so v3 files drop.
    assert _CACHE_SCHEMA_VERSION == 4


def test_key_is_stable_for_same_cycle():
    f = _finding()
    assert make_cache_key(f, cycle=CALIFORNIA_2025) == make_cache_key(
        f, cycle=CALIFORNIA_2025
    )


def test_fingerprint_is_in_the_key():
    f = _finding()
    fp = _standards_fingerprint(CALIFORNIA_2025)
    assert fp and fp in make_cache_key(f, cycle=CALIFORNIA_2025)


def test_correcting_an_edition_changes_the_key():
    f = _finding()
    a = _with_standards(StandardEdition("ASHRAE 90.1", "2022", source="UNVERIFIED: x"))
    b = _with_standards(
        StandardEdition("ASHRAE 90.1", "2019", source="confirmed: Title 24 2025")
    )
    assert make_cache_key(f, cycle=a) != make_cache_key(f, cycle=b)


def test_source_only_change_keeps_the_key_warm():
    # Confirming an already-correct edition (source flips off UNVERIFIED, edition
    # unchanged) must NOT invalidate — the verification question is identical.
    f = _finding()
    unconfirmed = _with_standards(
        StandardEdition("ASHRAE 90.1", "2019", source="UNVERIFIED: pending")
    )
    confirmed = _with_standards(
        StandardEdition("ASHRAE 90.1", "2019", source="confirmed: Title 24 2025 table")
    )
    assert make_cache_key(f, cycle=unconfirmed) == make_cache_key(f, cycle=confirmed)


def test_standardsless_cycle_degrades_to_no_std_sentinel():
    f = _finding()
    empty = _with_standards()  # a cycle that pins nothing
    assert _standards_fingerprint(empty) == ""
    assert "_no_std" in make_cache_key(f, cycle=empty)
