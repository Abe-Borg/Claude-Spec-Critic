"""Verification cache growth is bounded: LRU entry cap + compact on-disk form.

Before this the only pruning was age-based (on load) and every save rewrote
every entry with ``indent=2``. Now ``SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES``
(default 5000, ``0`` disables, malformed → default) caps the store: a hit
touches ``last_used_ts`` and moves the entry to the LRU tail; put / save /
load evict from the head. ``last_used_ts`` is persisted additively (legacy
rows fall back to ``created_ts``, no schema bump) and the file is written
compact through the same atomic temp + replace.
"""
from __future__ import annotations

import json
import os

import pytest

from src.core.code_cycles import CALIFORNIA_2025
from src.verification import verification_cache as vc
from src.verification.verification_cache import (
    VerificationCache,
    _CACHE_SCHEMA_VERSION,
    cache_max_entries,
    singleflight_wait_seconds,
)
from src.verification.verifier import VerificationResult


class _Finding:
    def __init__(self, issue: str):
        self.issue = issue
        self.existingText = ""
        self.replacementText = ""
        self.codeReference = "NFPA 13 9.3"
        self.actionType = "EDIT"


def _grounded(n: int = 0) -> VerificationResult:
    url = f"https://example.gov/std/{n}"
    return VerificationResult(
        verdict="CONFIRMED",
        explanation=f"Confirmed #{n}.",
        sources=[url],
        grounded=True,
        searched_sources=[url],
        cited_sources=[url],
        accepted_sources=[url],
        source_quote="Verbatim quote.",
    )


def _fill(cache: VerificationCache, count: int, start: int = 0) -> list[_Finding]:
    findings = []
    for i in range(start, start + count):
        f = _Finding(f"claim {i}")
        cache.put(f, cycle=CALIFORNIA_2025, result=_grounded(i))
        findings.append(f)
    return findings


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


class TestMaxEntriesEnv:
    def test_default_is_5000(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", raising=False)
        assert cache_max_entries() == 5000

    @pytest.mark.parametrize("raw", ["abc", "-4", "1.5", "", "   "])
    def test_malformed_or_negative_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", raw)
        assert cache_max_entries() == 5000

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "0")
        assert cache_max_entries() == 0

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", " 250 ")
        assert cache_max_entries() == 250


class TestSingleflightWaitEnv:
    def test_default_is_900_seconds(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", raising=False)
        assert singleflight_wait_seconds() == 900.0

    @pytest.mark.parametrize("raw", ["abc", "-1", "nan", "inf", ""])
    def test_malformed_negative_or_non_finite_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", raw)
        assert singleflight_wait_seconds() == 900.0

    def test_zero_means_forever(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", "0")
        assert singleflight_wait_seconds() == 0.0

    def test_fractional_seconds_accepted(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", "0.25")
        assert singleflight_wait_seconds() == 0.25


# ---------------------------------------------------------------------------
# LRU semantics
# ---------------------------------------------------------------------------


class TestLruCap:
    def test_put_evicts_least_recently_used(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "3")
        cache = VerificationCache()
        f0, f1, f2 = _fill(cache, 3)
        # Touch f0 so f1 becomes the least recently used.
        assert cache.get(f0, cycle=CALIFORNIA_2025) is not None
        _fill(cache, 1, start=3)  # fourth entry → evict f1

        assert cache.stats()["size"] == 3
        assert cache.stats()["evicted"] == 1
        assert cache.get(f1, cycle=CALIFORNIA_2025) is None
        assert cache.get(f0, cycle=CALIFORNIA_2025) is not None
        assert cache.get(f2, cycle=CALIFORNIA_2025) is not None

    def test_hit_touches_last_used_without_rewriting_created(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "0")
        cache = VerificationCache()
        (f,) = _fill(cache, 1)
        key = next(iter(cache._entries))
        entry = cache._entries[key]
        created = entry.created_ts
        entry.last_used_ts = 1.0  # force an old recency stamp
        assert cache.get(f, cycle=CALIFORNIA_2025) is not None
        assert cache._entries[key].last_used_ts > 1.0
        assert cache._entries[key].created_ts == created

    def test_overwrite_moves_the_entry_to_the_tail(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "2")
        cache = VerificationCache()
        f0, f1 = _fill(cache, 2)
        cache.put(f0, cycle=CALIFORNIA_2025, result=_grounded(99))  # re-put f0
        _fill(cache, 1, start=5)  # evicts f1, the stale head
        assert cache.get(f1, cycle=CALIFORNIA_2025) is None
        assert cache.get(f0, cycle=CALIFORNIA_2025).explanation == "Confirmed #99."

    def test_zero_cap_is_unbounded(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "0")
        cache = VerificationCache()
        _fill(cache, 50)
        assert cache.stats()["size"] == 50
        assert cache.stats()["evicted"] == 0
        assert cache.stats()["max_entries"] == 0

    def test_save_applies_the_cap(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "0")
        cache = VerificationCache()
        _fill(cache, 10)
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "4")
        written = cache.save_to_disk(tmp_path / "cache.json")
        assert written == 4
        assert cache.stats()["size"] == 4
        payload = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
        assert len(payload["entries"]) == 4

    def test_load_orders_by_recency_and_applies_the_cap(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "0")
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "0")
        writer = VerificationCache()
        findings = _fill(writer, 5)
        # Make the FIRST-inserted entry the most recently used on disk.
        keys = list(writer._entries)
        for i, key in enumerate(keys):
            writer._entries[key].last_used_ts = 1_000.0 + i
        writer._entries[keys[0]].last_used_ts = 9_999.0
        writer.save_to_disk(tmp_path / "cache.json")

        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", "2")
        reader = VerificationCache()
        loaded = reader.load_from_disk(tmp_path / "cache.json")

        assert loaded == 2
        assert reader.stats()["evicted"] == 3
        # Survivors: the two most recently used — keys[0] (9999) and keys[4].
        assert reader.get(findings[0], cycle=CALIFORNIA_2025) is not None
        assert reader.get(findings[4], cycle=CALIFORNIA_2025) is not None
        assert reader.get(findings[1], cycle=CALIFORNIA_2025) is None


# ---------------------------------------------------------------------------
# On-disk shape
# ---------------------------------------------------------------------------


class TestDiskFormat:
    def test_last_used_ts_round_trips_additively(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "0")
        cache = VerificationCache()
        (f,) = _fill(cache, 1)
        key = next(iter(cache._entries))
        cache._entries[key].last_used_ts = 4_321.5
        cache.save_to_disk(tmp_path / "cache.json")

        payload = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
        assert payload["version"] == _CACHE_SCHEMA_VERSION
        assert payload["entries"][key]["last_used_ts"] == 4_321.5
        assert "created_ts" in payload["entries"][key]

        reader = VerificationCache()
        assert reader.load_from_disk(tmp_path / "cache.json") == 1
        assert reader._entries[key].last_used_ts == 4_321.5

    def test_legacy_row_without_last_used_falls_back_to_created(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "0")
        cache = VerificationCache()
        _fill(cache, 1)
        cache.save_to_disk(tmp_path / "cache.json")
        payload = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
        key = next(iter(payload["entries"]))
        del payload["entries"][key]["last_used_ts"]  # a pre-LRU file
        payload["entries"][key]["created_ts"] = 777.0
        (tmp_path / "cache.json").write_text(json.dumps(payload), encoding="utf-8")

        reader = VerificationCache()
        assert reader.load_from_disk(tmp_path / "cache.json") == 1
        assert reader._entries[key].created_ts == 777.0
        assert reader._entries[key].last_used_ts == 777.0

    def test_write_is_compact_and_atomic(self, tmp_path):
        cache = VerificationCache()
        _fill(cache, 3)
        target = tmp_path / "nested" / "cache.json"
        cache.save_to_disk(target)
        raw = target.read_text(encoding="utf-8")
        assert "\n" not in raw
        # Byte-for-byte the compact form: re-serializing with the compact
        # separators reproduces the file exactly (no indent, no ", " / ": ").
        assert raw == json.dumps(json.loads(raw), separators=(",", ":"))
        assert json.loads(raw)["version"] == _CACHE_SCHEMA_VERSION
        # No temp file left behind.
        assert [p.name for p in target.parent.iterdir()] == ["cache.json"]

    def test_compact_write_is_materially_smaller(self, tmp_path):
        """The synthetic 1000-entry measurement the fix was sized on."""
        os.environ.pop("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES", None)
        cache = VerificationCache()
        _fill(cache, 1000)
        compact = tmp_path / "compact.json"
        cache.save_to_disk(compact)
        with cache._lock:
            payload = {
                "version": _CACHE_SCHEMA_VERSION,
                "saved_at": 0.0,
                "entries": {
                    k: {
                        "created_ts": e.created_ts,
                        "last_used_ts": e.last_used_ts,
                        "result": vc._result_to_dict(e.result),
                    }
                    for k, e in cache._entries.items()
                },
            }
        indented = tmp_path / "indented.json"
        indented.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        assert compact.stat().st_size < 0.75 * indented.stat().st_size


class TestDocstringHonesty:
    def test_module_docstring_states_the_60_day_default(self):
        doc = vc.__doc__ or ""
        assert "60 days" in doc
        assert "the default (0) is a database" not in doc
        assert "SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES" in doc
