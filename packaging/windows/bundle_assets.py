"""Build-time asset discovery for the PyInstaller spec — importable, unit-tested.

Why this exists: the tiktoken wheel does not ship the ``cl100k_base`` BPE rank
file. tiktoken downloads it at first use from a public Azure blob host, which
corporate networks commonly block even when api.anthropic.com is allowed — on
such a workstation the frozen app's token analysis raised, the gauge stayed
blank, and the Run button never enabled. The fix is to ship the file inside
the installer and point ``TIKTOKEN_CACHE_DIR`` at it; this module is the piece
that decides *what* ships:

* ``release.yml`` (or a local build) first warms a build-local cache
  directory — ``TIKTOKEN_CACHE_DIR=<dir> python -c "import tiktoken;
  tiktoken.get_encoding('cl100k_base')"`` — and names it in
  ``SPEC_CRITIC_TIKTOKEN_CACHE_SRC``.
* ``spec-critic.spec`` calls :func:`tiktoken_cache_datas`, which lists every
  file in that directory as a PyInstaller ``datas`` entry destined for the
  fixed bundle folder :data:`TIKTOKEN_CACHE_BUNDLE_DIR`
  (``dist/SpecCritic/_internal/tiktoken_cache/``).
* ``app_entry.py`` points ``TIKTOKEN_CACHE_DIR`` at that folder when frozen,
  before any ``src`` import. The folder name is duplicated there on purpose
  (the entry script imports nothing at module load) and pinned in lockstep by
  ``tests/test_packaging_entry.py``.

The helper is deliberately strict: a missing directory, an empty one, one that
lacks the ``cl100k_base`` rank file, or a rank file whose SHA-256 does not
match the hash tiktoken checks at runtime all raise :class:`BundleAssetError`
and fail the build. An installer without a usable rank file must never ship
silently — that would reproduce the exact first-use download this exists to
prevent.

Run directly (``python packaging/windows/bundle_assets.py``) to validate the
warmed cache and list what would be bundled; exit status 1 on any problem.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys
from typing import Mapping

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    # The spec is exec'd by PyInstaller from the repo root with ``src``
    # pip-installed, and the tests insert the root themselves; this keeps the
    # helper importable from any working directory (e.g. a manual build).
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.tokenizer import (  # noqa: E402 - path setup above
    CL100K_BASE_SHA256,
    ENCODING_NAME,
    TIKTOKEN_CACHE_DIR_ENV,
    cl100k_cache_filename,
)

#: Env var naming the pre-warmed tiktoken cache directory to bundle.
TIKTOKEN_CACHE_SRC_ENV = "SPEC_CRITIC_TIKTOKEN_CACHE_SRC"
#: Where the warm step puts the cache when the env var is unset. ``build/`` is
#: gitignored, and ``pyinstaller --clean`` removes only ``build/<specname>/``
#: (its own workpath), so a locally warmed cache survives a clean rebuild.
DEFAULT_TIKTOKEN_CACHE_SRC = _REPO_ROOT / "build" / "tiktoken_cache"
#: Bundle-relative folder the files land in (under ``_internal/`` in a
#: one-folder build, i.e. ``sys._MEIPASS`` at runtime).
TIKTOKEN_CACHE_BUNDLE_DIR = "tiktoken_cache"


class BundleAssetError(RuntimeError):
    """A required build asset is missing or invalid; the build must stop."""


def _warm_hint(src: pathlib.Path) -> str:
    return (
        f"Warm it first — {TIKTOKEN_CACHE_DIR_ENV}={src} python -c "
        f"\"import tiktoken; tiktoken.get_encoding('{ENCODING_NAME}')\" — or "
        f"point {TIKTOKEN_CACHE_SRC_ENV} at a directory that already holds the "
        f"{ENCODING_NAME} rank file."
    )


def resolve_tiktoken_cache_src(
    src_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> pathlib.Path:
    """Pick the cache source: explicit argument → env var → repo default."""
    if src_dir is not None:
        return pathlib.Path(src_dir)
    env = os.environ if environ is None else environ
    configured = env.get(TIKTOKEN_CACHE_SRC_ENV, "").strip()
    if configured:
        return pathlib.Path(os.path.expandvars(os.path.expanduser(configured)))
    return DEFAULT_TIKTOKEN_CACHE_SRC


def tiktoken_cache_datas(
    src_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    expected_sha256: str | None = CL100K_BASE_SHA256,
) -> list[tuple[str, str]]:
    """Return PyInstaller ``datas`` entries for every file in the warmed cache.

    Each entry is ``(absolute source path, TIKTOKEN_CACHE_BUNDLE_DIR)`` — the
    two-tuple shape ``Analysis(datas=...)`` takes. Files are sorted for a
    reproducible spec; tiktoken's ``*.tmp`` write-in-progress leftovers are
    excluded.

    ``expected_sha256`` is the digest tiktoken itself verifies at runtime (a
    mismatched cached file is deleted and re-downloaded — precisely the
    behavior being prevented), so a corrupt or truncated rank file fails the
    build here. Pass ``None`` to skip the content check (tests only).

    Raises:
        BundleAssetError: directory missing / not a directory / empty / no
            ``cl100k_base`` rank file / rank file hash mismatch. The message
            names ``SPEC_CRITIC_TIKTOKEN_CACHE_SRC`` and how to warm the cache.
    """
    src = resolve_tiktoken_cache_src(src_dir, environ=environ)
    origin = f"{TIKTOKEN_CACHE_SRC_ENV} (or its default {DEFAULT_TIKTOKEN_CACHE_SRC})"
    if not src.exists():
        raise BundleAssetError(
            f"tiktoken cache source directory {src} does not exist — {origin} "
            f"must name a directory containing the {ENCODING_NAME} rank file. "
            f"{_warm_hint(src)}"
        )
    if not src.is_dir():
        raise BundleAssetError(
            f"tiktoken cache source {src} is not a directory ({origin})."
        )
    files = sorted(
        p for p in src.iterdir() if p.is_file() and not p.name.endswith(".tmp")
    )
    if not files:
        raise BundleAssetError(
            f"tiktoken cache source directory {src} is empty — an installer "
            f"built from it would download the {ENCODING_NAME} rank file at "
            f"first use ({origin}). {_warm_hint(src)}"
        )
    rank_name = cl100k_cache_filename()
    rank_file = src / rank_name
    if rank_file not in files:
        raise BundleAssetError(
            f"tiktoken cache source directory {src} holds {len(files)} file(s) "
            f"but not the {ENCODING_NAME} rank file {rank_name} (tiktoken names "
            f"it sha1(<download URL>)); {origin}. {_warm_hint(src)}"
        )
    if expected_sha256 is not None:
        digest = hashlib.sha256(rank_file.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise BundleAssetError(
                f"{rank_file} is not the {ENCODING_NAME} rank file tiktoken "
                f"expects (sha256 {digest} != {expected_sha256}); tiktoken would "
                f"reject it at runtime and re-download. Delete it and warm the "
                f"cache again. {_warm_hint(src)}"
            )
    return [(str(p.resolve()), TIKTOKEN_CACHE_BUNDLE_DIR) for p in files]


def main(argv: list[str] | None = None) -> int:
    """Validate the warmed cache and print the entries the spec would bundle."""
    src_dir = argv[0] if argv else None
    try:
        entries = tiktoken_cache_datas(src_dir)
    except BundleAssetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for source, dest in entries:
        print(f"{source} -> {dest}/")
    print(f"tiktoken cache OK: {len(entries)} file(s) will be bundled under {TIKTOKEN_CACHE_BUNDLE_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
