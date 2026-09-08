"""Frozen-app entry point for the Windows PyInstaller build.

PyInstaller freezes a *script*, not a module, so this thin wrapper calls
``src.gui.gui.main``. It also adds two headless flags the release workflow
uses to smoke-test the frozen executable without opening a window:

    SpecCritic.exe --version     print the version and exit
    SpecCritic.exe --selfcheck   import the app's heavy modules — proving
                                 PyInstaller bundled every hidden import —
                                 then count one string with the app's
                                 tokenizer, and exit 0 (non-zero on any
                                 import error, a tokenizer failure, or a
                                 zero count)

Frozen-run environment (must happen BEFORE any ``src`` import): tiktoken does
not ship the ``cl100k_base`` rank file and downloads it at first use from a
host many corporate networks block. The spec bundles a pre-warmed copy at
``<bundle>/tiktoken_cache/`` (``bundle_assets.py``); ``configure_tiktoken_cache``
points ``TIKTOKEN_CACHE_DIR`` there when running frozen so the tokenizer reads
the bundled file and never fetches. Unfrozen runs (``python main.py``, the
tests) are untouched, and an operator-set ``TIKTOKEN_CACHE_DIR`` is respected.
Nothing from ``src`` (or the packaging helpers) is imported at module load —
every ``src`` import in this file sits inside a function that ``main`` reaches
only after the environment is configured.

OS trust store (frozen runs only, also before any ``src`` import): the SDK's
HTTP stack (``httpx2``) already builds its TLS context from the operating
system's certificate store through ``truststore``, but the self-updater
(``src/core/updates.py``) uses the standard library ``urllib``, which trusts
only ``certifi``'s bundle. ``configure_os_trust_store`` calls
``truststore.inject_into_ssl()`` so every ``ssl.SSLContext`` created afterwards
— including urllib's — also honours a certificate installed in the OS store
(a corporate TLS-intercepting proxy is the common case). It is best-effort: an
import or injection failure is logged and startup continues on ``certifi``.

The GUI build is windowed (``console=False``), so ``sys.stdout`` may be ``None``
in the frozen app; ``_emit`` writes results to the file named by
``SPEC_CRITIC_SELFCHECK_OUT`` (set by CI) as well as printing when it can,
so the smoke step can read the outcome regardless.
"""
from __future__ import annotations

import os
import sys
from typing import MutableMapping

# Bundle-relative folder the spec places the warmed tiktoken cache in. Pinned
# in lockstep with packaging/windows/bundle_assets.py::TIKTOKEN_CACHE_BUNDLE_DIR
# by tests/test_packaging_entry.py (duplicated rather than imported: this
# script must import nothing at module load).
TIKTOKEN_CACHE_BUNDLE_DIR = "tiktoken_cache"
TIKTOKEN_CACHE_DIR_ENV = "TIKTOKEN_CACHE_DIR"

# Short, deterministic text the self-check counts. Any non-empty English
# sentence yields a positive cl100k_base count.
SELFCHECK_PROBE_TEXT = "Spec Critic self-check: count these tokens."


def frozen_bundle_dir() -> str | None:
    """PyInstaller's bundle directory (``sys._MEIPASS``) when frozen, else ``None``.

    In a one-folder build ``_MEIPASS`` is ``<install dir>/_internal`` — the
    directory the spec's ``datas`` land in. The executable's own directory is
    the fallback only for a bootloader that did not set ``_MEIPASS``.
    """
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return str(meipass)
    return os.path.dirname(os.path.abspath(sys.executable))


def configure_tiktoken_cache(
    environ: MutableMapping[str, str] | None = None,
) -> str | None:
    """Point tiktoken at the bundled rank file when running frozen.

    Returns the cache directory in effect after the call: the bundled
    ``<_MEIPASS>/tiktoken_cache`` folder, or an operator's pre-existing
    ``TIKTOKEN_CACHE_DIR`` (``setdefault`` never overrides an explicit value).
    Returns ``None`` — and touches nothing — when not frozen.
    """
    env = os.environ if environ is None else environ
    base = frozen_bundle_dir()
    if base is None:
        return None
    env.setdefault(TIKTOKEN_CACHE_DIR_ENV, os.path.join(base, TIKTOKEN_CACHE_BUNDLE_DIR))
    return env[TIKTOKEN_CACHE_DIR_ENV]


def configure_os_trust_store() -> str:
    """Route stdlib TLS verification through the OS trust store when frozen.

    Returns a short status string for diagnostics: ``"injected"`` when
    ``truststore.inject_into_ssl()`` ran, ``"not-frozen"`` when nothing was
    done (source runs keep the interpreter's default), or ``"unavailable"``
    when the package is missing / injection failed — that case is logged at
    WARNING and never blocks startup. Idempotent: truststore itself tolerates
    a second injection.
    """
    if not getattr(sys, "frozen", False):
        return "not-frozen"
    try:
        import truststore

        truststore.inject_into_ssl()
        return "injected"
    except Exception as exc:  # noqa: BLE001 - startup must never depend on this
        try:
            import logging

            logging.getLogger("spec_critic.app_entry").warning(
                "OS trust store not enabled (truststore unavailable: %s); "
                "TLS verification falls back to the bundled certifi CA list.",
                exc,
            )
        except Exception:
            pass
        return "unavailable"


def _emit(message: str) -> None:
    try:
        if sys.stdout is not None:
            print(message)
    except Exception:
        pass
    out = os.environ.get("SPEC_CRITIC_SELFCHECK_OUT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except OSError:
            pass


def _print_version() -> int:
    import src

    _emit(src.__version__)
    return 0


def tokenizer_probe() -> dict[str, object]:
    """Count one short string with the app's tokenizer; report where it loaded from.

    ``rank_file_present`` is sampled BEFORE the count on purpose: the CI runner
    has network access, so a successful count alone cannot distinguish "read
    the bundled file" from "downloaded it". The smoke step asserts both a
    positive count and ``rank_file_present=True``.
    """
    from src.core import tokenizer

    status = tokenizer.encoder_cache_status()
    tokens = tokenizer.count_tokens(SELFCHECK_PROBE_TEXT)
    return {
        "encoding": tokenizer.ENCODING_NAME,
        "tokens": tokens,
        "rank_file_present": status.rank_file_present,
        "cache_dir": status.cache_dir,
    }


def format_tokenizer_probe(probe: dict[str, object]) -> str:
    """One parseable line: ``tokenizer: <enc> tokens=<n> rank_file_present=<bool> cache_dir=<dir>``.

    ``cache_dir`` goes last because a Windows path may contain spaces; the
    release workflow's smoke step regex-matches ``tokens=(\\d+)`` and
    ``rank_file_present=True``.
    """
    return (
        f"tokenizer: {probe['encoding']} tokens={probe['tokens']} "
        f"rank_file_present={probe['rank_file_present']} cache_dir={probe['cache_dir']}"
    )


def _selfcheck() -> int:
    try:
        import src
        from src.orchestration import pipeline  # noqa: F401 - proves the engine froze
        from src.core import updates  # noqa: F401 - proves the updater froze
        import src.gui.gui  # noqa: F401 - pulls customtkinter + tkinterdnd2
    except Exception:
        import traceback

        _emit("SELFCHECK FAILED:\n" + traceback.format_exc())
        return 1
    # An app that cannot count tokens never enables Run (the gauge stays
    # blank), so the probe is part of "the frozen app works", not an extra.
    try:
        probe = tokenizer_probe()
    except Exception:
        import traceback

        _emit("SELFCHECK FAILED: tokenizer probe\n" + traceback.format_exc())
        return 1
    if int(probe["tokens"]) <= 0:  # type: ignore[call-overload]
        _emit("SELFCHECK FAILED: tokenizer probe counted 0 tokens\n" + format_tokenizer_probe(probe))
        return 1
    _emit(f"SpecCritic {src.__version__} selfcheck ok\n" + format_tokenizer_probe(probe))
    return 0


def main(argv: list[str] | None = None) -> int:
    # First, before any ``src`` import: the tokenizer must find the bundled
    # rank file the moment it is first used.
    configure_tiktoken_cache()
    # Second, still before any ``src`` import and therefore before any network
    # use: TLS contexts created from here on trust the OS certificate store.
    configure_os_trust_store()
    args = sys.argv[1:] if argv is None else list(argv)
    if "--version" in args:
        return _print_version()
    if "--selfcheck" in args:
        return _selfcheck()
    from src.gui.gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
