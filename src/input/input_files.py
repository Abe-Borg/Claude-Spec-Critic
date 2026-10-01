"""When two supplied specification paths are the same input (plan WP-05).

A review identifies each specification by its bare file name: deterministic
alerts, the review request map, the repair lookup, program routing and
partitions, the report, and the edit sidecar all key on ``spec.filename``.
Two *different* files that share a name (``A/spec.docx`` and
``B/spec.docx``) would therefore be conflated, and which one a stage used
would depend on input order. The GUI refuses such a pair when files are
added, but that guard is only as universal as the GUI: every other entry
point (the headless pipeline, a routed program's preparation, a recovery)
checks here, before anything is paid for.

Names are compared case-insensitively: Windows cannot tell ``Spec.docx``
from ``spec.docx``, and the edit applier compares names the same way, so a
pair that differs only in case could never be applied safely either.

The same file supplied twice — the same path, another spelling of it, or a
hard link — is one input, not a collision: it is kept once, at its first
position.

Stdlib-only, so the pipeline, the program layer, and the GUI's file picker
share one rule.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def basename_key(name: str) -> str:
    """The form in which two file names are compared: case-folded."""
    return str(name).casefold()


def same_file_key(path: str | os.PathLike) -> tuple:
    """An identity for the file ``path`` names.

    The file's ``(device, inode)`` when it exists and the platform reports a
    real inode (a hard link, a symlink, or a case variant on a
    case-insensitive volume all share it); otherwise its resolved, case-
    normalized path, so paths that do not exist yet still compare sensibly.
    """
    candidate = Path(path)
    # Resolve first: ``dir/sub/../spec.docx`` names ``dir/spec.docx`` even
    # when ``sub`` does not exist, and must get that file's identity.
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        resolved = candidate
    try:
        status = resolved.stat()
    except OSError:
        status = None
    if status is not None and status.st_ino:
        return ("inode", status.st_dev, status.st_ino)
    return ("path", os.path.normcase(str(resolved)))


class BasenameCollisionError(ValueError):
    """Different files share a file name, so a review cannot tell them apart.

    ``collisions`` maps each shared name (as first supplied) to the distinct
    paths that carry it, in input order.
    """

    def __init__(self, collisions: dict[str, tuple[Path, ...]]):
        self.collisions = {name: tuple(paths) for name, paths in collisions.items()}
        described = "; ".join(
            f"{name!r} ({', '.join(str(path) for path in paths)})"
            for name, paths in self.collisions.items()
        )
        super().__init__(
            "Different specification files share a file name, and a review "
            "identifies each specification by its file name: "
            f"{described}. Rename one of them, or review them in separate runs."
        )


def unique_spec_inputs(paths: Iterable[str | os.PathLike]) -> list[Path]:
    """``paths`` as one input per file, refusing distinct files that share a name.

    Returns the paths in input order with repeats of the same file dropped
    (the first spelling is kept). Raises :class:`BasenameCollisionError`
    naming every shared name when two different files carry the same file
    name, compared with :func:`basename_key` — whatever order they came in.
    """
    kept: list[Path] = []
    seen_files: set[tuple] = set()
    by_name: dict[str, list[Path]] = {}
    for raw in paths:
        path = Path(raw)
        identity = same_file_key(path)
        if identity in seen_files:
            continue
        seen_files.add(identity)
        kept.append(path)
        by_name.setdefault(basename_key(path.name), []).append(path)
    collisions = {
        group[0].name: tuple(group) for group in by_name.values() if len(group) > 1
    }
    if collisions:
        raise BasenameCollisionError(collisions)
    return kept


__all__ = [
    "BasenameCollisionError",
    "basename_key",
    "same_file_key",
    "unique_spec_inputs",
]
