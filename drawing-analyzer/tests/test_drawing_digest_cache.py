"""Digest-cache tests (Workstream 1).

Hermetic: builds a fake ``RenderedSheet`` and a fake Anthropic client, so no
PyMuPDF, no network, no on-disk default cache (tests inject their own
``DigestCache`` — in-memory or pointed at ``tmp_path``).
"""
from __future__ import annotations

import json
from pathlib import Path

from drawing_analyzer.digest import DIGEST_PROMPT_VERSION, digest_sheet
from drawing_analyzer.digest_cache import (
    DigestCache,
    default_cache_path,
    digest_cache_key,
    persistence_enabled,
)
from drawing_analyzer.models import ImageTile, RenderedSheet, SheetRef
from tests.fixtures.fake_anthropic import FakeMessage, FakeTextBlock, FakeUsage

OPUS = "claude-opus-4-8"


def _sheet(overview: bytes = b"OVERVIEW", tiles=(b"T0", b"T1")) -> RenderedSheet:
    ref = SheetRef(
        pdf_path=Path("M-101.pdf"), page_index=0, source_name="M-101.pdf", page_count=1
    )
    ov = ImageTile(png_bytes=overview, width_px=100, height_px=80, kind="overview")
    tl = [
        ImageTile(png_bytes=b, width_px=100, height_px=80, kind="tile", row=i, col=0, label=f"r{i}c0")
        for i, b in enumerate(tiles)
    ]
    return RenderedSheet(
        ref=ref, overview=ov, tiles=tl, page_width_pt=100, page_height_pt=80,
        rows=len(tl), cols=1,
    )


class _CountingClient:
    def __init__(self, responder):
        self.calls = 0
        self._responder = responder

        class _Msgs:
            def create(_self, **kw):
                self.calls += 1
                return self._responder(kw)

        self.messages = _Msgs()


def _ok_response(_kw):
    return FakeMessage(
        content=[FakeTextBlock(text="VAV-3 serves Rm 120")],
        usage=FakeUsage(input_tokens=500, output_tokens=80),
    )


# --------------------------------------------------------------------------- #
# Key
# --------------------------------------------------------------------------- #


def _key(sheet, **over):
    base = dict(
        model=OPUS, prompt_version=DIGEST_PROMPT_VERSION,
        max_tokens=16000, effort="high", use_thinking=True,
    )
    base.update(over)
    return digest_cache_key(sheet, **base)


def test_key_stable_for_same_inputs():
    assert _key(_sheet()) == _key(_sheet())


def test_key_changes_with_content_model_and_params():
    base = _key(_sheet())
    assert _key(_sheet(overview=b"DIFFERENT")) != base       # page content
    assert _key(_sheet(tiles=(b"T0", b"T9"))) != base        # a tile changed
    assert _key(_sheet(), model="claude-sonnet-4-6") != base  # model swap
    assert _key(_sheet(), prompt_version="other") != base     # prompt edit
    assert _key(_sheet(), max_tokens=8000) != base
    assert _key(_sheet(), effort="low") != base
    assert _key(_sheet(), use_thinking=False) != base


# --------------------------------------------------------------------------- #
# DigestCache store/persistence
# --------------------------------------------------------------------------- #


def test_cache_in_memory_get_put():
    c = DigestCache(None, persist=False)
    assert c.get("k") is None
    c.put("k", {"text": "hi"})
    assert c.get("k") == {"text": "hi"}
    assert c.stats()["size"] == 1


def test_cache_persists_and_reloads(tmp_path):
    path = tmp_path / "dc.json"
    c1 = DigestCache(path, persist=True)
    c1.put("k", {"text": "persisted"})
    assert path.exists()
    # A fresh instance loads the entry from disk.
    c2 = DigestCache(path, persist=True)
    assert c2.get("k") == {"text": "persisted"}


def test_cache_persist_false_writes_no_file(tmp_path):
    path = tmp_path / "dc.json"
    c = DigestCache(path, persist=False)
    c.put("k", {"text": "x"})
    assert not path.exists()


def test_cache_corrupt_file_loads_empty(tmp_path):
    path = tmp_path / "dc.json"
    path.write_text("not json {", encoding="utf-8")
    c = DigestCache(path, persist=True)  # must not raise
    assert c.get("k") is None


def test_cache_wrong_schema_ignored(tmp_path):
    path = tmp_path / "dc.json"
    path.write_text(json.dumps({"_schema_version": 999, "entries": {"k": {"text": "x"}}}), encoding="utf-8")
    c = DigestCache(path, persist=True)
    assert c.get("k") is None


def test_default_cache_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DRAWING_ANALYZER_CACHE_PATH", str(tmp_path / "custom.json"))
    assert default_cache_path() == tmp_path / "custom.json"


def test_persistence_toggle(monkeypatch):
    monkeypatch.setenv("DRAWING_ANALYZER_CACHE_PERSIST", "off")
    assert persistence_enabled() is False
    monkeypatch.setenv("DRAWING_ANALYZER_CACHE_PERSIST", "1")
    assert persistence_enabled() is True


# --------------------------------------------------------------------------- #
# digest_sheet ↔ cache integration
# --------------------------------------------------------------------------- #


def test_digest_sheet_stores_then_serves_from_cache():
    cache = DigestCache(None, persist=False)
    client = _CountingClient(_ok_response)

    first = digest_sheet(_sheet(), client=client, model=OPUS, cache=cache)
    assert first.ok and first.cached is False
    assert client.calls == 1

    second = digest_sheet(_sheet(), client=client, model=OPUS, cache=cache)
    assert second.ok and second.cached is True
    assert second.text == "VAV-3 serves Rm 120"
    assert second.input_tokens == 500 and second.output_tokens == 80
    assert client.calls == 1  # no second API call — served from cache


def test_digest_sheet_cache_miss_on_model_change():
    cache = DigestCache(None, persist=False)
    client = _CountingClient(_ok_response)

    digest_sheet(_sheet(), client=client, model=OPUS, cache=cache)
    # Different model → different key → another API call.
    sd = digest_sheet(_sheet(), client=client, model="claude-sonnet-4-6", cache=cache)
    assert sd.cached is False
    assert client.calls == 2


def test_digest_sheet_does_not_cache_empty_result():
    cache = DigestCache(None, persist=False)
    client = _CountingClient(lambda kw: FakeMessage(content=[], stop_reason="max_tokens"))

    sd = digest_sheet(_sheet(), client=client, model=OPUS, cache=cache)
    assert not sd.ok
    assert cache.stats()["size"] == 0  # empty/error digests are never cached


def test_digest_sheet_no_cache_when_none():
    client = _CountingClient(_ok_response)
    sd = digest_sheet(_sheet(), client=client, model=OPUS)  # cache=None
    assert sd.ok and sd.cached is False
    assert client.calls == 1
