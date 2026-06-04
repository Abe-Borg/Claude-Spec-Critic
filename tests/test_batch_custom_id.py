"""Regression: review batch ``custom_id`` must stay within Anthropic's 1-64
character limit even for very long filenames and very large batch indexes.

The id is ``review__{stem}__{idx}``. The original code truncated the stem to a
fixed 50 chars, so ``review__`` (8) + 50 + ``__`` (2) + a 5-digit index = 65,
one over the 64-char Batches API ceiling — reachable on a 10k+ spec run. The
fix budgets the stem against the index-aware framing; these tests lock the
ceiling and the uniqueness property that makes the truncation safe.
"""

import pytest

from src.batch.batch import _review_custom_id, _sanitize_custom_id

_MAX = 64


def test_short_name_small_index_is_unchanged_shape():
    cid = _review_custom_id("M-21-fire-sprinklers.docx", 3)
    assert cid == "review__M-21-fire-sprinklers__3"
    assert len(cid) <= _MAX


@pytest.mark.parametrize("idx", [0, 9, 99, 9_999, 100_000, 9_999_999])
def test_long_name_large_index_stays_within_limit(idx):
    # A pathologically long, punctuation-heavy stem that the legacy 50-char
    # truncation would have pushed over the limit once paired with a big index.
    long_name = ("23 05 00 — Common Work Results for HVAC (Division 23 "
                 "mechanical basis-of-design narrative) FINAL rev C.docx")
    cid = _review_custom_id(long_name, idx)
    assert 1 <= len(cid) <= _MAX
    assert cid.startswith("review__")
    assert cid.endswith(f"__{idx}")


def test_ids_unique_even_when_stems_truncate_to_same_prefix():
    # Two distinct files whose sanitized stems share the first ~40 chars: after
    # truncation the stems collide, but the trailing ``__{idx}`` keeps the ids
    # distinct (uniqueness is carried by the enumerate index, not the stem).
    base = "23 09 00 Instrumentation and Control for HVAC narrative section "
    a = _review_custom_id(base + "ALPHA variant.docx", 7)
    b = _review_custom_id(base + "BETA variant.docx", 8)
    assert a != b
    assert len(a) <= _MAX and len(b) <= _MAX


def test_sanitize_respects_passed_max_len():
    # The stem helper honors the computed budget rather than the default 50.
    assert len(_sanitize_custom_id("x" * 200, max_len=10)) == 10


@pytest.mark.parametrize("idx", [0, 9, 999, 9999])
def test_low_index_stem_matches_legacy_default_50(idx):
    # Resume compatibility: the bare-batch recovery path
    # (orchestration/batch_resume.recover_from_bare_batch_id) re-sanitizes local
    # filenames with the default 50 and matches them against the parsed
    # custom_id stem. For every realistic index (< 10000) the submitted stem must
    # therefore stay byte-identical to the legacy 50-char truncation, or the
    # real-filename recovery silently falls back to the truncated stem.
    long_name = (
        "23 09 00 Instrumentation and Control for HVAC narrative "
        "section FINAL rev C.docx"
    )
    assert len(_sanitize_custom_id(long_name)) == 50  # guard: the cap actually bites
    cid = _review_custom_id(long_name, idx)
    stem = cid[len("review__"): -len(f"__{idx}")]
    assert stem == _sanitize_custom_id(long_name)  # legacy default max_len=50
    assert len(cid) <= _MAX
