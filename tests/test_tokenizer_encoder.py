"""Encoder-loading contract in ``src.core.tokenizer`` (hermetic; tiktoken stubbed).

The tiktoken wheel does not ship the ``cl100k_base`` rank file — it is fetched
from the network at first use unless a cached copy sits in
``TIKTOKEN_CACHE_DIR``. These tests pin the pieces that make that failure
diagnosable and the Windows bundle possible:

* the URL / hash / cache-file-name constants mirror the pinned tiktoken,
* the cache-directory resolution mirrors tiktoken's own order,
* the encoder is memoized (one load per process, thread-safe),
* a load failure raises ``EncoderLoadError`` — an ``Exception`` subclass, so
  ``count_tokens`` callers that catch broadly are unchanged — whose message
  names ``TIKTOKEN_CACHE_DIR``, the directory in effect, and the download,
  and is logged under ``src.core.tokenizer``; a failure is never memoized.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import threading
from pathlib import Path

import pytest
import tiktoken

from src.core import tokenizer


@pytest.fixture
def fresh_encoder():
    """Run the test against an empty memo, then restore whatever was loaded."""
    with tokenizer._ENCODER_LOCK:
        saved = tokenizer._ENCODER
    tokenizer._reset_encoder_for_tests()
    try:
        yield
    finally:
        with tokenizer._ENCODER_LOCK:
            tokenizer._ENCODER = saved


class _FakeEncoding:
    def encode(self, text: str) -> list[int]:
        return [1] * len(text.split())


def _counting_loader(monkeypatch, *, fail_with: BaseException | None = None):
    calls: list[str] = []

    def fake_get_encoding(name: str):
        calls.append(name)
        if fail_with is not None:
            raise fail_with
        return _FakeEncoding()

    monkeypatch.setattr(tiktoken, "get_encoding", fake_get_encoding)
    return calls


# --------------------------------------------------------------------------
# Constants mirror the pinned tiktoken release
# --------------------------------------------------------------------------


class TestPinnedConstants:
    def test_url_and_hash_match_installed_tiktoken(self):
        spec = importlib.util.find_spec("tiktoken_ext.openai_public")
        assert spec is not None and spec.origin
        source = Path(spec.origin).read_text(encoding="utf-8")
        # Both literals must appear verbatim in the pinned plugin, and in the
        # cl100k_base constructor specifically.
        start = source.index("def cl100k_base(")
        body = source[start:source.index("def ", start + 1)]
        assert tokenizer.CL100K_BASE_BLOB_URL in body
        assert tokenizer.CL100K_BASE_SHA256 in body

    def test_cache_filename_is_sha1_of_url(self):
        expected = hashlib.sha1(tokenizer.CL100K_BASE_BLOB_URL.encode("utf-8")).hexdigest()
        assert tokenizer.cl100k_cache_filename() == expected
        assert len(expected) == 40

    def test_encoding_name(self):
        assert tokenizer.ENCODING_NAME == "cl100k_base"
        assert tokenizer.TIKTOKEN_CACHE_DIR_ENV == "TIKTOKEN_CACHE_DIR"


# --------------------------------------------------------------------------
# Cache-directory resolution mirrors tiktoken.load.read_file_cached
# --------------------------------------------------------------------------


class TestCacheDirResolution:
    def test_default_is_tempdir_data_gym_cache(self):
        resolved = tokenizer.effective_tiktoken_cache_dir({})
        assert os.path.basename(resolved) == "data-gym-cache"

    def test_legacy_var_beats_default(self):
        assert tokenizer.effective_tiktoken_cache_dir({"DATA_GYM_CACHE_DIR": "/legacy"}) == "/legacy"

    def test_tiktoken_cache_dir_beats_legacy(self):
        env = {"TIKTOKEN_CACHE_DIR": "/primary", "DATA_GYM_CACHE_DIR": "/legacy"}
        assert tokenizer.effective_tiktoken_cache_dir(env) == "/primary"

    def test_empty_string_is_returned_verbatim(self):
        # An empty TIKTOKEN_CACHE_DIR disables tiktoken's cache; report it as-is.
        assert tokenizer.effective_tiktoken_cache_dir({"TIKTOKEN_CACHE_DIR": ""}) == ""

    def test_status_reports_presence(self, tmp_path):
        env = {"TIKTOKEN_CACHE_DIR": str(tmp_path)}
        absent = tokenizer.encoder_cache_status(env)
        assert absent.cache_dir == str(tmp_path)
        assert absent.rank_file == os.path.join(str(tmp_path), tokenizer.cl100k_cache_filename())
        assert absent.rank_file_present is False

        Path(absent.rank_file).write_bytes(b"ranks")
        present = tokenizer.encoder_cache_status(env)
        assert present.rank_file_present is True

    def test_status_with_disabled_cache(self):
        status = tokenizer.encoder_cache_status({"TIKTOKEN_CACHE_DIR": ""})
        assert status.cache_dir == ""
        assert status.rank_file == ""
        assert status.rank_file_present is False


# --------------------------------------------------------------------------
# Memoization
# --------------------------------------------------------------------------


class TestEncoderMemoization:
    def test_one_load_per_process(self, fresh_encoder, monkeypatch):
        calls = _counting_loader(monkeypatch)
        first = tokenizer.get_encoder()
        second = tokenizer.get_encoder()
        assert first is second
        assert tokenizer.count_tokens("three words here") == 3
        assert calls == ["cl100k_base"]

    def test_concurrent_first_use_loads_once(self, fresh_encoder, monkeypatch):
        calls = _counting_loader(monkeypatch)
        results: list[object] = []
        errors: list[BaseException] = []
        start = threading.Barrier(8)

        def worker():
            try:
                start.wait()
                results.append(tokenizer.get_encoder())
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 8 and all(r is results[0] for r in results)
        assert calls == ["cl100k_base"]


# --------------------------------------------------------------------------
# Load failure: actionable, logged, chained, never memoized
# --------------------------------------------------------------------------


class TestEncoderLoadFailure:
    def test_failure_raises_actionable_message(self, fresh_encoder, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
        original = ConnectionError("Tunnel connection failed: 403 Forbidden")
        calls = _counting_loader(monkeypatch, fail_with=original)

        with caplog.at_level(logging.ERROR, logger="src.core.tokenizer"):
            with pytest.raises(tokenizer.EncoderLoadError) as excinfo:
                tokenizer.count_tokens("anything")

        message = str(excinfo.value)
        assert "TIKTOKEN_CACHE_DIR" in message
        assert str(tmp_path) in message
        assert "absent" in message  # the rank file was not there before the load
        assert tokenizer.CL100K_BASE_BLOB_URL in message
        assert "download" in message
        assert "ConnectionError: Tunnel connection failed: 403 Forbidden" in message
        assert "api.anthropic.com" in message
        assert excinfo.value.__cause__ is original
        # Existing callers catch ``Exception`` around count_tokens; keep it one.
        assert isinstance(excinfo.value, Exception)
        assert calls == ["cl100k_base"]

        records = [r for r in caplog.records if r.name == "src.core.tokenizer"]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert records[0].getMessage() == message

    def test_failure_is_not_memoized(self, fresh_encoder, monkeypatch):
        _counting_loader(monkeypatch, fail_with=ValueError("Hash mismatch"))
        with pytest.raises(tokenizer.EncoderLoadError):
            tokenizer.get_encoder()
        assert tokenizer._ENCODER is None

        calls = _counting_loader(monkeypatch)  # the operator fixed the cache
        assert isinstance(tokenizer.get_encoder(), _FakeEncoding)
        assert calls == ["cl100k_base"]

    def test_present_but_rejected_rank_file_is_reported(self, fresh_encoder, monkeypatch, tmp_path):
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
        (tmp_path / tokenizer.cl100k_cache_filename()).write_bytes(b"corrupt")
        _counting_loader(monkeypatch, fail_with=ValueError("Hash mismatch for data downloaded"))
        with pytest.raises(tokenizer.EncoderLoadError, match="present"):
            tokenizer.get_encoder()

    def test_disabled_cache_is_reported(self, fresh_encoder, monkeypatch):
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
        _counting_loader(monkeypatch, fail_with=ConnectionError("blocked"))
        with pytest.raises(tokenizer.EncoderLoadError, match="empty string"):
            tokenizer.get_encoder()
