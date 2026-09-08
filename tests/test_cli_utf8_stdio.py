"""UTF-8 stdout/stderr for the two console entry points.

``python -m src.tracing show`` prints status glyphs (``✓ ✎ ✗ ◆ — ⚠ ⚡``) and
``scripts/recover_batch.py`` prefixes its log lines with ``· ✓ ✗``. A legacy
Windows console, or stdout redirected to a file / pipe, hands Python a cp1252
stream on which ``print("✓")`` raises ``UnicodeEncodeError`` — for the
recovery tool that is *after* the batch was collected, so the report exists
but the run looks like a crash. Both entry points now reconfigure
``sys.stdout`` / ``sys.stderr`` to UTF-8 with ``errors="replace"`` at the top
of ``main`` — only when the stream exposes ``reconfigure`` (a ``TextIOWrapper``)
and never when it is ``None``.

The tests below drive each CLI's real print path with a strict cp1252
``TextIOWrapper`` standing in for the console: without the reconfiguration
the first glyph raises; with it the output arrives as UTF-8 bytes.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from src.batch.batch_runtime import BatchNotFinishedError
from src.tracing import cli as trace_cli

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RECOVER_SCRIPT = _REPO_ROOT / "scripts" / "recover_batch.py"


def _cp1252_stream() -> io.TextIOWrapper:
    """A strict cp1252 text stream over a byte buffer — a legacy console stand-in."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)


def _bytes_of(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()  # type: ignore[attr-defined]


def _install_cp1252_stdio(monkeypatch: pytest.MonkeyPatch) -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
    """Swap ``sys.stdout`` / ``sys.stderr`` for strict cp1252 streams.

    Called from the test body, not a fixture: pytest's capture manager
    re-installs its own ``sys.stdout`` when it resumes between fixture setup
    and the call phase, so a fixture-time swap would be silently undone.
    """
    out, err = _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def test_fixture_stream_really_rejects_the_glyphs() -> None:
    # Guard the guard: the stand-in must behave like a cp1252 console, or the
    # tests below would pass for the wrong reason.
    stream = _cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write("✓")
    stream.write("·")  # U+00B7 is in cp1252 — the middle-dot step marker never raised


# --------------------------------------------------------------------------
# python -m src.tracing show
# --------------------------------------------------------------------------


def _write_trace(root: Path, run_id: str) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": "batch",
                "model": "test-model",
                "cycle_label": "cycle",
                "started_at": 1_700_000_000.0,
                "ended_at": 1_700_000_042.5,
                "capture_level": "default",
                "files_reviewed": ["a.docx"],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "spans.jsonl").write_text(json.dumps({"kind": "review"}) + "\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    findings = [
        # ⚠ VERIFICATION_FAILED and ⚡ VERIFIED_CONTESTED are outside cp1252;
        # the NOT_CHECKED em dash is inside it, so the mix proves the whole line
        # survives, not just the encodable parts.
        {"severity": "HIGH", "section": "23 05 00", "issue": "failed one",
         "verification": {"verification_failed": True}},
        {"severity": "MEDIUM", "section": "23 05 00", "issue": "contested one",
         "verification": {"verdict": "CONFIRMED", "models_disagreed": True}},
        {"severity": "GRIPES", "section": "23 05 00", "issue": "unchecked one"},
    ]
    (run_dir / "findings.jsonl").write_text(
        "".join(json.dumps(f) + "\n" for f in findings), encoding="utf-8"
    )


def test_trace_show_glyphs_survive_a_cp1252_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, err = _install_cp1252_stdio(monkeypatch)
    _write_trace(tmp_path, "run-utf8")

    rc = trace_cli.main(["--trace-dir", str(tmp_path), "show", "run-utf8"])

    assert rc == 0
    assert out.encoding.lower().replace("-", "") == "utf8"
    assert err.encoding.lower().replace("-", "") == "utf8"
    text = _bytes_of(out).decode("utf-8")
    assert "⚠ [HIGH" in text
    assert "⚡ [MEDIUM" in text
    assert "— [GRIPES" in text
    assert "VERIFICATION_FAILED" in text and "VERIFIED_CONTESTED" in text


def test_trace_list_and_missing_run_paths_never_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, err = _install_cp1252_stdio(monkeypatch)
    _write_trace(tmp_path, "run-utf8")
    assert trace_cli.main(["--trace-dir", str(tmp_path), "list"]) == 0
    assert "run-utf8" in _bytes_of(out).decode("utf-8")
    # The error path writes to stderr — reconfigured too.
    assert trace_cli.main(["--trace-dir", str(tmp_path), "show", "nope"]) == 1
    assert "No trace found" in _bytes_of(err).decode("utf-8")


def test_configure_utf8_stdio_tolerates_none_and_non_reconfigurable_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A windowed build has sys.stdout None; a custom sink may lack reconfigure.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    trace_cli._configure_utf8_stdio()  # must not raise
    assert sys.stdout is None

    class Stubborn:
        def reconfigure(self, **_kw):
            raise io.UnsupportedOperation("nope")

    monkeypatch.setattr(sys, "stdout", Stubborn())
    trace_cli._configure_utf8_stdio()  # swallowed


# --------------------------------------------------------------------------
# scripts/recover_batch.py
# --------------------------------------------------------------------------


def _load_recovery_cli():
    spec = importlib.util.spec_from_file_location("recover_batch_utf8_under_test", _RECOVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_recover_batch_error_glyph_survives_a_cp1252_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = _load_recovery_cli()
    out, _err = _install_cp1252_stdio(monkeypatch)
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setattr(cli, "_ensure_api_key", lambda parser: None)

    def _still_running(parser, ns):
        raise BatchNotFinishedError("msgbatch_utf8", reason="max_elapsed")

    monkeypatch.setattr(cli, "_build_submission", _still_running)

    rc = cli.main(["--batch-id", "msgbatch_utf8", "--module", "california_k12_mep"])

    assert rc == 2
    assert out.encoding.lower().replace("-", "") == "utf8"
    text = _bytes_of(out).decode("utf-8")
    # The ✗ error line and the plain follow-up hint both made it out.
    assert " ✗ Batch msgbatch_utf8 has not finished processing" in text
    assert "Re-run this tool later" in text


def test_recover_batch_log_levels_all_encode(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_recovery_cli()
    out, _err = _install_cp1252_stdio(monkeypatch)
    with pytest.raises(UnicodeEncodeError):
        cli._log("unconfigured", level="success")  # the pre-fix failure mode
    cli._configure_utf8_stdio()
    for level in ("step", "info", "success", "warning", "error"):
        cli._log(f"{level} line", level=level)
    text = _bytes_of(out).decode("utf-8")
    assert " · step line" in text
    assert " ✓ success line" in text
    assert " ✗ error line" in text
