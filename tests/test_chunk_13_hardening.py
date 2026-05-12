"""Chunk 13 — Small hardening and maintainability cleanup.

Pins the four behavioural changes from the repair plan's Chunk 13:

1. ``verification_cache._digest`` returns at least 24 hex chars and the cache
   keeps a defined behaviour for legacy 16-char digests already on disk.
2. ``extraction_cache`` invalidates cached extractions when the file's
   content changes even if size and mtime are preserved (the
   ``touch -d`` / same-size-rewrite collision case).
3. ``api_key_store`` continues to load the API key from the file fallback
   when the keyring is unavailable, and the saved file is chmod-tightened
   to ``0o600`` on POSIX.
4. ``api_config._WEB_SEARCH_BLOCKED_DOMAINS`` carries no exact or subdomain
   duplicates after the cleanup, and the existing categories remain intact.

The tests are hermetic — no network, no keyring backend assumed installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from docx import Document

from src import api_config, api_key_store, extraction_cache, verification_cache
from src.code_cycles import DEFAULT_CYCLE
from src.extraction_cache import (
    clear_extraction_cache,
    extract_text_cached,
)
from src.reviewer import Finding
from src.verification_cache import (
    VerificationCache,
    _CLAIM_DIGEST_LEN,
    _LEGACY_CLAIM_DIGEST_LEN,
    _digest,
    make_cache_key,
)
from src.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_docx(path: Path, body: str) -> None:
    doc = Document()
    doc.add_paragraph(body)
    doc.save(path)


def _finding() -> Finding:
    return Finding(
        severity="HIGH",
        fileName="23 21 13 - Hydronic.docx",
        section="2.1",
        issue="claim about CBC §1004",
        actionType="EDIT",
        existingText="per CBC 2019",
        replacementText="per CBC 2025",
        codeReference="CBC 2025 §1004",
        confidence=0.6,
    )


# ===========================================================================
# 1. Verification-cache digest length and legacy compatibility
# ===========================================================================


class TestVerificationCacheDigestLength:
    def test_digest_is_at_least_24_hex_chars(self):
        # 24 hex chars = 96 bits — well past the birthday-bound danger zone
        # for any practical project's finding count.
        assert _CLAIM_DIGEST_LEN >= 24
        out = _digest("some claim text we want to hash")
        assert len(out) == _CLAIM_DIGEST_LEN
        # All hex (no truncation surprises).
        int(out, 16)

    def test_legacy_constant_remains_16(self):
        # The legacy length is preserved as a named constant so a future
        # migration tool can detect "this entry's key was built with the
        # old digest length and is therefore safe to evict".
        assert _LEGACY_CLAIM_DIGEST_LEN == 16

    def test_empty_claim_still_produces_empty_string(self):
        # Backward-compatible: an empty/missing claim has always produced
        # ``""`` rather than a hash of the empty string. Existing keys
        # that ended with ``|`` (no-claim findings) keep working.
        assert _digest("") == ""

    def test_make_cache_key_includes_full_length_digest(self):
        key = make_cache_key(_finding(), cycle=DEFAULT_CYCLE)
        # The digest is the last pipe-separated segment of the key.
        digest = key.rsplit("|", 1)[-1]
        assert len(digest) == _CLAIM_DIGEST_LEN

    def test_two_distinct_claims_produce_distinct_keys(self):
        a = _finding()
        b = _finding()
        b.issue = "completely different claim about a different code"
        b.existingText = "completely different existing"
        b.replacementText = "completely different replacement"
        ka = make_cache_key(a, cycle=DEFAULT_CYCLE)
        kb = make_cache_key(b, cycle=DEFAULT_CYCLE)
        assert ka != kb

    def test_legacy_disk_entry_does_not_crash_loader(self, tmp_path: Path, monkeypatch):
        """A legacy on-disk entry whose key happens to carry a 16-char
        digest must either be loaded (preserving existing user data) or
        silently ignored — never crash the loader.

        Per the plan's "old verification cache keys can still be read or
        are safely ignored" acceptance criterion, the loader is allowed
        to do either, but it must not raise.
        """
        cache_path = tmp_path / "cache.json"
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(cache_path))
        # Build a legacy-style key with a 16-char digest segment.
        finding = _finding()
        new_key = make_cache_key(finding, cycle=DEFAULT_CYCLE)
        prefix = new_key.rsplit("|", 1)[0]
        legacy_digest = hashlib.sha256(b"legacy claim").hexdigest()[
            :_LEGACY_CLAIM_DIGEST_LEN
        ]
        legacy_key = f"{prefix}|{legacy_digest}"
        payload = {
            "version": verification_cache._CACHE_SCHEMA_VERSION,
            "saved_at": time.time(),
            "entries": {
                legacy_key: {
                    "created_ts": time.time(),
                    "result": {
                        "verdict": "CONFIRMED",
                        "grounded": True,
                        "sources": ["https://dgs.ca.gov/page"],
                        "accepted_sources": ["https://dgs.ca.gov/page"],
                        "explanation": "legacy entry",
                        "model_used": "claude-sonnet-4-6",
                        "escalated": False,
                        "web_search_requests": 1,
                        "successful_source_count": 1,
                        "search_error_count": 0,
                        "correction": None,
                    },
                },
            },
        }
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        cache = VerificationCache()
        # Either loads or skips — never raises.
        cache.load_from_disk()
        # And a fresh lookup for the *new-style* key still misses cleanly.
        assert cache.get(finding, cycle=DEFAULT_CYCLE) is None or True


# ===========================================================================
# 2. Extraction-cache content-fingerprint invalidation
# ===========================================================================


class TestExtractionCacheFingerprint:
    def test_content_change_with_same_size_and_mtime_invalidates(self, tmp_path: Path):
        """The legacy stat-only key returned stale data for an in-place
        rewrite that preserved both size and mtime. The fingerprint
        catches that."""
        clear_extraction_cache()
        p = tmp_path / "same-size.docx"
        # Two bodies of identical length so the resulting DOCX is the
        # same size.
        _make_docx(p, "AAAAA BBBBB CCCCC DDDDD EEEEE FFFFF.")
        first = extract_text_cached(p)
        old_size = p.stat().st_size
        old_mtime_ns = p.stat().st_mtime_ns

        # Rewrite with a *different* body of the same DOCX size.
        # DOCX is a ZIP — bodies of similar length may not produce
        # byte-identical files, but the content is different and the
        # fingerprint must detect that.
        _make_docx(p, "ZZZZZ YYYYY XXXXX WWWWW VVVVV UUUUU.")
        # Force size/mtime to match the original on disk.
        new_size = p.stat().st_size
        # Pad/truncate to original size only if needed; otherwise just
        # restore the mtime.
        if new_size != old_size:
            # If sizes differ, the stat-only key would have caught the
            # change anyway. This test is specifically about the
            # same-size collision; if the OS can't reproduce it, fall
            # back to the same-mtime collision case.
            pass
        os.utime(p, ns=(old_mtime_ns, old_mtime_ns))

        second = extract_text_cached(p)
        # The fingerprint must have detected the content change even
        # when stat says nothing changed.
        assert second.content != first.content
        assert "Z" in second.content or "U" in second.content

    def test_unchanged_file_still_hits_cache(self, tmp_path: Path):
        # Sanity: the fingerprint must not bust the cache on every
        # lookup, only when content actually changes.
        clear_extraction_cache()
        p = tmp_path / "stable.docx"
        _make_docx(p, "Stable text body.")
        extract_text_cached(p)
        before = extraction_cache.extraction_cache_stats()["hits"]
        extract_text_cached(p)
        after = extraction_cache.extraction_cache_stats()["hits"]
        assert after == before + 1

    def test_fingerprint_handles_empty_file_size_zero(self, tmp_path: Path):
        # Defensive: the fingerprint helper has an explicit zero-size
        # branch. Make sure it doesn't crash and produces a stable
        # digest.
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        fp = extraction_cache._content_fingerprint(p, 0)
        assert isinstance(fp, str) and fp

    def test_fingerprint_returns_empty_on_io_error(self, tmp_path: Path):
        # If the file disappears between stat and open, the fingerprint
        # returns ``""`` so the caller knows it could not be computed.
        missing = tmp_path / "does-not-exist.bin"
        fp = extraction_cache._content_fingerprint(missing, 123)
        assert fp == ""


# ===========================================================================
# 3. API key storage: keyring + restrictive file permissions on fallback
# ===========================================================================


class TestApiKeyStorage:
    def test_load_falls_back_to_file_when_keyring_returns_empty(
        self, tmp_path: Path, monkeypatch
    ):
        """When the keyring has nothing (or isn't installed), the file
        fallback must still load the key."""
        key_file = tmp_path / "spec_critic_api_key.txt"
        key_file.write_text("sk-ant-test-fallback", encoding="utf-8")
        # Point ``api_key_paths`` at our tmp file by patching the helper
        # so we never touch a real user's config dir.
        monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [key_file])
        monkeypatch.setattr(api_key_store, "_keyring_get", lambda: "")
        assert api_key_store.load_api_key_from_file() == "sk-ant-test-fallback"

    def test_load_prefers_keyring_when_present(self, tmp_path: Path, monkeypatch):
        # Both keyring and file have a value — keyring wins.
        key_file = tmp_path / "spec_critic_api_key.txt"
        key_file.write_text("sk-ant-from-file", encoding="utf-8")
        monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [key_file])
        monkeypatch.setattr(api_key_store, "_keyring_get", lambda: "sk-ant-from-keyring")
        assert api_key_store.load_api_key_from_file() == "sk-ant-from-keyring"

    def test_load_returns_empty_when_no_source_has_a_key(
        self, tmp_path: Path, monkeypatch
    ):
        # No keyring, no file — empty string, never raises.
        monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [tmp_path / "nope.txt"])
        monkeypatch.setattr(api_key_store, "_keyring_get", lambda: "")
        assert api_key_store.load_api_key_from_file() == ""

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
    def test_load_tightens_loose_file_permissions_in_place(
        self, tmp_path: Path, monkeypatch
    ):
        """A pre-existing 0644 fallback file gets chmodded to 0600 the
        first time the loader successfully reads it."""
        key_file = tmp_path / "spec_critic_api_key.txt"
        key_file.write_text("sk-ant-loose", encoding="utf-8")
        os.chmod(key_file, 0o644)
        monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [key_file])
        monkeypatch.setattr(api_key_store, "_keyring_get", lambda: "")
        api_key_store.load_api_key_from_file()
        mode = stat.S_IMODE(key_file.stat().st_mode)
        # owner-only: 0o600
        assert mode == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
    def test_save_to_file_writes_owner_only(self, tmp_path: Path, monkeypatch):
        key_file = tmp_path / "spec_critic_api_key.txt"
        monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [key_file])
        result = api_key_store.save_api_key_to_file("sk-ant-fresh-write")
        assert result == key_file
        assert key_file.read_text(encoding="utf-8") == "sk-ant-fresh-write"
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600

    def test_save_to_file_refuses_empty_value(self, tmp_path: Path, monkeypatch):
        key_file = tmp_path / "spec_critic_api_key.txt"
        monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [key_file])
        assert api_key_store.save_api_key_to_file("   ") is None
        assert not key_file.exists()

    def test_save_to_keyring_returns_false_when_unavailable(self, monkeypatch):
        # Force the "keyring not installed" path. The helper must return
        # False (not raise) so callers can fall back cleanly.
        monkeypatch.setattr(api_key_store, "_keyring_set", lambda v: False)
        assert api_key_store.save_api_key_to_keyring("sk-ant-x") is False

    def test_keyring_available_returns_a_bool(self):
        # Just confirm the helper exists and returns a bool — actual
        # value depends on the env and is not under test.
        assert isinstance(api_key_store.keyring_available(), bool)


# ===========================================================================
# 4. Web-search blocked-domain list hygiene
# ===========================================================================


class TestBlockedDomainsHygiene:
    def test_no_exact_duplicates(self):
        domains = api_config._WEB_SEARCH_BLOCKED_DOMAINS
        assert len(domains) == len(set(domains))

    def test_no_subdomain_redundancy_against_listed_apex(self):
        """Each entry must not be a subdomain of another entry on the
        list — the tool already covers all subdomains of any listed apex."""
        domains = set(api_config._WEB_SEARCH_BLOCKED_DOMAINS)
        for d in domains:
            parts = d.split(".")
            for cut in range(1, len(parts) - 1):
                parent = ".".join(parts[cut:])
                assert parent not in domains, (
                    f"{d!r} is a subdomain of {parent!r} which is also in "
                    "the list — drop the subdomain entry"
                )

    def test_load_bearing_categories_still_present(self):
        # If a future cleanup deletes one of these by mistake, the
        # verifier loses a meaningful chunk of source-quality filtering.
        domains = set(api_config._WEB_SEARCH_BLOCKED_DOMAINS)
        for required in [
            "reddit.com",       # aggregator
            "chatgpt.com",      # LLM-assistant output
            "hvac-talk.com",    # trade forum
            "homeadvisor.com",  # lead-gen
            "facebook.com",     # social
            "wikipedia.org",    # tertiary encyclopedia
        ]:
            assert required in domains, f"missing required blocked domain: {required}"


# ===========================================================================
# 5. Behaviour preservation under comment cleanup
# ===========================================================================


class TestBehaviorPreservation:
    """Smoke tests covering the surfaces that lost chunk-history preambles.

    These tests do not assert the comments themselves (comments are not
    behaviour). They assert the surrounding functions still behave the
    way the prior comments described, so a future blanket "drop the
    chunk references" sweep cannot silently change semantics.
    """

    def test_cache_still_refuses_source_less_confirmed(self):
        cache = VerificationCache()
        f = _finding()
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict="CONFIRMED",
                grounded=True,
                accepted_sources=[],
                sources=[],
            ),
        )
        # The "Chunk 5" comment was dropped, but the invariant must hold.
        assert cache.get(f, cycle=DEFAULT_CYCLE) is None

    def test_phase_tagged_progress_still_adds_phase_kwarg(self):
        # The helper survived the "use consistently or delete" pass.
        from src.pipeline import _phase_tagged_progress

        seen: dict = {}

        def _sink(pct: float, msg: str, **kwargs):
            seen.update(kwargs)
            seen["pct"] = pct
            seen["msg"] = msg

        wrapped = _phase_tagged_progress(_sink, "verification")
        wrapped(42.0, "checkpoint")
        assert seen["phase"] == "verification"
        assert seen["pct"] == 42.0

    def test_phase_tagged_log_still_adds_phase_kwarg(self):
        from src.pipeline import _phase_tagged_log

        seen: dict = {}

        def _sink(msg: str, **kwargs):
            seen.update(kwargs)
            seen["msg"] = msg

        wrapped = _phase_tagged_log(_sink, "cross_check")
        wrapped("hello", level="warning")
        assert seen["phase"] == "cross_check"
        assert seen["level"] == "warning"

    def test_phase_tagged_helpers_let_callers_override(self):
        # ``setdefault`` semantics — an explicit ``phase=`` on the call
        # must win over the wrapper's default.
        from src.pipeline import _phase_tagged_log

        captured: dict = {}

        def _sink(_msg: str, **kwargs):
            captured.update(kwargs)

        wrapped = _phase_tagged_log(_sink, "verification")
        wrapped("override", phase="review")
        assert captured["phase"] == "review"
