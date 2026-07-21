"""Concurrency contracts for the process-local specification extraction cache."""
from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.input import extraction_cache as ec
from src.input.extractor import ExtractedSpec


def _result(path: Path) -> ExtractedSpec:
    return ExtractedSpec(
        filename=path.name,
        content=f"content:{path.name}",
        word_count=1,
        source_path=str(path),
        paragraph_map=[],
        extraction_warnings=[f"warning:{path.name}"],
    )


def _fresh_cache(monkeypatch) -> ec._ExtractionCache:
    cache = ec._ExtractionCache(max_entries=16)
    monkeypatch.setattr(ec, "_extraction_cache", cache)
    return cache


def test_concurrent_cold_misses_extract_each_path_once_and_isolate_results(
    tmp_path, monkeypatch
):
    cache = _fresh_cache(monkeypatch)
    path_a = tmp_path / "a.docx"
    path_b = tmp_path / "b.docx"
    path_a.write_bytes(b"a")
    path_b.write_bytes(b"b")

    calls: Counter[str] = Counter()
    calls_lock = threading.Lock()
    release_extractors = threading.Event()
    start_callers = threading.Barrier(2)
    waiter_seen = threading.Event()
    waiter_count = 0

    original_lookup = cache._lookup_or_reserve

    def observed_lookup(path):
        nonlocal waiter_count
        answer = original_lookup(path)
        if answer[0] == "waiter":
            with calls_lock:
                waiter_count += 1
                if waiter_count == 2:
                    waiter_seen.set()
        return answer

    def fake_extract(paths, *, max_workers=None):
        del max_workers
        with calls_lock:
            calls.update(path.name for path in paths)
        assert release_extractors.wait(timeout=2)
        return [_result(path) for path in paths]

    monkeypatch.setattr(cache, "_lookup_or_reserve", observed_lookup)
    monkeypatch.setattr("src.input.extractor.extract_multiple_specs", fake_extract)

    def load(paths):
        start_callers.wait(timeout=2)
        return ec.extract_multiple_specs_cached(paths)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(load, [path_a, path_b])
        second_future = pool.submit(load, [path_b, path_a])
        assert waiter_seen.wait(timeout=2)
        release_extractors.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert calls == Counter({"a.docx": 1, "b.docx": 1})
    assert [item.filename for item in first] == ["a.docx", "b.docx"]
    assert [item.filename for item in second] == ["b.docx", "a.docx"]

    # The cache, the leader, and every waiter own independent mutable values.
    first[0].content = "mutated"
    first[0].extraction_warnings.append("caller-only")
    assert second[1].content == "content:a.docx"
    assert second[1].extraction_warnings == ["warning:a.docx"]
    cached_again = ec.extract_multiple_specs_cached([path_a])[0]
    assert cached_again.content == "content:a.docx"
    assert cached_again.extraction_warnings == ["warning:a.docx"]
    assert calls == Counter({"a.docx": 1, "b.docx": 1})


def test_singleflight_propagates_leader_exception_and_allows_retry(
    tmp_path, monkeypatch
):
    cache = _fresh_cache(monkeypatch)
    path = tmp_path / "broken.docx"
    path.write_bytes(b"broken")

    calls = 0
    calls_lock = threading.Lock()
    release_extractor = threading.Event()
    start_callers = threading.Barrier(2)
    waiter_seen = threading.Event()
    original_lookup = cache._lookup_or_reserve

    def observed_lookup(candidate):
        answer = original_lookup(candidate)
        if answer[0] == "waiter":
            waiter_seen.set()
        return answer

    def failing_extract(paths, *, max_workers=None):
        nonlocal calls
        del paths, max_workers
        with calls_lock:
            calls += 1
        assert release_extractor.wait(timeout=2)
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(cache, "_lookup_or_reserve", observed_lookup)
    monkeypatch.setattr("src.input.extractor.extract_multiple_specs", failing_extract)

    def load():
        start_callers.wait(timeout=2)
        return ec.extract_multiple_specs_cached([path])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(load)
        second_future = pool.submit(load)
        assert waiter_seen.wait(timeout=2)
        release_extractor.set()
        for future in (first_future, second_future):
            with pytest.raises(RuntimeError, match="synthetic extraction failure"):
                future.result(timeout=2)

    assert calls == 1

    # Failure removes the reservation; a later run can retry and populate the
    # cache instead of inheriting a permanently failed Future.
    monkeypatch.setattr(
        "src.input.extractor.extract_multiple_specs",
        lambda paths, *, max_workers=None: [_result(candidate) for candidate in paths],
    )
    retried = ec.extract_multiple_specs_cached([path])
    assert [item.filename for item in retried] == ["broken.docx"]


def test_bad_owned_path_does_not_poison_healthy_shared_waiter(tmp_path, monkeypatch):
    cache = _fresh_cache(monkeypatch)
    bad = tmp_path / "bad.docx"
    good = tmp_path / "good.docx"
    bad.write_bytes(b"bad")
    good.write_bytes(b"good")

    calls: Counter[str] = Counter()
    calls_lock = threading.Lock()
    good_started = threading.Event()
    waiter_seen = threading.Event()
    release_good = threading.Event()
    original_lookup = cache._lookup_or_reserve

    def observed_lookup(path):
        answer = original_lookup(path)
        if path == good and answer[0] == "waiter":
            waiter_seen.set()
        return answer

    def mixed_extract(paths, *, max_workers=None):
        del max_workers
        assert len(paths) == 1
        path = paths[0]
        with calls_lock:
            calls[path.name] += 1
        if path == bad:
            raise RuntimeError("bad file only")
        good_started.set()
        assert release_good.wait(timeout=2)
        return [_result(path)]

    monkeypatch.setattr(cache, "_lookup_or_reserve", observed_lookup)
    monkeypatch.setattr("src.input.extractor.extract_multiple_specs", mixed_extract)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(ec.extract_multiple_specs_cached, [bad, good])
        assert good_started.wait(timeout=2)
        waiter = pool.submit(ec.extract_multiple_specs_cached, [good])
        assert waiter_seen.wait(timeout=2)
        release_good.set()

        with pytest.raises(RuntimeError, match="bad file only"):
            owner.result(timeout=2)
        waiter_result = waiter.result(timeout=2)

    assert [item.filename for item in waiter_result] == ["good.docx"]
    assert calls == Counter({"bad.docx": 1, "good.docx": 1})
