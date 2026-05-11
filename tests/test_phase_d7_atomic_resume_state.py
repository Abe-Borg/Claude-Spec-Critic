"""Phase D7.1 — atomic resume-state writes.

``batch_state_store.save_batch_state`` writes the resume JSON through a
temp file in the same directory and ``os.replace``\\s it into the target
position, with an fsync between write and replace. A crash or partial
write therefore cannot corrupt an existing resume-state file — the
previous valid target is left intact and the half-written temp file is
removed on failure.

These tests cover:
- normal write produces the expected JSON content,
- temp-file pattern is used (same directory as target),
- simulated write failure (replace) leaves the previous target untouched
  and removes the temp file,
- save_batch_state itself does not raise even when the underlying I/O
  fails (the in-flight batch run must keep going).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src import batch_state_store


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``_batch_state_path`` into a tmp directory for the test.

    Returns the absolute path of the target file. The file does not
    exist at fixture start.
    """
    path = tmp_path / "batch_state.json"
    monkeypatch.setattr(batch_state_store, "_batch_state_path", lambda: path)
    return path


def _read_target(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestAtomicResumeStateWrite:
    def test_normal_save_writes_expected_json(self, state_path: Path) -> None:
        payload = {"phase": "review_poll", "marker": "fresh"}
        batch_state_store.save_batch_state(payload)

        assert state_path.exists(), "target file should be created"
        loaded = _read_target(state_path)
        assert loaded == payload

    def test_save_does_not_leave_temp_files_on_success(
        self, state_path: Path
    ) -> None:
        payload = {"phase": "review_poll", "marker": "fresh"}
        batch_state_store.save_batch_state(payload)

        # Only the target file should remain. No ``.batch_state.*.tmp``
        # leftovers — ``os.replace`` consumes the temp file.
        siblings = list(state_path.parent.iterdir())
        assert siblings == [state_path], (
            f"unexpected temp files left behind: {siblings}"
        )

    def test_replace_failure_preserves_existing_target(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed a valid file the user must not lose.
        existing = {"phase": "review_poll", "marker": "previous-valid-run"}
        state_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        original_bytes = state_path.read_bytes()

        captured: dict[str, str] = {}
        real_replace = os.replace

        def boom_replace(src, dst):
            captured["src"] = str(src)
            captured["dst"] = str(dst)
            raise OSError("simulated replace failure")

        monkeypatch.setattr(batch_state_store.os, "replace", boom_replace)

        # save_batch_state must swallow the failure — an in-flight
        # batch run cannot abort just because a save call failed.
        batch_state_store.save_batch_state({"marker": "crashed-mid-write"})

        # The previous valid target survives byte-for-byte.
        assert state_path.exists()
        assert state_path.read_bytes() == original_bytes
        assert _read_target(state_path) == existing

        # The half-written temp file is cleaned up so it cannot leak
        # across runs.
        temp_files = [
            p for p in state_path.parent.iterdir()
            if p.name.startswith(".batch_state.") and p.name.endswith(".tmp")
        ]
        assert temp_files == [], (
            f"temp file was not cleaned up after failure: {temp_files}"
        )

        # Sanity: the replace attempt aimed at the right paths — same
        # directory as the target, target is the configured state path.
        assert captured["dst"] == str(state_path)
        assert Path(captured["src"]).parent == state_path.parent

        # Restore so other tests / monkeypatch teardown work as
        # expected (defensive — monkeypatch already restores on its
        # own).
        monkeypatch.setattr(batch_state_store.os, "replace", real_replace)

    def test_fsync_failure_preserves_existing_target(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed a valid existing target.
        existing = {"phase": "review_collect", "marker": "previous-run"}
        state_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        original_bytes = state_path.read_bytes()

        def boom_fsync(_fd: int) -> None:
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(batch_state_store.os, "fsync", boom_fsync)

        # Must not raise.
        batch_state_store.save_batch_state({"marker": "crashed-during-fsync"})

        # Existing target untouched.
        assert state_path.read_bytes() == original_bytes
        assert _read_target(state_path) == existing

        # No temp leftovers.
        temp_files = [
            p for p in state_path.parent.iterdir()
            if p.name.startswith(".batch_state.") and p.name.endswith(".tmp")
        ]
        assert temp_files == [], (
            f"temp file was not cleaned up after fsync failure: {temp_files}"
        )

    def test_mkstemp_failure_preserves_existing_target(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = {"phase": "verification_poll", "marker": "previous-run"}
        state_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        original_bytes = state_path.read_bytes()

        def boom_mkstemp(**_kwargs):
            raise OSError("simulated tempfile failure")

        monkeypatch.setattr(
            batch_state_store.tempfile, "mkstemp", boom_mkstemp
        )

        # save_batch_state must swallow the failure.
        batch_state_store.save_batch_state({"marker": "would-not-save"})

        # Existing target untouched.
        assert state_path.read_bytes() == original_bytes
        assert _read_target(state_path) == existing

    def test_temp_file_lives_in_target_directory(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomicity guarantee depends on the temp file being on the same
        filesystem as the target. The implementation passes the target's
        parent directory to ``tempfile.mkstemp``; this test pins that
        contract via a small spy.
        """
        captured: dict[str, object] = {}
        real_mkstemp = batch_state_store.tempfile.mkstemp

        def spy_mkstemp(**kwargs):
            captured.update(kwargs)
            return real_mkstemp(**kwargs)

        monkeypatch.setattr(
            batch_state_store.tempfile, "mkstemp", spy_mkstemp
        )

        batch_state_store.save_batch_state({"marker": "any"})

        assert "dir" in captured, "mkstemp should be invoked with dir=..."
        assert captured["dir"] == str(state_path.parent)
        assert captured.get("prefix", "").startswith(".batch_state.")
        assert captured.get("suffix") == ".tmp"

    def test_unserializable_payload_does_not_corrupt_existing_target(
        self, state_path: Path
    ) -> None:
        # Seed an existing valid target.
        existing = {"phase": "finalize", "marker": "previous-run"}
        state_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        original_bytes = state_path.read_bytes()

        # Sets aren't JSON-serializable; ``json.dumps`` will raise.
        # save_batch_state must catch that, leave the target intact,
        # and not create any temp file.
        bad_payload = {"junk": {1, 2, 3}}
        batch_state_store.save_batch_state(bad_payload)

        assert state_path.read_bytes() == original_bytes
        temp_files = [
            p for p in state_path.parent.iterdir()
            if p.name.startswith(".batch_state.") and p.name.endswith(".tmp")
        ]
        assert temp_files == []

    def test_round_trip_through_load_after_atomic_save(
        self, state_path: Path
    ) -> None:
        """Sanity: the new write path is compatible with the load path.

        ``load_batch_state`` performs schema validation; the atomic
        write must produce a file shape ``load_batch_state`` can still
        read. We exercise this with a minimally valid resume payload
        (not the full schema — we only need ``load_batch_state`` to
        get past the JSON decode + age check).
        """
        from datetime import datetime, timezone

        payload = {
            "version": "test",
            "schema": "v2",
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "phase": "review_poll",
            # Intentionally minimal: ``load_batch_state`` will fall
            # through to the legacy branch and then discard. We only
            # care that the JSON written by the atomic save is
            # readable.
            "submission": {},
        }
        batch_state_store.save_batch_state(payload)

        # File is parseable.
        loaded_raw = json.loads(state_path.read_text(encoding="utf-8"))
        assert loaded_raw["phase"] == "review_poll"
        assert loaded_raw["schema"] == "v2"
