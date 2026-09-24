"""Command line for the edit applier.

    python -m applier REPORT.edits.json --specs ./specs

Defaults are the conservative ones on purpose: tracked changes, a policy that
withholds every finding a verifier did not settle, no model calls, and a new
file rather than an edit in place. Every way of making the run *less* careful
is an explicit flag, and every flag that spends money says so in its help.

Exit status: ``0`` the run finished, ``1`` it could not start (unreadable
sidecar, bad option, no specifications), ``2`` ``--strict`` and an instruction
could not be applied, ``3`` a document was held because its name matched
several different supplied files or its edited copy would overwrite a supplied
file. ``3`` does not need ``--strict``: the inputs are wrong, not the edits,
and the documents that were bound safely have still been processed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .assist import AssistConfig, AssistUnavailable, build_client
from .docx_edit import DIRECT, TRACKED
from .policy import Policy, PolicyConfig, parse_force_statuses
from .receipt import build_receipt, render_summary, write_receipt
from .run import DEFAULT_OUTPUT_SUFFIX, RunSettings, apply_sidecar
from .sidecar import SidecarError, load_sidecar

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAPPLIED = 2
#: A document was not processed because its binding or destination was
#: unsafe (``FILE_AMBIGUOUS`` / ``DESTINATION_CONFLICT``). Not gated on
#: ``--strict``.
EXIT_INPUT_HELD = 3

#: Outcomes that mean the invocation, not an edit, needs fixing.
_INPUT_HOLDS = ("FILE_AMBIGUOUS", "DESTINATION_CONFLICT")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m applier",
        description=(
            "Apply Spec Critic edit instructions to their specifications as "
            "Word tracked changes. Never edits a source file in place."
        ),
        epilog=(
            "Spec Critic emits edit instructions and does not apply them; "
            "this program applies them as revisions a human accepts or "
            "rejects in Word. It refuses rather than guesses. Exit status: "
            f"{EXIT_OK} finished, {EXIT_ERROR} could not start, "
            f"{EXIT_UNAPPLIED} --strict and something was not applied, "
            f"{EXIT_INPUT_HELD} a document was held because its name matched "
            "several supplied files or its copy would overwrite a supplied file."
        ),
    )
    parser.add_argument("sidecar", type=Path, help="A <report>.edits.json file.")
    parser.add_argument(
        "--specs",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "A .docx specification, or a directory of them. Repeatable. "
            "Defaults to the sidecar's own directory."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write edited copies (default: beside each source).",
    )
    parser.add_argument(
        "--output-suffix",
        default=DEFAULT_OUTPUT_SUFFIX,
        help=f"Suffix for edited copies (default: {DEFAULT_OUTPUT_SUFFIX}).",
    )
    parser.add_argument(
        "--mode",
        choices=("tracked", "direct"),
        default="tracked",
        help=(
            "tracked (default): write edits as Word revisions for a human to "
            "accept or reject. direct: write them in with no revision marks."
        ),
    )
    parser.add_argument(
        "--policy",
        choices=[member.value for member in Policy],
        default=Policy.CONSERVATIVE.value,
        help=(
            "Which findings may be applied. strict: only verifier-settled "
            "ones. conservative (default): adds locally-classified ones. "
            "all: adds findings no verdict was reached on. DISPUTED and "
            "VERIFIED_CONTESTED are excluded from every policy."
        ),
    )
    parser.add_argument(
        "--force-status",
        action="append",
        default=[],
        metavar="STATUS",
        help=(
            "Admit this report status regardless of policy, including "
            "DISPUTED / VERIFIED_CONTESTED. Repeatable. Read the report's "
            "evidence panel first."
        ),
    )
    parser.add_argument(
        "--min-edit-confidence",
        type=float,
        default=0.0,
        help=(
            "Withhold edits below this model self-rating (0.0-1.0, default "
            "0.0 = off). Note it is the review model's confidence in the "
            "edit, recorded before verification ran."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "Apply only these ids. Repeatable. A finding id (e.g. "
            "rf-1a2b3c4d5e6f) selects every place that finding applies; an "
            "occurrence id (oc-..., schema 6 and 7 sidecars) selects one place."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be applied, write nothing.",
    )
    parser.add_argument(
        "--allow-tracked-source",
        action="store_true",
        help=(
            "Proceed on a specification that already has pending tracked "
            "changes. Off by default — the review read its accept-all view. "
            "Individual edits whose target text sits inside an undecided "
            "revision are still refused."
        ),
    )
    parser.add_argument(
        "--assist",
        action="store_true",
        help=(
            "COSTS MONEY. On an ambiguous target, ask a model to choose among "
            "the matching elements. It may pick a location only, never write "
            "text, and a drifted target is still never applied."
        ),
    )
    parser.add_argument(
        "--assist-model",
        default=AssistConfig().model,
        help=f"Model for --assist (default: {AssistConfig().model}).",
    )
    parser.add_argument(
        "--assist-api-key",
        default=None,
        help=(
            "API key for --assist. Defaults to ANTHROPIC_API_KEY, then the "
            "key Spec Critic stored."
        ),
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help=(
            "Where to write the JSON receipt (default: "
            "<sidecar-stem>.applied.json beside the sidecar)."
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Print only the summary."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            f"Exit {EXIT_UNAPPLIED} when any instruction could not be applied "
            "(for CI). Policy holds do not count. An ambiguous or unsafe input "
            f"exits {EXIT_INPUT_HELD} with or without this flag."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _collect_specs(paths: list[Path], sidecar_path: Path) -> list[Path]:
    """Expand the --specs arguments into a de-duplicated list of .docx files.

    Mirrors ``pipeline._get_spec_files``: case-insensitive suffix match,
    Word's ``~$`` lock files skipped, de-duplicated by resolved path, and
    deterministically ordered — a run that silently skipped a lock file on
    one machine and picked it up on another would be very hard to debug.
    """
    roots = paths or [sidecar_path.parent]
    found: dict[Path, Path] = {}
    for root in roots:
        root = Path(root)
        if root.is_dir():
            candidates = sorted(root.iterdir())
        else:
            candidates = [root]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.casefold() != ".docx":
                continue
            if candidate.name.startswith("~$"):
                continue
            found.setdefault(candidate.resolve(), candidate)
    return [found[key] for key in sorted(found)]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = (lambda message: None) if args.quiet else (lambda message: print(message))

    try:
        sidecar = load_sidecar(args.sidecar)
    except SidecarError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        force_statuses = parse_force_statuses(args.force_status)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not 0.0 <= args.min_edit_confidence <= 1.0:
        print(
            "error: --min-edit-confidence must be between 0.0 and 1.0",
            file=sys.stderr,
        )
        return EXIT_ERROR

    client = None
    assist = AssistConfig(enabled=bool(args.assist), model=args.assist_model)
    if assist.enabled:
        try:
            client = build_client(args.assist_api_key)
        except AssistUnavailable as exc:
            print(f"error: --assist requested but unavailable: {exc}", file=sys.stderr)
            return EXIT_ERROR

    settings = RunSettings(
        mode=TRACKED if args.mode == "tracked" else DIRECT,
        policy=PolicyConfig(
            policy=Policy(args.policy),
            min_edit_confidence=args.min_edit_confidence,
            force_statuses=force_statuses,
            only_finding_ids=frozenset(args.only),
        ),
        assist=assist,
        dry_run=bool(args.dry_run),
        allow_tracked_source=bool(args.allow_tracked_source),
        output_dir=args.output_dir,
        output_suffix=args.output_suffix,
    )

    specs = _collect_specs(args.specs, Path(args.sidecar))
    if not specs:
        print(
            "error: no .docx specifications found. Pass --specs with a file "
            "or directory.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    log(f"{len(specs)} specification(s) available; {len(sidecar.entries)} instruction(s) to consider.")

    file_results = apply_sidecar(sidecar, specs, settings, client=client, log=log)
    receipt = build_receipt(
        sidecar=sidecar,
        file_results=file_results,
        settings=settings.to_dict(),
        entries_in=len(sidecar.entries) + len(sidecar.malformed),
    )
    receipt_path = args.receipt or Path(args.sidecar).with_name(
        Path(args.sidecar).stem + ".applied.json"
    )
    try:
        write_receipt(receipt, receipt_path)
    except OSError as exc:
        print(f"warning: could not write the receipt: {exc}", file=sys.stderr)
    else:
        receipt["receipt_path"] = str(receipt_path)

    print(render_summary(receipt, file_results))
    if not args.quiet:
        print(f"\n  receipt        {receipt_path}")

    counts = receipt["accounting"]["by_outcome"]
    held = sum(counts[status] for status in _INPUT_HOLDS)
    if held:
        print(
            f"error: {held} instruction(s) were held because their "
            "specification's name matched several different supplied files, "
            "or its edited copy would overwrite a supplied file. Nothing was "
            "guessed; see the reasons above.",
            file=sys.stderr,
        )
        return EXIT_INPUT_HELD
    # A DUPLICATE is not unapplied: the change it asks for was made once.
    unapplied = (
        counts["UNLOCATED"]
        + counts["FAILED"]
        + counts["MALFORMED"]
        + counts["FILE_MISSING"]
        + counts["EDIT_CONFLICT"]
    )
    if args.strict and unapplied:
        return EXIT_UNAPPLIED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
