"""Third-party license notices for the Windows build — importable, unit-tested.

Why this exists: the README's License section says a bundled binary
distribution must carry every bundled package's license text, and
``SpecCriticSetup.exe`` did not. The PyInstaller build bundles the Python
interpreter, Tcl/Tk, and every runtime dependency with its transitive
dependencies, but the only license files that reached ``dist/SpecCritic/`` were
the ones inside the few ``.dist-info`` folders the spec copies for runtime
metadata reads, and nothing carried the interpreter's or Tcl/Tk's. This module
writes one ``THIRD-PARTY-NOTICES.txt`` holding all of them; ``installer.iss``
installs it beside the app.

What counts as bundled is decided by the build, not by what happens to be
installed on the build machine:

* ``spec-critic.spec`` calls :func:`write_third_party_notices` right after
  ``Analysis``, passing every table of files the analysis collected
  (``a.pure``, ``a.scripts``, ``a.binaries``, ``a.datas``). A distribution is
  bundled when one of those source files is listed in its ``RECORD``, so the
  notices name what ships: PyInstaller's own build-time helpers (altgraph,
  pefile, pip, ...) are left out unless a file of theirs is collected.
  PyInstaller itself is always listed, because its bootloader is
  ``SpecCritic.exe``. Spec Critic's own distribution is never listed; its
  license ships as ``LICENSE.txt``.
* Each bundled distribution contributes the license files it ships: the ones
  its metadata declares (``License-File``), any other LICENSE / LICENCE /
  COPYING / NOTICE / COPYRIGHT file in its ``.dist-info``, and license-named
  files elsewhere in its ``RECORD`` (third-party code vendored inside the
  package). A multi-line ``License`` metadata field, which some packages use
  for the full text, counts when no file does.
* The interpreter contributes ``LICENSE.txt`` from its install directory
  (``sys.base_prefix``), reproduced whole. python.org's Windows build writes
  that file by appending the terms of the libraries it ships to the PSF
  license: the MSVC runtime notice, bzip2, libffi, OpenSSL, Tcl, Tk, and Tix
  (CPython's ``PCbuild/regen.targets``).
* Tcl and Tk contribute ``license.terms`` from the library directories
  PyInstaller bundles (``PyInstaller.utils.hooks.tcl_tk.tcltk_info``). The
  python.org install has that file for Tk only (``tcl\\tk8.6``), so the Tcl
  terms are taken from the interpreter's ``LICENSE.txt``, which holds them
  verbatim.

The module is deliberately strict, like ``bundle_assets.py``: a bundled
distribution with no license text, an interpreter without its license file,
or Tcl/Tk terms that cannot be found raise :class:`NoticesError` and fail the
build, naming every gap at once, and no notices file is written. An installer
that silently leaves a license out must never ship. When a package really
ships no license text, the remedy is a release that does, or the text, taken
from that project's source at exactly that version, saved as
``third_party_licenses/<name>-<version>.txt`` next to this file. The version in
the file name makes an upgrade fail again until someone checks the new text.

Run directly (``python packaging/windows/third_party_notices.py``) to check,
before the long PyInstaller build, that this environment's interpreter and
Tcl/Tk license texts can be found; exit status 1 on any problem. Which
distributions are bundled is known only after the analysis, so that check runs
inside the spec.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import platform
import re
import sys
import sysconfig
from importlib import metadata
from typing import Iterable, Sequence

#: The notices document. The spec writes it into PyInstaller's ``dist/``
#: folder, next to (not inside) the one-folder build, and ``installer.iss``
#: installs it beside ``SpecCritic.exe``.
NOTICES_FILENAME = "THIRD-PARTY-NOTICES.txt"

#: Listed whatever the analysis collected: PyInstaller's bootloader *is*
#: ``SpecCritic.exe``, and it is not a file of the analysis tables.
ALWAYS_BUNDLED = ("pyinstaller",)
#: Never listed: Spec Critic's own license ships as ``LICENSE.txt``.
FIRST_PARTY = ("spec-critic",)

#: License texts for bundled packages that ship none, one file per release:
#: ``<canonical name>-<version>.txt``. Empty today; see the module docstring.
LICENSE_OVERRIDES_DIR = pathlib.Path(__file__).resolve().parent / "third_party_licenses"

#: Heading every CPython license file carries; checked so that some other
#: ``LICENSE.txt`` in the install directory is never passed off as Python's.
PSF_LICENSE_MARKER = "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2"

#: Tcl 8.6's and Tk 8.6's license terms open by naming their copyright
#: holders, and the two lists differ only by Apple Inc.; both end with the same
#: sentence. Matched with any whitespace between words, because the
#: interpreter's ``LICENSE.txt`` has CRLF line endings.
TCL_TERMS_OPENING = (
    "This software is copyrighted by the Regents of the University of "
    "California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState "
    "Corporation and other parties."
)
TK_TERMS_OPENING = (
    "This software is copyrighted by the Regents of the University of "
    "California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState "
    "Corporation, Apple Inc. and other parties."
)
TCL_TK_TERMS_CLOSING = "terms specified in this license."

_LICENSE_NAME_RE = re.compile(r"^(licen[cs]e|copying|notice|copyright)", re.IGNORECASE)
# A module named license.py (or a compiled one) is code, not a license text.
_NOT_TEXT_SUFFIXES = frozenset(
    {".py", ".pyc", ".pyo", ".pyi", ".pyd", ".so", ".dll", ".dylib", ".exe"}
)
_TCL_TK_DIR_RE = re.compile(r"^(?:tcl|tk)(\d+(?:\.\d+)*)$", re.IGNORECASE)
_RULE = "=" * 78


class NoticesError(RuntimeError):
    """A bundled component's license text cannot be found; the build must stop."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__(
            "The third-party notices are incomplete, so the build stops "
            "(README, License: a bundled binary distribution must carry every "
            "bundled package's license text):\n"
            + "\n".join(f"  - {p}" for p in self.problems)
        )


@dataclasses.dataclass(frozen=True)
class LicenseText:
    """One license text and where it came from, as the notices show it."""

    source: str
    text: str


@dataclasses.dataclass(frozen=True)
class Component:
    """A bundled component: its name, version, license, and full texts."""

    name: str
    version: str
    license: str
    texts: tuple[LicenseText, ...]
    note: str = ""

    @property
    def title(self) -> str:
        return f"{self.name} {self.version}".strip()


def canonical_name(name: str) -> str:
    """PEP 503 normalization: ``Jaraco.Classes`` and ``jaraco_classes`` match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _read_text(path: str | os.PathLike[str]) -> str | None:
    """File text, decoded and with LF line endings; None when blank or unreadable.

    Leading blank lines and trailing whitespace are dropped; everything else is
    kept as written (a centered heading keeps its indentation). A file that is
    not UTF-8 is read as Latin-1, which decodes any byte, rather than dropped.
    """
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n").rstrip()
    return text or None


def _flexible(phrase: str) -> str:
    """Regex for ``phrase`` with any run of whitespace between its words."""
    return r"\s+".join(re.escape(word) for word in phrase.split())


# ---------------------------------------------------------------------------
# Which distributions are bundled
# ---------------------------------------------------------------------------


def source_paths(tables: Iterable[Iterable[Sequence[object]]]) -> list[str]:
    """Source paths from PyInstaller's ``(dest, source, typecode)`` tables."""
    paths = []
    for table in tables:
        for entry in table:
            if len(entry) >= 2 and isinstance(entry[1], str) and entry[1]:
                paths.append(entry[1])
    return paths


def bundled_distributions(
    sources: Iterable[str],
    distributions: Iterable[metadata.Distribution] | None = None,
    *,
    always: Sequence[str] = ALWAYS_BUNDLED,
    first_party: Sequence[str] = FIRST_PARTY,
) -> list[metadata.Distribution]:
    """The installed distributions that own at least one bundled source file.

    A file is owned by the distribution whose ``RECORD`` lists it. When a name
    is installed twice on the path, the first copy wins, as it does for
    imports. ``always`` is added whatever was collected (it must be
    installed), ``first_party`` is removed. Sorted by canonical name.
    """
    dists: dict[str, metadata.Distribution] = {}
    owners: dict[str, str] = {}
    for dist in metadata.distributions() if distributions is None else distributions:
        name = dist.metadata.get("Name")
        if not name:
            continue
        key = canonical_name(name)
        if key in dists:
            continue
        dists[key] = dist
        for file in dist.files or ():
            owners.setdefault(_normalize_path(dist.locate_file(file)), key)

    bundled = {owners[p] for p in map(_normalize_path, sources) if p in owners}
    missing = [n for n in always if canonical_name(n) not in dists]
    if missing:
        raise NoticesError(
            [
                f"{n} is not installed in this environment, but its code is always "
                "bundled (PyInstaller's bootloader is SpecCritic.exe)"
                for n in missing
            ]
        )
    bundled |= {canonical_name(n) for n in always}
    bundled -= {canonical_name(n) for n in first_party}
    return [dists[key] for key in sorted(bundled)]


# ---------------------------------------------------------------------------
# A distribution's license
# ---------------------------------------------------------------------------


def license_identifier(dist: metadata.Distribution) -> str:
    """The license a distribution declares: expression, short field, or classifiers."""
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    field = (meta.get("License") or "").strip()
    if field and "\n" not in field and len(field) <= 200 and field.upper() != "UNKNOWN":
        return field
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or ()
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    return "not declared in its package metadata (see the text below)"


def _metadata_dir(files: Sequence[metadata.PackagePath]) -> str | None:
    for file in files:
        parts = file.parts
        if len(parts) >= 2 and (
            (parts[-1] == "METADATA" and parts[-2].endswith(".dist-info"))
            or (parts[-1] == "PKG-INFO" and parts[-2].endswith(".egg-info"))
        ):
            return "/".join(parts[:-1])
    return None


def _is_license_name(file: metadata.PackagePath) -> bool:
    name = file.parts[-1] if file.parts else ""
    return (
        bool(_LICENSE_NAME_RE.match(name))
        and os.path.splitext(name)[1].lower() not in _NOT_TEXT_SUFFIXES
        and "__pycache__" not in file.parts
    )


def distribution_license_texts(
    dist: metadata.Distribution,
    *,
    overrides_dir: str | os.PathLike[str] | None = LICENSE_OVERRIDES_DIR,
) -> tuple[LicenseText, ...]:
    """Every license text ``dist`` ships, in order; empty when it ships none.

    Order: the files its ``License-File`` metadata declares (under
    ``.dist-info/licenses/`` in metadata 2.4, directly in ``.dist-info`` in
    older wheels), then any other license-named file in its ``.dist-info``,
    then license-named files elsewhere in its ``RECORD`` (vendored code). With
    none of those, a multi-line ``License`` field, and after that an override
    file from ``overrides_dir``.
    """
    files = list(dist.files or ())
    by_path = {file.as_posix(): file for file in files}
    meta_dir = _metadata_dir(files)

    chosen: list[metadata.PackagePath] = []
    if meta_dir is not None:
        for declared in dist.metadata.get_all("License-File") or ():
            for candidate in (f"{meta_dir}/licenses/{declared}", f"{meta_dir}/{declared}"):
                file = by_path.get(candidate)
                if file is not None:
                    if file not in chosen:
                        chosen.append(file)
                    break
    own, vendored = [], []
    for file in sorted(files, key=lambda f: f.as_posix()):
        if file in chosen or not _is_license_name(file):
            continue
        in_meta = meta_dir is not None and file.as_posix().startswith(meta_dir + "/")
        (own if in_meta else vendored).append(file)

    texts = []
    for file in [*chosen, *own, *vendored]:
        text = _read_text(dist.locate_file(file))
        if text:
            texts.append(LicenseText(file.as_posix(), text))
    if texts:
        return tuple(texts)

    field = (dist.metadata.get("License") or "").strip()
    if len([line for line in field.splitlines() if line.strip()]) >= 3:
        return (LicenseText("the License field of its package metadata", field),)

    if overrides_dir is not None:
        name = canonical_name(dist.metadata.get("Name") or "")
        override = pathlib.Path(overrides_dir) / f"{name}-{dist.version}.txt"
        text = _read_text(override)
        if text:
            return (
                LicenseText(
                    f"packaging/windows/third_party_licenses/{override.name} "
                    "(supplied with Spec Critic; the package ships no license file)",
                    text,
                ),
            )
    return ()


def distribution_components(
    dists: Iterable[metadata.Distribution],
    *,
    overrides_dir: str | os.PathLike[str] | None = LICENSE_OVERRIDES_DIR,
) -> list[Component]:
    """One component per distribution; raises naming every one without a text."""
    components, missing = [], []
    for dist in dists:
        name = dist.metadata.get("Name") or "?"
        texts = distribution_license_texts(dist, overrides_dir=overrides_dir)
        if not texts:
            missing.append(f"{name} {dist.version}")
            continue
        components.append(Component(name, dist.version, license_identifier(dist), texts))
    if missing:
        where = (
            pathlib.Path(overrides_dir) / "<name>-<version>.txt"
            if overrides_dir is not None
            else "packaging/windows/third_party_licenses/<name>-<version>.txt"
        )
        raise NoticesError(
            [
                f"{m} is bundled but ships no license text (no license file in its "
                "RECORD and no full text in its License metadata). Upgrade to a "
                "release that ships one, or save the text from that project's "
                f"source at exactly this version as {where}"
                for m in missing
            ]
        )
    return components


# ---------------------------------------------------------------------------
# The interpreter and Tcl/Tk
# ---------------------------------------------------------------------------


def interpreter_license_path(
    base_prefix: str | os.PathLike[str] | None = None,
    stdlib_dir: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """The license file of the interpreter being bundled.

    ``LICENSE.txt`` in ``sys.base_prefix`` is where python.org's Windows
    install keeps it (and the base prefix, not a virtualenv's, is the install
    PyInstaller copies the interpreter from); the standard-library directory is
    where POSIX builds keep it.
    """
    base = pathlib.Path(sys.base_prefix if base_prefix is None else base_prefix)
    stdlib = pathlib.Path(sysconfig.get_path("stdlib") if stdlib_dir is None else stdlib_dir)
    candidates = [base / "LICENSE.txt", base / "LICENSE", stdlib / "LICENSE.txt"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise NoticesError(
        [
            "the interpreter's license file was not found (looked for "
            + ", ".join(str(c) for c in candidates)
            + ")"
        ]
    )


def _tcl_tk_version(library_dir: pathlib.Path) -> str:
    match = _TCL_TK_DIR_RE.match(library_dir.name)
    return match.group(1) if match else ""


def _terms(
    label: str,
    library_dir: pathlib.Path,
    opening: str,
    interpreter_text: str | None,
) -> LicenseText | None:
    """``license.terms`` in the library directory, else the block in Python's license."""
    text = _read_text(library_dir / "license.terms")
    if text:
        return LicenseText(f"license.terms in the bundled {library_dir.name} library directory", text)
    if interpreter_text:
        pattern = _flexible(opening) + r".*?" + _flexible(TCL_TK_TERMS_CLOSING)
        match = re.search(pattern, interpreter_text, re.DOTALL)
        if match:
            return LicenseText(
                f"the {label} license terms in the interpreter's LICENSE.txt "
                f"(the {library_dir.name} library directory has no license file of its own)",
                match.group(0),
            )
    return None


def base_components(
    *,
    tcl_data_dir: str | os.PathLike[str] | None,
    tk_data_dir: str | os.PathLike[str] | None,
    interpreter_license: str | os.PathLike[str] | None = None,
    python_version: str | None = None,
) -> list[Component]:
    """The interpreter, Tcl, and Tk components; raises naming every gap."""
    problems: list[str] = []
    interpreter_text = None
    try:
        path = (
            interpreter_license_path()
            if interpreter_license is None
            else pathlib.Path(interpreter_license)
        )
    except NoticesError as exc:
        problems += exc.problems
    else:
        interpreter_text = _read_text(path)
        if not interpreter_text:
            problems.append(f"the interpreter's license file {path} is empty or unreadable")
            interpreter_text = None
        elif PSF_LICENSE_MARKER not in interpreter_text:
            problems.append(
                f"{path} is not the Python license (it lacks {PSF_LICENSE_MARKER!r})"
            )
            interpreter_text = None

    components = []
    if interpreter_text:
        components.append(
            Component(
                "Python",
                python_version or platform.python_version(),
                "PSF-2.0",
                (LicenseText("LICENSE.txt from the interpreter's install directory", interpreter_text),),
                note="the bundled interpreter",
            )
        )

    for label, library, opening in (
        ("Tcl", tcl_data_dir, TCL_TERMS_OPENING),
        ("Tk", tk_data_dir, TK_TERMS_OPENING),
    ):
        if library is None:
            problems.append(
                f"the {label} library directory is unknown (PyInstaller found no "
                "usable tkinter), so its license terms cannot be located"
            )
            continue
        library_dir = pathlib.Path(library)
        terms = _terms(label, library_dir, opening, interpreter_text)
        if terms is None:
            problems.append(
                f"the {label} license terms were found neither in "
                f"{library_dir / 'license.terms'} nor in the interpreter's license file"
            )
            continue
        components.append(
            Component(
                label,
                _tcl_tk_version(library_dir),
                "TCL (the Tcl/Tk license)",
                (terms,),
                note="bundled with tkinter",
            )
        )
    if problems:
        raise NoticesError(problems)
    return components


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def render_notices(components: Sequence[Component]) -> str:
    """The notices document (LF line endings; the writer converts them)."""
    lines = [
        "THIRD-PARTY NOTICES",
        "",
        "Spec Critic for Windows is built with PyInstaller and bundles the",
        "components listed below: the Python interpreter, the Tcl/Tk toolkit, and",
        "Python packages. Each remains under its own license; its full text is",
        "reproduced in this file as the component distributes it. Spec Critic's",
        "own license is in LICENSE.txt, in this folder.",
        "",
        "Components",
        "----------",
    ]
    lines += [f"  {c.title} - {c.license}" for c in components]
    for component in components:
        heading = component.title + (f" ({component.note})" if component.note else "")
        lines += ["", "", _RULE, heading, f"License: {component.license}"]
        for text in component.texts:
            lines += ["", f"--- {text.source} ---", "", text.text]
    return "\n".join(lines) + "\n"


def build_notices(
    tables: Iterable[Iterable[Sequence[object]]],
    *,
    tcl_data_dir: str | os.PathLike[str] | None,
    tk_data_dir: str | os.PathLike[str] | None,
    distributions: Iterable[metadata.Distribution] | None = None,
    interpreter_license: str | os.PathLike[str] | None = None,
    python_version: str | None = None,
    overrides_dir: str | os.PathLike[str] | None = LICENSE_OVERRIDES_DIR,
) -> tuple[str, list[Component]]:
    """Render the notices for an analysis; raises naming every missing text."""
    problems: list[str] = []
    components: list[Component] = []
    try:
        components += base_components(
            tcl_data_dir=tcl_data_dir,
            tk_data_dir=tk_data_dir,
            interpreter_license=interpreter_license,
            python_version=python_version,
        )
    except NoticesError as exc:
        problems += exc.problems
    try:
        dists = bundled_distributions(source_paths(tables), distributions)
        components += distribution_components(dists, overrides_dir=overrides_dir)
    except NoticesError as exc:
        problems += exc.problems
    if problems:
        raise NoticesError(problems)
    return render_notices(components), components


def write_third_party_notices(
    out_path: str | os.PathLike[str],
    tables: Iterable[Iterable[Sequence[object]]],
    **kwargs,
) -> pathlib.Path:
    """Build the notices (see :func:`build_notices`) and write them to ``out_path``.

    Written as UTF-8 with a byte-order mark and Windows line endings, so every
    Notepad version shows the non-ASCII characters in license texts correctly.
    Nothing is written when a text is missing: the error propagates and fails
    the build. A notices file from an earlier build at ``out_path`` is removed
    first, so a failed build cannot leave it behind for the installer to pick up.
    """
    out = pathlib.Path(out_path)
    out.unlink(missing_ok=True)
    document, components = build_notices(tables, **kwargs)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(document)
    packages = len([c for c in components if not c.note])
    print(
        f"Third-party notices: the interpreter, Tcl/Tk, and {packages} packages "
        f"-> {out}"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    """Check this environment's interpreter and Tcl/Tk license texts can be found."""
    del argv  # no options; kept for the bundle_assets.py calling convention
    try:
        from PyInstaller.utils.hooks.tcl_tk import tcltk_info
    except ImportError as exc:
        print(
            f"ERROR: PyInstaller is not installed ({exc}); it locates the Tcl/Tk "
            "directories the build bundles.",
            file=sys.stderr,
        )
        return 1
    try:
        components = base_components(
            tcl_data_dir=tcltk_info.tcl_data_dir,
            tk_data_dir=tcltk_info.tk_data_dir,
        )
    except NoticesError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for component in components:
        for text in component.texts:
            print(f"{component.title}: {text.source} ({len(text.text.splitlines())} lines)")
    print(
        "Interpreter and Tcl/Tk license texts OK; the bundled packages' texts are "
        "checked by the spec after the analysis."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
