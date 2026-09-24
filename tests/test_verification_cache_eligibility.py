"""One cache-eligibility predicate at write, read, and disk load (plan WP-10).

Before the fix, ``VerificationCache.put`` checked ``grounded`` and the failure
and budget flags but not the verdict, so a *grounded* UNVERIFIED — the
verifier searching, finding sources, and still saying "I could not settle
this" — was cached for 60 days and replayed as a hit, and no later run ever
tried the claim again. ``load_from_disk`` repeated the same checks by hand, so
a row the old build wrote reloaded forever. And the loader trusted its data:
a NaN or infinite timestamp loaded (and poisoned the TTL and LRU ordering), and
a single string timestamp raised out of the load — after which the pipeline
started with an *empty* cache, discarding every valid row because of one.

These tests pin the replacement contract:

* ``cache_ineligibility_reason`` is the one rule; ``put``, ``get``, and
  ``load_from_disk`` all apply it, so they agree case by case;
* only grounded conclusive verdicts are reused — never an UNVERIFIED, a
  failure, a budget shortfall, a local classification, or a replay;
* legacy rows that fail the rule are ignored one by one, valid rows beside
  them still load, and there is no schema bump or flush;
* invalid records (timestamps, non-finite or malformed fields) are rejected
  individually and counted, never raised;
* a later run therefore retries an earlier UNVERIFIED under the normal
  escalation policy.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_cache import (
    VerificationCache,
    _CACHE_SCHEMA_VERSION,
    _CacheEntry,
    _result_to_dict,
    cache_ineligibility_reason,
    is_cache_eligible,
    make_cache_key,
)
from src.verification.verifier import VerificationResult

URL = "https://www.nfpa.org/codes-and-standards/nfpa-13"


def _finding(issue: str = "NFPA 13 spacing claim", **overrides) -> Finding:
    fields = dict(
        severity="MEDIUM",
        fileName="21 13 13.docx",
        section="3.2",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13 §10.2.4",
        confidence=0.5,
    )
    fields.update(overrides)
    return Finding(**fields)


def _result(verdict: str = "CONFIRMED", **overrides) -> VerificationResult:
    fields = dict(
        verdict=verdict,
        explanation="Checked against the retrieved standard.",
        grounded=True,
        sources=[URL],
        accepted_sources=[URL],
        searched_sources=[URL],
        cited_sources=[URL],
        source_quote="Sprinklers shall be spaced not more than 15 ft apart.",
        correction="Use 15 ft." if verdict == "CORRECTED" else None,
        cache_status="miss",
        web_search_requests=2,
        successful_source_count=1,
    )
    fields.update(overrides)
    return VerificationResult(**fields)


ELIGIBLE = [
    ("confirmed", _result("CONFIRMED")),
    ("corrected", _result("CORRECTED")),
    ("disputed_with_a_quote", _result("DISPUTED")),
    # DISPUTED is citation-gated but not quote-gated (the verifier's rule).
    ("disputed_without_a_quote", _result("DISPUTED", source_quote="")),
    # A contested verdict is still a grounded conclusion; a replay stays
    # contested because ``models_disagreed`` is persisted.
    ("contested", _result("CONFIRMED", models_disagreed=True)),
    # A result built outside the verifier (a test, an older call site).
    ("default_cache_status", _result("CONFIRMED", cache_status="n/a")),
    ("mixed_blank_and_real_sources", _result("CONFIRMED", accepted_sources=["", URL], sources=["  ", URL])),
]

INELIGIBLE = [
    ("unverified_grounded", _result("UNVERIFIED", source_quote=""), "inconclusive"),
    ("unverified_ungrounded", _result("UNVERIFIED", grounded=False, sources=[], accepted_sources=[]), "inconclusive"),
    ("unknown_verdict", _result("MAYBE"), "inconclusive"),
    ("empty_verdict", _result(""), "inconclusive"),
    ("operational_failure", _result("CONFIRMED", verification_failed=True), "operational failure"),
    ("budget_exhausted", _result("UNVERIFIED", budget_exhausted=True), "budget"),
    ("local_skip", _result("UNVERIFIED", cache_status="local_skip", grounded=False), "local"),
    ("local_mode_only", _result("CONFIRMED", verification_mode="local_skip"), "local"),
    ("a_hit_replay", _result("CONFIRMED", cache_status="hit"), "replay"),
    ("a_shared_replay", _result("UNVERIFIED", cache_status="shared"), "replay"),
    ("conclusive_but_ungrounded", _result("CONFIRMED", grounded=False), "not grounded"),
    ("no_citation", _result("DISPUTED", sources=[], accepted_sources=[]), "citation"),
    ("empty_citation", _result("DISPUTED", sources=[""], accepted_sources=[""]), "citation"),
    ("whitespace_citation", _result("DISPUTED", sources=["   "], accepted_sources=["   "]), "citation"),
    ("confirmed_without_a_quote", _result("CONFIRMED", source_quote="  "), "quote"),
    ("corrected_without_a_quote", _result("CORRECTED", source_quote=""), "quote"),
]

ALL_CASES = [(name, result, True) for name, result in ELIGIBLE] + [
    (name, result, False) for name, result, _reason in INELIGIBLE
]

# Cases whose disqualifying property is runtime-only: ``verification_failed``,
# ``budget_exhausted`` and ``cache_status`` are never persisted
# (``verification_cache._SKIPPED_FIELDS``), so a row on disk cannot express
# them — such a result is refused at ``put`` and never written. The load
# boundary is exercised with every case the persisted shape can carry.
_RUNTIME_ONLY_REASONS = {
    "operational_failure",
    "budget_exhausted",
    "local_skip",
    "a_hit_replay",
    "a_shared_replay",
}
LOAD_CASES = [case for case in ALL_CASES if case[0] not in _RUNTIME_ONLY_REASONS]


def _row(result: VerificationResult, *, created_ts: float | None = None, **extra) -> dict:
    row = {
        "created_ts": time.time() if created_ts is None else created_ts,
        "last_used_ts": time.time() if created_ts is None else created_ts,
        "result": _result_to_dict(result),
    }
    row.update(extra)
    return row


def _write(path: Path, entries: dict) -> Path:
    path.write_text(
        json.dumps({"version": _CACHE_SCHEMA_VERSION, "saved_at": time.time(), "entries": entries}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1. The predicate
# ---------------------------------------------------------------------------


class TestThePredicate:
    @pytest.mark.parametrize("name, result", ELIGIBLE, ids=[n for n, _ in ELIGIBLE])
    def test_grounded_conclusive_verdicts_are_eligible(self, name, result):
        assert cache_ineligibility_reason(result) is None
        assert is_cache_eligible(result) is True

    @pytest.mark.parametrize(
        "name, result, reason", INELIGIBLE, ids=[n for n, _, _ in INELIGIBLE]
    )
    def test_everything_else_is_refused_with_its_reason(self, name, result, reason):
        found = cache_ineligibility_reason(result)
        assert found is not None and reason in found
        assert is_cache_eligible(result) is False

    def test_a_missing_result_is_refused(self):
        assert cache_ineligibility_reason(None) is not None


# ---------------------------------------------------------------------------
# 2. Every boundary applies the same rule
# ---------------------------------------------------------------------------


class TestEveryBoundaryAgrees:
    @pytest.mark.parametrize(
        "name, result, eligible", ALL_CASES, ids=[n for n, _, _ in ALL_CASES]
    )
    def test_put(self, name, result, eligible):
        cache = VerificationCache()
        cache.put(_finding(), cycle=DEFAULT_CYCLE, result=result)
        assert cache.stats()["size"] == (1 if eligible else 0)

    @pytest.mark.parametrize(
        "name, result, eligible", ALL_CASES, ids=[n for n, _, _ in ALL_CASES]
    )
    def test_get(self, name, result, eligible):
        """An entry that reached the store some other way is dropped, not replayed."""
        cache = VerificationCache()
        finding = _finding()
        key = make_cache_key(finding, cycle=DEFAULT_CYCLE)
        cache._entries[key] = _CacheEntry(result=result, created_ts=time.time())
        replay = cache.get(finding, cycle=DEFAULT_CYCLE)
        assert (replay is not None) is eligible
        assert (key in cache._entries) is eligible

    @pytest.mark.parametrize(
        "name, result, eligible", LOAD_CASES, ids=[n for n, _, _ in LOAD_CASES]
    )
    def test_load(self, name, result, eligible, tmp_path):
        path = _write(tmp_path / "cache.json", {"k": _row(result)})
        cache = VerificationCache()
        assert cache.load_from_disk(path=path) == (1 if eligible else 0)
        assert cache.stats()["rejected_on_load"] == (0 if eligible else 1)

    @pytest.mark.parametrize("name", sorted(_RUNTIME_ONLY_REASONS))
    def test_runtime_only_flags_can_never_reach_the_disk(self, name, tmp_path):
        """The cases the load test skips: refused at ``put``, so never written."""
        (result,) = [r for n, r, _ in ALL_CASES if n == name]
        cache = VerificationCache()
        cache.put(_finding(), cycle=DEFAULT_CYCLE, result=result)
        path = tmp_path / "cache.json"
        assert cache.save_to_disk(path=path) == 0
        assert json.loads(path.read_text(encoding="utf-8"))["entries"] == {}


# ---------------------------------------------------------------------------
# 3. Legacy rows: ignored one by one, no flush, no schema bump
# ---------------------------------------------------------------------------


class TestLegacyRowsAreIgnoredOneByOne:
    def _legacy_file(self, tmp_path: Path) -> Path:
        """A cache file as the pre-fix build wrote it: its ``put`` stored any
        grounded UNVERIFIED, and an older build stored uncited verdicts."""
        return _write(
            tmp_path / "legacy.json",
            {
                "legacy_grounded_unverified": _row(_result("UNVERIFIED", source_quote="")),
                "legacy_unverified_2": _row(_result("UNVERIFIED", source_quote="", explanation="other")),
                "uncited_disputed": _row(_result("DISPUTED", sources=[], accepted_sources=[])),
                "valid_confirmed": _row(_result("CONFIRMED")),
                "valid_corrected": _row(_result("CORRECTED")),
                "valid_disputed": _row(_result("DISPUTED", source_quote="")),
            },
        )

    def test_valid_conclusive_rows_still_load_beside_them(self, tmp_path):
        cache = VerificationCache()
        assert cache.load_from_disk(path=self._legacy_file(tmp_path)) == 3
        assert sorted(cache._entries) == ["valid_confirmed", "valid_corrected", "valid_disputed"]
        stats = cache.stats()
        assert stats["rejected_on_load"] == 3
        assert stats["expired_on_load"] == 0

    def test_the_next_save_keeps_exactly_the_valid_rows(self, tmp_path):
        """Self-cleaning, not a flush: only the ignored rows leave the file."""
        path = self._legacy_file(tmp_path)
        cache = VerificationCache()
        cache.load_from_disk(path=path)
        cache.save_to_disk(path=path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["version"] == _CACHE_SCHEMA_VERSION == 4  # no schema bump
        assert sorted(saved["entries"]) == ["valid_confirmed", "valid_corrected", "valid_disputed"]

    def test_a_file_of_only_legacy_unverified_rows_loads_nothing_and_raises_nothing(self, tmp_path):
        path = _write(
            tmp_path / "only_legacy.json",
            {f"u{i}": _row(_result("UNVERIFIED", source_quote="", explanation=f"e{i}")) for i in range(4)},
        )
        cache = VerificationCache()
        assert cache.load_from_disk(path=path) == 0
        assert cache.stats()["rejected_on_load"] == 4


# ---------------------------------------------------------------------------
# 4. Invalid records are rejected individually
# ---------------------------------------------------------------------------

_NOW = time.time()
_FUTURE = _NOW + 30 * 86400

BAD_CREATED = {
    "nan": float("nan"),
    "inf": float("inf"),
    "-inf": float("-inf"),
    "negative": -1.0,
    "zero": 0,
    "missing": None,
    "string": "yesterday",
    "numeric_string": str(_NOW),
    "bool": True,
    "future": _FUTURE,
    "too_large_for_a_float": 10**400,
}

BAD_LAST_USED = {
    "nan": float("nan"),
    "inf": float("inf"),
    "negative": -5,
    "string": "x",
    "bool": True,
    "future": _FUTURE,
}

BAD_FIELDS = {
    "nan_count": {"web_search_requests": float("nan")},
    "inf_count": {"web_search_requests": float("inf")},
    "negative_count": {"successful_source_count": -1},
    "fractional_count": {"web_fetch_requests": 2.5},
    "string_count": {"search_error_count": "3"},
    "bool_count": {"web_search_requests": True},
    "string_bool": {"grounded": "false"},
    "string_sources": {"accepted_sources": URL},
    "non_string_source": {"sources": [URL, 7]},
    "non_string_verdict": {"verdict": 5},
    "non_string_correction": {"correction": 5},
}


def _dump(entries: dict) -> str:
    # ``allow_nan`` is the default: a hand-edited file can hold NaN / Infinity
    # and Python's json module reads them back.
    return json.dumps({"version": _CACHE_SCHEMA_VERSION, "saved_at": _NOW, "entries": entries})


class TestInvalidRecordsAreRejectedIndividually:
    def _load_with(self, tmp_path, bad_rows: dict) -> VerificationCache:
        entries = {"good": _row(_result("CONFIRMED"))}
        entries.update(bad_rows)
        path = tmp_path / "cache.json"
        path.write_text(_dump(entries), encoding="utf-8")
        cache = VerificationCache()
        cache.load_from_disk(path=path)
        return cache

    @pytest.mark.parametrize("label, value", BAD_CREATED.items(), ids=list(BAD_CREATED))
    def test_an_invalid_creation_time(self, tmp_path, label, value):
        row = _row(_result("CONFIRMED"))
        row["created_ts"] = value
        if value is None:
            del row["created_ts"]
        cache = self._load_with(tmp_path, {"bad": row})
        assert sorted(cache._entries) == ["good"]
        assert cache.stats()["rejected_on_load"] == 1

    @pytest.mark.parametrize("label, value", BAD_LAST_USED.items(), ids=list(BAD_LAST_USED))
    def test_an_invalid_last_used_time(self, tmp_path, label, value):
        row = _row(_result("CONFIRMED"))
        row["last_used_ts"] = value
        cache = self._load_with(tmp_path, {"bad": row})
        assert sorted(cache._entries) == ["good"]
        assert cache.stats()["rejected_on_load"] == 1

    @pytest.mark.parametrize("absent", [None, 0, 0.0, "missing"], ids=["none", "int_zero", "float_zero", "missing"])
    def test_a_legacy_row_without_a_last_used_time_still_loads(self, tmp_path, absent):
        created = _NOW - 3600
        row = _row(_result("CONFIRMED"), created_ts=created)
        if absent == "missing":
            del row["last_used_ts"]
        else:
            row["last_used_ts"] = absent
        cache = self._load_with(tmp_path, {"legacy": row})
        assert "legacy" in cache._entries
        assert cache._entries["legacy"].last_used_ts == created

    @pytest.mark.parametrize("label, patch", BAD_FIELDS.items(), ids=list(BAD_FIELDS))
    def test_a_field_of_the_wrong_type_or_a_non_finite_number(self, tmp_path, label, patch):
        row = _row(_result("CONFIRMED"))
        row["result"].update(patch)
        cache = self._load_with(tmp_path, {"bad": row})
        assert sorted(cache._entries) == ["good"]
        assert cache.stats()["rejected_on_load"] == 1

    def test_a_whole_float_count_is_a_count(self, tmp_path):
        row = _row(_result("CONFIRMED"))
        row["result"]["web_search_requests"] = 3.0
        cache = self._load_with(tmp_path, {"float_count": row})
        assert cache._entries["float_count"].result.web_search_requests == 3

    def test_blank_list_entries_are_dropped_not_replayed(self, tmp_path):
        row = _row(_result("CONFIRMED"))
        row["result"]["accepted_sources"] = ["", "   ", URL]
        row["result"]["sources"] = ["", URL]
        cache = self._load_with(tmp_path, {"k": row})
        result = cache._entries["k"].result
        assert result.accepted_sources == [URL]
        assert result.sources == [URL]

    def test_every_bad_row_at_once_still_loads_the_good_one(self, tmp_path):
        """One bad record never costs the file its valid ones (it used to raise)."""
        bad: dict = {}
        for label, value in BAD_CREATED.items():
            row = _row(_result("CONFIRMED"))
            row["created_ts"] = value
            bad[f"created_{label}"] = row
        for label, patch in BAD_FIELDS.items():
            row = _row(_result("CONFIRMED"))
            row["result"].update(patch)
            bad[f"field_{label}"] = row
        bad["not_a_dict"] = "junk"
        bad["result_not_a_dict"] = {"created_ts": _NOW, "result": ["junk"]}
        cache = self._load_with(tmp_path, bad)
        assert sorted(cache._entries) == ["good"]
        assert cache.stats()["rejected_on_load"] == len(bad)

    def test_a_non_numeric_schema_version_is_refused_not_raised(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text(json.dumps({"version": "four", "entries": {}}), encoding="utf-8")
        assert VerificationCache().load_from_disk(path=path) == 0

    def test_nan_really_round_trips_through_json(self):
        """The premise of the NaN / Infinity cases: the file can hold them."""
        loaded = json.loads(_dump({"x": {"created_ts": float("nan")}}))
        assert math.isnan(loaded["entries"]["x"]["created_ts"])


# ---------------------------------------------------------------------------
# 5. The run's log says what was ignored
# ---------------------------------------------------------------------------


class TestThePipelineLogLine:
    def test_ignored_rows_are_counted_in_the_load_message(self, tmp_path, monkeypatch):
        from src.orchestration.pipeline import _make_verification_cache

        path = _write(
            tmp_path / "cache.json",
            {
                "valid": _row(_result("CONFIRMED")),
                "legacy_a": _row(_result("UNVERIFIED", source_quote="")),
                "legacy_b": _row(_result("UNVERIFIED", source_quote="", explanation="b")),
            },
        )
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(path))
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", raising=False)
        lines: list[str] = []
        cache = _make_verification_cache(log=lambda msg, **_kw: lines.append(msg))
        assert cache.stats()["size"] == 1
        assert lines == [
            "Verification cache: loaded 1 entry(ies) from disk, "
            "2 ignored (not reusable or invalid)."
        ]

    def test_a_file_of_only_ignored_rows_is_still_reported(self, tmp_path, monkeypatch):
        from src.orchestration.pipeline import _make_verification_cache

        path = _write(tmp_path / "cache.json", {"legacy": _row(_result("UNVERIFIED", source_quote=""))})
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(path))
        monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", raising=False)
        lines: list[str] = []
        _make_verification_cache(log=lambda msg, **_kw: lines.append(msg))
        assert lines == [
            "Verification cache: loaded 0 entry(ies) from disk, "
            "1 ignored (not reusable or invalid)."
        ]


# ---------------------------------------------------------------------------
# 6. A later run retries an earlier UNVERIFIED under the normal policy
# ---------------------------------------------------------------------------


class TestALaterRunRetries:
    """Driven through the real ``verify_finding`` and the real disk round trip.

    A HIGH finding: its UNVERIFIED initial pass escalates, which is the
    "normal escalation policy" a later run must get to apply again.
    """

    @staticmethod
    def _run(monkeypatch, verdict: str, cache: VerificationCache):
        from tests.fixtures.verification_drivers import (
            message,
            run_realtime,
            search_blocks,
            verdict_call,
            verdict_payload,
        )

        payload = (
            verdict_payload("UNVERIFIED", source_quote=None)
            if verdict == "UNVERIFIED"
            else verdict_payload(verdict)
        )
        msg = message([*search_blocks(), verdict_call(payload)])
        finding = _finding(severity="HIGH", issue="An adoption question the verifier cannot settle")
        result, client = run_realtime(monkeypatch, msg, finding=finding, cache=cache)
        return result, [call["model"] for call in client.calls]

    def test_an_unverified_is_verified_again_and_escalates_again(self, monkeypatch, tmp_path):
        from src.verification.verification_prescreen import VERIFICATION_ESCALATION_MODEL

        path = tmp_path / "cache.json"
        first = VerificationCache()
        result, models = self._run(monkeypatch, "UNVERIFIED", first)
        assert result.verdict == "UNVERIFIED" and result.grounded
        assert len(models) == 2 and models[1] == VERIFICATION_ESCALATION_MODEL
        assert first.save_to_disk(path=path) == 0  # nothing reusable to save

        second = VerificationCache()
        second.load_from_disk(path=path)
        again, models_again = self._run(monkeypatch, "UNVERIFIED", second)
        # Retried from scratch, and the escalation policy applied again.
        assert models_again == models
        assert again.cache_status == "miss"
        assert again.escalation_attempted is True

    def test_a_legacy_unverified_row_does_not_stop_the_retry(self, monkeypatch, tmp_path):
        """The row the pre-fix build wrote for this very finding is ignored."""
        finding = _finding(severity="HIGH", issue="An adoption question the verifier cannot settle")
        key = make_cache_key(finding, cycle=DEFAULT_CYCLE)
        path = _write(tmp_path / "cache.json", {key: _row(_result("UNVERIFIED", source_quote=""))})
        cache = VerificationCache()
        cache.load_from_disk(path=path)
        assert cache.stats()["rejected_on_load"] == 1
        result, models = self._run(monkeypatch, "UNVERIFIED", cache)
        assert len(models) == 2  # initial pass + escalation, not a replay
        assert result.cache_status == "miss"

    def test_control_a_confirmed_is_replayed_by_the_later_run(self, monkeypatch, tmp_path):
        path = tmp_path / "cache.json"
        first = VerificationCache()
        result, models = self._run(monkeypatch, "CONFIRMED", first)
        assert result.verdict == "CONFIRMED" and len(models) == 1
        assert first.save_to_disk(path=path) == 1

        second = VerificationCache()
        second.load_from_disk(path=path)
        replay, models_again = self._run(monkeypatch, "CONFIRMED", second)
        assert models_again == []  # no API call at all
        assert replay.cache_status == "hit"
        assert replay.cache_entry_created_ts > 0
