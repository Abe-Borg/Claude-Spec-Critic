"""CLI for inspecting and pruning agent traces.

Usage:
    python -m src.tracing show <run_id> [--trace-dir DIR]
    python -m src.tracing list [--trace-dir DIR]
    python -m src.tracing prune [--keep-last N | --older-than 30d] [--trace-dir DIR] [--yes]

``show`` prints a finding-by-finding summary to stdout for quick triage
without opening the HTML viewer. ``list`` enumerates available runs.
``prune`` deletes old trace directories — ``--keep-last N`` keeps the N
most recent, ``--older-than`` deletes anything older than a duration
(e.g. ``30d``, ``12h``).

All reads are local JSONL; no network, no Anthropic SDK import.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from .config import default_trace_root


# Mirror report_status.classify_status so the CLI labels match the report
# and the HTML viewer without importing the (heavier) report module.
_STATUS_GLYPHS = {
    "VERIFIED_SUPPORTED": "✓",
    "VERIFIED_CONTRADICTED": "✎",
    "DISPUTED": "✗",
    "INSUFFICIENT_EVIDENCE": "?",
    "LOCALLY_CLASSIFIED": "◆",
    "NOT_CHECKED": "—",
    "MANUAL_REVIEW_REQUIRED": "!",
    "VERIFICATION_FAILED": "⚠",
    "VERIFIED_CONTESTED": "⚡",
}


def _classify_status(finding: dict) -> str:
    v = finding.get("verification")
    if not v:
        return "NOT_CHECKED"
    if v.get("verification_failed"):
        return "VERIFICATION_FAILED"
    if v.get("models_disagreed"):
        return "VERIFIED_CONTESTED"
    if v.get("cache_status") == "local_skip":
        return "LOCALLY_CLASSIFIED"
    verdict = (v.get("verdict") or "").strip().upper()
    grounded = bool(v.get("grounded"))
    has_accepted = bool(v.get("accepted_sources") or v.get("sources"))
    if verdict == "CONFIRMED" and grounded and has_accepted:
        return "VERIFIED_SUPPORTED"
    if verdict == "CORRECTED" and grounded and has_accepted:
        return "VERIFIED_CONTRADICTED"
    if verdict == "DISPUTED":
        return "DISPUTED"
    return "INSUFFICIENT_EVIDENCE"


def _parse_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_run(trace_dir: Path) -> dict | None:
    run_path = trace_dir / "run.json"
    if not run_path.exists():
        return None
    try:
        return json.loads(run_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _iter_run_dirs(root: Path):
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "run.json").exists():
            yield child


def _parse_duration(text: str) -> float:
    """Parse a duration like ``30d`` / ``12h`` / ``90m`` into seconds."""
    text = text.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    # Bare number → days (the most common pruning unit).
    return float(text) * 86400


# ---- commands ----------------------------------------------------------
def cmd_list(args) -> int:
    root = Path(args.trace_dir) if args.trace_dir else default_trace_root()
    runs = list(_iter_run_dirs(root))
    if not runs:
        print(f"No traces found under {root}")
        return 0
    print(f"{'RUN ID':<16} {'MODE':<10} {'WHEN':<20} {'SPANS':>6} {'FINDINGS':>9}")
    for d in runs:
        run = _load_run(d) or {}
        started = run.get("started_at")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
        n_spans = len(_parse_jsonl(d / "spans.jsonl"))
        n_find = len(_parse_jsonl(d / "findings.jsonl"))
        print(f"{run.get('run_id', d.name):<16} {run.get('mode', '?'):<10} {when:<20} {n_spans:>6} {n_find:>9}")
    return 0


def _resolve_run_dir(root: Path, run_id: str) -> Path | None:
    """Find a run by directory name first, then by the run_id in run.json.

    In production the directory name == run_id, but a copied/renamed trace
    folder can diverge, so fall back to matching the embedded run_id.
    """
    direct = root / run_id
    if (direct / "run.json").exists():
        return direct
    for d in _iter_run_dirs(root):
        run = _load_run(d)
        if run and run.get("run_id") == run_id:
            return d
    return None


def cmd_show(args) -> int:
    root = Path(args.trace_dir) if args.trace_dir else default_trace_root()
    trace_dir = _resolve_run_dir(root, args.run_id)
    run = _load_run(trace_dir) if trace_dir else None
    if run is None:
        print(f"No trace found for run_id '{args.run_id}' under {root}", file=sys.stderr)
        return 1

    started = run.get("started_at")
    ended = run.get("ended_at")
    dur = f"{ended - started:.1f}s" if (started and ended) else "running/unknown"
    print(f"Run {run.get('run_id')}  ({run.get('mode')} · {run.get('model')} · {run.get('cycle_label')})")
    print(f"  started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)) if started else '?'} · duration {dur} · capture={run.get('capture_level')}")
    print(f"  files: {', '.join(run.get('files_reviewed', []) or []) or '(none)'}")

    spans = _parse_jsonl(trace_dir / "spans.jsonl")
    events = _parse_jsonl(trace_dir / "events.jsonl")
    findings = _parse_jsonl(trace_dir / "findings.jsonl")

    # Span-kind histogram.
    kind_counts: dict[str, int] = {}
    for s in spans:
        kind_counts[s.get("kind", "?")] = kind_counts.get(s.get("kind", "?"), 0) + 1
    print(f"\n  spans: {len(spans)} ({', '.join(f'{k}×{n}' for k, n in sorted(kind_counts.items()))})")
    print(f"  events: {len(events)}")

    # Finding-by-finding summary — the headline of the command.
    print(f"\nFindings ({len(findings)}):")
    if not findings:
        print("  (none — an incomplete run may have no terminal snapshots)")
    for f in findings:
        status = _classify_status(f)
        glyph = _STATUS_GLYPHS.get(status, "?")
        sev = f.get("severity", "?")
        section = f.get("section", "")
        issue = (f.get("issue", "") or "").replace("\n", " ")
        if len(issue) > 80:
            issue = issue[:77] + "..."
        print(f"  {glyph} [{sev:<8}] {section:<10} {status}")
        print(f"      {issue}")
        v = f.get("verification") or {}
        bits = []
        if v.get("verdict"):
            bits.append(f"verdict={v['verdict']}")
        if v.get("web_search_requests"):
            bits.append(f"searches={v['web_search_requests']}")
        if v.get("web_fetch_requests"):
            bits.append(f"fetches={v['web_fetch_requests']}")
        if v.get("models_disagreed"):
            bits.append("models_disagreed")
        if v.get("budget_exhausted"):
            bits.append("budget_exhausted")
        if v.get("verification_failed"):
            bits.append("verification_failed")
        if bits:
            print(f"      ({' · '.join(bits)})")
    return 0


def cmd_prune(args) -> int:
    root = Path(args.trace_dir) if args.trace_dir else default_trace_root()
    runs = list(_iter_run_dirs(root))
    if not runs:
        print(f"No traces found under {root}")
        return 0

    # Sort newest-first by started_at (fall back to mtime).
    def _started(d: Path) -> float:
        run = _load_run(d) or {}
        return run.get("started_at") or d.stat().st_mtime

    runs.sort(key=_started, reverse=True)

    to_delete: list[Path] = []
    if args.keep_last is not None:
        to_delete = runs[args.keep_last:]
    elif args.older_than is not None:
        cutoff = time.time() - _parse_duration(args.older_than)
        to_delete = [d for d in runs if _started(d) < cutoff]
    else:
        print("Specify --keep-last N or --older-than DURATION", file=sys.stderr)
        return 2

    if not to_delete:
        print("Nothing to prune.")
        return 0

    print(f"Will delete {len(to_delete)} trace director{'y' if len(to_delete)==1 else 'ies'}:")
    for d in to_delete:
        print(f"  {d.name}")
    if not args.yes:
        reply = input("Proceed? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0
    for d in to_delete:
        shutil.rmtree(d, ignore_errors=True)
    print(f"Deleted {len(to_delete)} trace director{'y' if len(to_delete)==1 else 'ies'}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.tracing", description="Inspect and prune agent traces.")
    parser.add_argument("--trace-dir", help="Override the trace root (default: ~/.spec_critic/traces)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List available trace runs")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Print a finding-by-finding summary for one run")
    p_show.add_argument("run_id", help="The run_id (trace subdirectory name)")
    p_show.set_defaults(func=cmd_show)

    p_prune = sub.add_parser("prune", help="Delete old trace directories")
    g = p_prune.add_mutually_exclusive_group()
    g.add_argument("--keep-last", type=int, metavar="N", help="Keep the N most recent runs")
    g.add_argument("--older-than", metavar="DURATION", help="Delete runs older than e.g. 30d / 12h")
    p_prune.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p_prune.set_defaults(func=cmd_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
