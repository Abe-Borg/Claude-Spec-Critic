"""Synthetic credentials must not survive into any diagnostics or trace artifact.

See CLAUDE.md, "Test Harness" — the secret-redaction bullet. Three
demonstrated bypasses, each of which let a credential through a scrubber that
was already redacting the identical value elsewhere in the same structure:

1. ``tracing.redaction.scrub_data`` and ``diagnostics._scrub_and_bound`` both
   returned ``repr(data)`` past their six-level recursion bound. ``repr`` of a
   nested container renders every value inside it verbatim, so the deeper a
   credential sat, the *less* protected it was — exactly inverting the intent.
2. ``DiagnosticsReport.log`` scrubbed the structured ``data`` payload and left
   ``message`` untouched, so an exception rendered into the message wrote the
   credential out in full beside a redacted copy of itself.
3. ``TraceRecorder.prompt_ref`` wrote prompt text to ``prompts.jsonl`` raw, and
   returned it raw inline in deep mode, while ``record_finding_snapshot``
   directly below it already routed its payload through ``scrub_data``.

**Every credential in this module is fabricated** and matches the shape the
patterns look for, nothing more. No real key appears here or in any fixture.

**What these tests do not claim.** Scrubbing is credential-shaped pattern
matching against a fixed set of prefixes (``sk-ant-``, ``Bearer ``, ``AKIA``).
It is not a general secret detector, and spec content is captured in full by
design. A green run here means these three paths no longer leak a
*recognized* credential shape — not that a trace is safe to publish. The
existing traces on disk are also unchanged: this fixes the writer, it does not
retroactively clean anything already written.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile

import pytest

from src.orchestration.diagnostics import (
    _MAX_DEPTH_MARKER,
    _REDACTED,
    DiagnosticsReport,
    _scrub_and_bound,
)
from src.tracing.config import LEVEL_DEEP, LEVEL_DEFAULT
from src.tracing.recorder import TraceRecorder
from src.tracing.redaction import scrub_data

# Fabricated credentials, shaped to match each production pattern. Never real.
FAKE_ANTHROPIC_KEY = "sk-ant-api03-NOTAREALKEYNOTAREALKEYNOTAREALKEY0123456789"
FAKE_BEARER = "Bearer NOTAREALTOKEN0123456789abcdefgh"
FAKE_AWS_KEY = "AKIA0123456789ABCDEF"

ALL_FAKE_SECRETS = (FAKE_ANTHROPIC_KEY, FAKE_BEARER, FAKE_AWS_KEY)


def _nest(value, depth: int) -> dict:
    """Wrap ``value`` in ``depth`` levels of single-key dicts."""
    out = value
    for i in reversed(range(depth)):
        out = {f"level{i}": out}
    return out


def _contains_any_secret(blob: str) -> str | None:
    """Return the first fabricated secret found in ``blob``, or None."""
    for secret in ALL_FAKE_SECRETS:
        if secret in blob:
            return secret
    return None


class TestDepthBoundIsNotABypass:
    """Past the recursion bound, a container must not be rendered by ``repr``."""

    @pytest.mark.parametrize("scrubber", [scrub_data, _scrub_and_bound], ids=["tracing", "diagnostics"])
    @pytest.mark.parametrize("depth", [0, 3, 6, 7, 12, 40], ids=lambda d: f"depth{d}")
    @pytest.mark.parametrize("secret", ALL_FAKE_SECRETS, ids=["anthropic", "bearer", "aws"])
    def test_no_secret_survives_at_any_nesting_level(self, scrubber, depth, secret):
        """The regression: depth 7+ used to leak what depth 0 redacted."""
        payload = _nest({"api_key": secret}, depth)
        assert _contains_any_secret(repr(scrubber(payload))) is None

    @pytest.mark.parametrize("scrubber", [scrub_data, _scrub_and_bound], ids=["tracing", "diagnostics"])
    def test_a_scalar_past_the_bound_is_still_scrubbed(self, scrubber):
        """A scalar cannot recurse, so it is scrubbed rather than discarded.

        Dropping it would be the easy fix and the wrong one: it would throw
        away redact-able strings and numeric telemetry to solve a recursion
        problem that scalars do not have.
        """
        deep_scalar = _nest(FAKE_ANTHROPIC_KEY, 7)
        rendered = repr(scrubber(deep_scalar))
        assert FAKE_ANTHROPIC_KEY not in rendered
        assert _REDACTED in rendered

    @pytest.mark.parametrize("scrubber", [scrub_data, _scrub_and_bound], ids=["tracing", "diagnostics"])
    def test_numeric_telemetry_survives_past_the_bound(self, scrubber):
        out = scrubber(_nest(4242, 7))
        for i in range(7):
            out = out[f"level{i}"]
        assert out == 4242

    @pytest.mark.parametrize("scrubber", [scrub_data, _scrub_and_bound], ids=["tracing", "diagnostics"])
    def test_siblings_are_retained_when_one_branch_is_marked(self, scrubber):
        """Only the over-deep container collapses; its siblings are untouched."""
        payload = _nest(
            {"nested": {"x": 1}, "count": 7, "label": "ok", "api_key": FAKE_AWS_KEY},
            6,
        )
        out = scrubber(payload)
        for i in range(6):
            out = out[f"level{i}"]
        assert out["nested"] == _MAX_DEPTH_MARKER
        assert out["count"] == 7
        assert out["label"] == "ok"
        assert out["api_key"] == _REDACTED

    @pytest.mark.parametrize("scrubber", [scrub_data, _scrub_and_bound], ids=["tracing", "diagnostics"])
    def test_a_cyclic_structure_still_terminates(self, scrubber):
        """The bound's original purpose must survive the fix."""
        cycle: dict = {"name": "root"}
        cycle["self"] = cycle
        assert scrubber(cycle) is not None  # completes rather than recursing forever

    @pytest.mark.parametrize("scrubber", [scrub_data, _scrub_and_bound], ids=["tracing", "diagnostics"])
    def test_shallow_structures_are_unchanged(self, scrubber):
        """The common path keeps its existing output."""
        payload = {"phase": "review", "count": 3, "files": ["a.docx", "b.docx"]}
        assert scrubber(payload) == payload


class TestDiagnosticsMessageIsScrubbed:
    """``log`` scrubs the message on the same terms as the structured payload."""

    @pytest.mark.parametrize("secret", ALL_FAKE_SECRETS, ids=["anthropic", "bearer", "aws"])
    def test_a_credential_in_the_message_does_not_survive(self, secret):
        report = DiagnosticsReport()
        report.log("verification", "error", f"Request failed: {secret}", {"api_key": secret})
        event = report.events[-1]
        assert _contains_any_secret(event.message) is None
        assert _contains_any_secret(repr(event.data)) is None

    def test_an_ordinary_message_is_byte_identical(self):
        """The common case must not change — most messages carry no credential."""
        report = DiagnosticsReport()
        message = "Rate limited (429), retrying in 5s — attempt 2 of 3"
        report.log("verification", "warning", message)
        assert report.events[-1].message == message

    def test_unicode_messages_survive(self):
        report = DiagnosticsReport()
        message = "Spec 21 05 00 — vérification échouée ✓"
        report.log("review", "info", message)
        assert report.events[-1].message == message

    def test_a_credential_message_collapses_whole(self):
        """C4: conservative whole-value replacement, substring preservation is
        out of scope. The diagnostic context around the credential is lost —
        that is the accepted cost, and it is paid only by messages that would
        otherwise have leaked."""
        report = DiagnosticsReport()
        report.log("p", "error", f"auth failed: {FAKE_ANTHROPIC_KEY}")
        assert report.events[-1].message == _REDACTED

    def test_a_non_string_message_does_not_crash(self):
        report = DiagnosticsReport()
        report.log("p", "info", None)  # type: ignore[arg-type]
        assert report.events[-1].message is None

    def test_other_diagnostics_survive_a_scrubbed_event(self):
        """Scrubbing one message must not disturb the surrounding report."""
        report = DiagnosticsReport()
        report.log("a", "info", "first message")
        report.log("b", "error", f"leaked {FAKE_BEARER}")
        report.log("c", "info", "third message")
        assert [e.message for e in report.events] == [
            "first message",
            _REDACTED,
            "third message",
        ]
        assert [e.phase for e in report.events] == ["a", "b", "c"]


class TestPromptCaptureIsScrubbed:
    """``prompt_ref`` scrubs before storing and before returning inline."""

    @staticmethod
    def _recorder(level: str):
        directory = pathlib.Path(tempfile.mkdtemp())
        return TraceRecorder(run_id="r", trace_dir=directory, capture_level=level), directory

    @pytest.mark.parametrize("secret", ALL_FAKE_SECRETS, ids=["anthropic", "bearer", "aws"])
    def test_default_mode_writes_no_secret_to_prompts_jsonl(self, secret):
        recorder, directory = self._recorder(LEVEL_DEFAULT)
        recorder.start()
        recorder.prompt_ref("review", f"Project context: {secret}")
        recorder.stop()
        prompts = directory / "prompts.jsonl"
        body = prompts.read_text(encoding="utf-8") if prompts.exists() else ""
        assert _contains_any_secret(body) is None

    @pytest.mark.parametrize("secret", ALL_FAKE_SECRETS, ids=["anthropic", "bearer", "aws"])
    def test_deep_mode_returns_no_secret_inline(self, secret):
        recorder, _ = self._recorder(LEVEL_DEEP)
        recorder.start()
        ref = recorder.prompt_ref("review", f"Project context: {secret}")
        recorder.stop()
        assert _contains_any_secret(json.dumps(ref)) is None

    def test_a_clean_prompt_keeps_its_existing_hash(self):
        """The reference is computed from the stored content.

        A secret-free prompt scrubs to itself, so its digest is unchanged and
        every previously written reference still resolves. If the digest were
        taken before scrubbing instead, a stored-but-redacted prompt would
        carry a reference to content that is not on disk.
        """
        recorder, directory = self._recorder(LEVEL_DEFAULT)
        recorder.start()
        text = "Review this spec section 21 05 00."
        ref = recorder.prompt_ref("review", text)
        recorder.stop()
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        assert ref["ref"] == expected
        assert text in (directory / "prompts.jsonl").read_text(encoding="utf-8")

    def test_a_scrubbed_prompts_reference_matches_what_was_stored(self):
        recorder, directory = self._recorder(LEVEL_DEFAULT)
        recorder.start()
        ref = recorder.prompt_ref("review", f"context: {FAKE_ANTHROPIC_KEY}")
        recorder.stop()
        rows = [
            json.loads(line)
            for line in (directory / "prompts.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        stored = next(r for r in rows if r["hash"] == ref["ref"])
        assert stored["text"] == _REDACTED
        assert ref["ref"] == hashlib.sha256(_REDACTED.encode("utf-8")).hexdigest()[:24]

    def test_repeated_safe_prompts_still_deduplicate(self):
        """Deduplication is keyed on the digest, which must not have moved."""
        recorder, directory = self._recorder(LEVEL_DEFAULT)
        recorder.start()
        text = "Identical prompt body."
        first = recorder.prompt_ref("review", text)
        second = recorder.prompt_ref("review", text)
        recorder.stop()
        assert first["ref"] == second["ref"]
        rows = [
            line
            for line in (directory / "prompts.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1


class TestNoSecretReachesAnyTraceFile:
    """The end-to-end claim, under the real recorder lifecycle.

    Each individual fix is pinned above; this asserts the property that
    actually matters — that nothing lands in *any* trace file — at both
    capture levels, so a future writer added to the recorder is covered by at
    least this sweep even if its own test is forgotten.
    """

    @pytest.mark.parametrize("level", [LEVEL_DEFAULT, LEVEL_DEEP], ids=["default", "deep"])
    def test_no_trace_file_contains_a_synthetic_secret(self, level):
        directory = pathlib.Path(tempfile.mkdtemp())
        recorder = TraceRecorder(run_id="sweep", trace_dir=directory, capture_level=level)
        recorder.start(mode="batch", model="test-model")
        recorder.prompt_ref("review", f"prompt body {FAKE_ANTHROPIC_KEY}")
        recorder.add_event(
            None,
            "api_call",
            phase="review",
            headers={"authorization": FAKE_BEARER},
            nested=_nest({"api_key": FAKE_AWS_KEY}, 9),
        )
        recorder.stop()

        written = sorted(p for p in directory.rglob("*") if p.is_file())
        assert written, "recorder produced no files; the sweep would be vacuous"
        for path in written:
            body = path.read_text(encoding="utf-8", errors="replace")
            found = _contains_any_secret(body)
            assert found is None, f"{path.name} contains a synthetic secret: {found[:16]}…"
