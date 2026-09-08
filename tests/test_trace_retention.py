"""Automatic trace retention (B-29, tracing half) and CLI prune parity.

Tracing is on by default and every run stores full prompts per spec, so
``session.start_run_recorder`` now prunes old runs on every run start via
``retention.apply_startup_retention`` — the same selection/deletion code
``python -m src.tracing prune`` uses.

Covers:
    - env parsing: defaults, ``0`` disables, malformed / negative → defaults
    - retention by age, by count, and both at once (union)
    - the run being started always survives (even under clock skew)
    - a deletion failure never propagates (logged, others still deleted)
    - the startup hook never raises even when selection itself blows up
    - resume (``reattach_run_recorder``) and disabled tracing prune nothing
    - the CLI ``prune`` subcommand still behaves as before, through the
      shared functions (spied), including the confirmation prompt

All tests are hermetic — temp dirs only, no network, no real API key.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tracing import cli, retention
from src.tracing.config import (
    DEFAULT_TRACE_MAX_RUNS,
    DEFAULT_TRACE_RETENTION_DAYS,
    ENV_TRACE,
    ENV_TRACE_DEEP,
    ENV_TRACE_DIR,
    ENV_TRACE_MAX_RUNS,
    ENV_TRACE_RETENTION_DAYS,
    trace_max_runs,
    trace_retention_days,
)
from src.tracing.recorder import get_recorder, set_recorder
from src.tracing.session import (
    reattach_run_recorder,
    start_run_recorder,
    stop_run_recorder,
)

DAY = 86400.0
# Fixed clock for the pure selection tests so they never depend on wall time.
NOW = 1_800_000_000.0


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (ENV_TRACE, ENV_TRACE_DEEP, ENV_TRACE_DIR, ENV_TRACE_RETENTION_DAYS, ENV_TRACE_MAX_RUNS):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "traces"
    r.mkdir()
    return r


@pytest.fixture(autouse=True)
def _no_global_recorder():
    set_recorder(None)
    yield
    rec = get_recorder()
    if rec is not None:
        try:
            rec.stop()
        finally:
            set_recorder(None)


def _make_run(root: Path, run_id: str, started_at: float) -> Path:
    d = root / run_id
    d.mkdir()
    (d / "run.json").write_text(
        json.dumps({"run_id": run_id, "started_at": started_at, "mode": "batch"}),
        encoding="utf-8",
    )
    (d / "spans.jsonl").write_text('{"span_id": "s1"}\n', encoding="utf-8")
    return d


def _names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


# ---- env parsing --------------------------------------------------------
def test_env_defaults(clean_env: None) -> None:
    assert trace_retention_days() == DEFAULT_TRACE_RETENTION_DAYS == 30
    assert trace_max_runs() == DEFAULT_TRACE_MAX_RUNS == 50


@pytest.mark.parametrize("raw, expected", [("7", 7), (" 12 ", 12), ("0", 0), ("1000", 1000)])
def test_env_valid_values(clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, raw)
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, raw)
    assert trace_retention_days() == expected
    assert trace_max_runs() == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-3", "1.5", "30d", "None"])
def test_env_malformed_falls_back_to_defaults(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, raw)
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, raw)
    assert trace_retention_days() == DEFAULT_TRACE_RETENTION_DAYS
    assert trace_max_runs() == DEFAULT_TRACE_MAX_RUNS


# ---- pure selection -----------------------------------------------------
def test_select_by_age(root: Path) -> None:
    _make_run(root, "r40d", NOW - 40 * DAY)
    _make_run(root, "r31d", NOW - 31 * DAY)
    _make_run(root, "r29d", NOW - 29 * DAY)
    _make_run(root, "rnow", NOW)
    sel = retention.select_prune_candidates(root, older_than_seconds=30 * DAY, now=NOW)
    assert [d.name for d in sel.candidates] == ["r31d", "r40d"]  # newest-first
    assert [d.name for d in sel.kept] == ["rnow", "r29d"]
    assert sel.protected == ()


def test_select_by_count(root: Path) -> None:
    for i in range(6):
        _make_run(root, f"r{i}", NOW - i * 60)  # r0 newest
    sel = retention.select_prune_candidates(root, keep_last=3, now=NOW)
    assert [d.name for d in sel.candidates] == ["r3", "r4", "r5"]
    assert [d.name for d in sel.kept] == ["r0", "r1", "r2"]


def test_select_union_of_both_knobs(root: Path) -> None:
    _make_run(root, "old", NOW - 90 * DAY)      # doomed by age only
    for i in range(4):
        _make_run(root, f"r{i}", NOW - i * 60)  # r3 doomed by count only
    sel = retention.select_prune_candidates(root, keep_last=3, older_than_seconds=30 * DAY, now=NOW)
    assert sorted(d.name for d in sel.candidates) == ["old", "r3"]


def test_select_never_touches_dirs_without_run_json(root: Path) -> None:
    stray = root / "not_a_run"
    stray.mkdir()
    (stray / "spans.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "loose_file.txt").write_text("x", encoding="utf-8")
    _make_run(root, "ancient", NOW - 400 * DAY)
    result = retention.prune_trace_runs(root, keep_last=0, older_than_seconds=1, now=NOW)
    assert [d.name for d in result.deleted] == ["ancient"]
    assert _names(root) == ["loose_file.txt", "not_a_run"]


def test_select_falls_back_to_mtime_without_started_at(root: Path) -> None:
    d = root / "nometa"
    d.mkdir()
    (d / "run.json").write_text("{}", encoding="utf-8")
    assert retention.run_started_at(d) == pytest.approx(d.stat().st_mtime)
    broken = root / "broken"
    broken.mkdir()
    (broken / "run.json").write_text("{not json", encoding="utf-8")
    assert retention.load_run_meta(broken) is None
    assert retention.run_started_at(broken) == pytest.approx(broken.stat().st_mtime)


def test_protect_shields_by_path_and_name(root: Path) -> None:
    keep = _make_run(root, "current", NOW - 400 * DAY)  # ancient AND beyond count
    _make_run(root, "other", NOW)
    sel = retention.select_prune_candidates(
        root, keep_last=0, older_than_seconds=DAY, protect=(keep,), now=NOW
    )
    assert [d.name for d in sel.candidates] == ["other"]
    assert [d.name for d in sel.protected] == ["current"]
    assert keep in sel.kept


# ---- startup policy -----------------------------------------------------
def test_startup_prunes_by_age_with_defaults(clean_env: None, root: Path) -> None:
    _make_run(root, "r40d", NOW - 40 * DAY)
    _make_run(root, "r31d", NOW - 31 * DAY)
    _make_run(root, "r29d", NOW - 29 * DAY)
    cur = _make_run(root, "cur", NOW)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    assert sorted(d.name for d in result.deleted) == ["r31d", "r40d"]
    assert result.failed == ()
    assert _names(root) == ["cur", "r29d"]


def test_startup_prunes_by_count_with_defaults(clean_env: None, root: Path) -> None:
    for i in range(1, 56):  # 55 recent runs, r1 newest
        _make_run(root, f"r{i:02d}", NOW - i * 60)
    cur = _make_run(root, "cur", NOW)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    # 56 runs including the new one; default keeps the 50 most recent.
    assert sorted(d.name for d in result.deleted) == [f"r{i}" for i in range(50, 56)]
    remaining = _names(root)
    assert len(remaining) == 50
    assert "cur" in remaining and "r01" in remaining and "r49" in remaining


def test_startup_never_deletes_the_run_being_started(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Even under clock skew (the new run looks ancient) and max_runs=1,
    the run being started survives."""
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, "1")
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, "1")
    cur = _make_run(root, "cur", NOW - 500 * DAY)
    _make_run(root, "newest", NOW)
    _make_run(root, "old", NOW - 10 * DAY)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    assert [d.name for d in result.deleted] == ["old"]
    assert [d.name for d in result.selection.protected] == ["cur"]
    assert _names(root) == ["cur", "newest"]


def test_zero_disables_age_only(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, "0")
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, "3")
    _make_run(root, "ancient", NOW - 900 * DAY)
    _make_run(root, "r1", NOW - 60)
    _make_run(root, "r2", NOW - 120)
    cur = _make_run(root, "cur", NOW)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    # Count limit still applies (3 kept incl. current); age limit is off, so
    # 'ancient' only goes because it is the 4th most recent, not by age.
    assert [d.name for d in result.deleted] == ["ancient"]
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, "10")
    _make_run(root, "ancient2", NOW - 900 * DAY)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None and result.deleted == ()
    assert "ancient2" in _names(root)


def test_zero_disables_count_only(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, "0")
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, "7")
    for i in range(1, 80):  # far beyond the default 50 — all recent
        _make_run(root, f"r{i:02d}", NOW - i * 60)
    _make_run(root, "stale", NOW - 8 * DAY)
    cur = _make_run(root, "cur", NOW)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    assert [d.name for d in result.deleted] == ["stale"]
    assert len(_names(root)) == 80


def test_both_zero_disables_retention_entirely(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, "0")
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, "0")
    _make_run(root, "ancient", NOW - 900 * DAY)
    cur = _make_run(root, "cur", NOW)
    # Disabled means "don't even list the directory".
    monkeypatch.setattr(retention, "iter_run_dirs", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("listed")))
    assert retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW) is None
    assert _names(root) == ["ancient", "cur"]


def test_malformed_env_uses_defaults_in_startup_path(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_MAX_RUNS, "lots")
    monkeypatch.setenv(ENV_TRACE_RETENTION_DAYS, "-5")
    _make_run(root, "r40d", NOW - 40 * DAY)   # beyond the default 30 days
    for i in range(1, 53):                     # 52 recent + current = 53 > 50
        _make_run(root, f"r{i:02d}", NOW - i * 60)
    cur = _make_run(root, "cur", NOW)
    result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    assert sorted(d.name for d in result.deleted) == ["r40d", "r50", "r51", "r52"]
    assert len(_names(root)) == 50


def test_deletion_failure_does_not_propagate(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    a = _make_run(root, "old_a", NOW - 40 * DAY)
    b = _make_run(root, "old_b", NOW - 41 * DAY)
    cur = _make_run(root, "cur", NOW)
    real_rmtree = retention.shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path).name == "old_a":
            raise PermissionError("locked by another process")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(retention, "shutil", SimpleNamespace(rmtree=flaky_rmtree))
    with caplog.at_level(logging.WARNING, logger="src.tracing.retention"):
        result = retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW)
    assert result is not None
    assert [d.name for d in result.deleted] == ["old_b"]
    assert [(d.name, "PermissionError" in reason) for d, reason in result.failed] == [("old_a", True)]
    assert a.exists() and not b.exists() and cur.exists()
    assert "could not delete" in caplog.text and "old_a" in caplog.text


def test_startup_retention_never_raises(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cur = _make_run(root, "cur", NOW)

    def boom(*_a, **_k):
        raise RuntimeError("listing exploded")

    monkeypatch.setattr(retention, "select_prune_candidates", boom)
    with caplog.at_level(logging.WARNING, logger="src.tracing.retention"):
        assert retention.apply_startup_retention(current_run_dir=cur, root=root, now=NOW) is None
    assert "Trace retention skipped" in caplog.text
    assert cur.exists()


# ---- wiring through session.start_run_recorder ---------------------------
def test_start_run_recorder_prunes_older_runs(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_DIR, str(root))
    now = time.time()
    _make_run(root, "old_a", now - 40 * DAY)
    _make_run(root, "old_b", now - 31 * DAY)
    _make_run(root, "recent", now - 60)
    rec = start_run_recorder(run_id="fresh01", mode="realtime", model="m", cycle_label="c", files=[])
    assert rec is not None
    try:
        assert rec.trace_dir == root / "fresh01"
        assert (rec.trace_dir / "run.json").exists()
        assert _names(root) == ["fresh01", "recent"]
    finally:
        stop_run_recorder(rec)
    assert get_recorder() is None


def test_start_run_recorder_survives_retention_failure(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    """A retention blow-up must never cost the operator the run's recorder."""
    from src.tracing import session

    monkeypatch.setenv(ENV_TRACE_DIR, str(root))
    _make_run(root, "old_a", time.time() - 40 * DAY)

    def boom(**_k):
        raise RuntimeError("must be swallowed upstream")

    # apply_startup_retention itself swallows everything; pin that the
    # session wiring is the only call site and stays on the never-raise path.
    monkeypatch.setattr(session, "apply_startup_retention", retention.apply_startup_retention)
    monkeypatch.setattr(retention, "prune_trace_runs", boom)
    rec = start_run_recorder(run_id="fresh02", mode="realtime", model="m", cycle_label="c", files=[])
    assert rec is not None
    try:
        assert get_recorder() is rec
        assert (root / "old_a").exists()  # nothing pruned, nothing raised
    finally:
        stop_run_recorder(rec)


def test_disabled_tracing_prunes_nothing(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_DIR, str(root))
    monkeypatch.setenv(ENV_TRACE, "0")
    _make_run(root, "old_a", time.time() - 400 * DAY)
    assert start_run_recorder(run_id="off1", mode="realtime", model="m", cycle_label="c", files=[]) is None
    assert _names(root) == ["old_a"]


def test_reattach_does_not_prune(clean_env: None, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv(ENV_TRACE_DIR, str(root))
    _make_run(root, "old_a", time.time() - 400 * DAY)
    resumed = _make_run(root, "resumed", time.time() - 3600)
    rec = reattach_run_recorder({"run_id": "resumed", "trace_dir": str(resumed), "capture_level": "default"})
    assert rec is not None
    try:
        assert _names(root) == ["old_a", "resumed"]
    finally:
        stop_run_recorder(rec)


# ---- CLI parity ---------------------------------------------------------
def test_cli_prune_keep_last_through_shared_functions(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = time.time()
    for i in range(5):
        _make_run(root, f"r{i}", now - i * 60)
    calls: list[dict] = []
    real_select = retention.select_prune_candidates

    def spy_select(root_arg, **kwargs):
        calls.append(kwargs)
        return real_select(root_arg, **kwargs)

    monkeypatch.setattr(cli, "select_prune_candidates", spy_select)
    rc = cli.main(["--trace-dir", str(root), "prune", "--keep-last", "2", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [{"keep_last": 2}]
    assert "Will delete 3 trace directories:" in out
    assert "  r2\n  r3\n  r4\n" in out          # newest-first, as before
    assert "Deleted 3 trace directories." in out
    assert _names(root) == ["r0", "r1"]


def test_cli_prune_older_than_through_shared_functions(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = time.time()
    _make_run(root, "old", now - 40 * DAY)
    _make_run(root, "fresh", now - 60)
    calls: list[dict] = []
    real_select = retention.select_prune_candidates

    def spy_select(root_arg, **kwargs):
        calls.append(kwargs)
        return real_select(root_arg, **kwargs)

    monkeypatch.setattr(cli, "select_prune_candidates", spy_select)
    rc = cli.main(["--trace-dir", str(root), "prune", "--older-than", "30d", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [{"older_than_seconds": 30 * DAY}]
    assert "Will delete 1 trace directory:" in out
    assert "Deleted 1 trace directory." in out
    assert _names(root) == ["fresh"]


def test_cli_prune_nothing_to_prune_and_no_traces(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--trace-dir", str(root), "prune", "--keep-last", "1"]) == 0
    assert "No traces found" in capsys.readouterr().out
    _make_run(root, "only", time.time())
    assert cli.main(["--trace-dir", str(root), "prune", "--keep-last", "5"]) == 0
    assert "Nothing to prune." in capsys.readouterr().out
    assert _names(root) == ["only"]


def test_cli_prune_prompt_abort_deletes_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = time.time()
    _make_run(root, "a", now)
    _make_run(root, "b", now - 60)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert cli.main(["--trace-dir", str(root), "prune", "--keep-last", "1"]) == 0
    assert "Aborted." in capsys.readouterr().out
    assert _names(root) == ["a", "b"]
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    assert cli.main(["--trace-dir", str(root), "prune", "--keep-last", "1"]) == 0
    assert _names(root) == ["a"]


def test_cli_prune_reports_deletion_failure(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = time.time()
    _make_run(root, "a", now)
    _make_run(root, "b", now - 60)
    monkeypatch.setattr(
        retention, "shutil",
        SimpleNamespace(rmtree=lambda *_a, **_k: (_ for _ in ()).throw(OSError("busy"))),
    )
    rc = cli.main(["--trace-dir", str(root), "prune", "--keep-last", "1", "--yes"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "could not delete b" in captured.err
    assert "Deleted 0 trace directories." in captured.out
    assert _names(root) == ["a", "b"]


def test_cli_list_and_show_still_load_runs(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``list`` / ``show`` moved onto the shared run-dir helpers; keep them green."""
    _make_run(root, "abc123", time.time())
    assert cli.main(["--trace-dir", str(root), "list"]) == 0
    assert "abc123" in capsys.readouterr().out
    assert cli.main(["--trace-dir", str(root), "show", "abc123"]) == 0
    assert "Run abc123" in capsys.readouterr().out
