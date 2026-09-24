"""The import direction between Spec Critic and the applier is one-way.

``CLAUDE.md`` states that Spec Critic emits edit instructions and does not
apply them, and v3.0.0 deleted the machinery that did. Shipping an applier in
the same repository is only compatible with that promise while the dependency
runs in exactly one direction: the applier may read the app, and no part of
the app may reach the applier.

An AST tripwire rather than a convention, in the style of
``test_dc_applicability.TestPropagationIsStructurallyEnforced`` and the
research/verification no-cycle rule: a convention is re-litigated by whoever
is in a hurry, and the failure here would be silent — the GUI would simply
start applying edits again, which is the exact behaviour that was removed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
_APPLIER = _REPO_ROOT / "applier"

#: The applier resolves element ids with the same code that minted them, and
#: reads the same status enum the sidecar serialized. Both are read-only, and
#: both are load-bearing: re-deriving either independently is how an applier
#: ends up editing the wrong paragraph under a status it misread. Anything
#: beyond this list is coupling that needs a reason.
ALLOWED_SRC_IMPORTS = {
    "src.core.api_config",
    "src.core.api_key_store",
    "src.input.extractor",
    "src.output.report_status",
    "src.output.edit_sidecar",
}


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """Absolute module names a file imports, resolving relative imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = path.relative_to(_REPO_ROOT).with_suffix("").parts
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[:-1])
                for _ in range(node.level - 1):
                    if base:
                        base.pop()
                prefix = ".".join(base)
                modules.add(f"{prefix}.{node.module}" if node.module else prefix)
            elif node.module:
                modules.add(node.module)
    return modules


class TestNothingInTheAppReachesTheApplier:
    def test_no_src_module_imports_applier(self):
        offenders = {
            str(path.relative_to(_REPO_ROOT)): sorted(
                name for name in _imported_modules(path) if name.split(".")[0] == "applier"
            )
            for path in _python_files(_SRC)
        }
        offenders = {path: names for path, names in offenders.items() if names}
        assert offenders == {}, (
            "Spec Critic must not import the applier — the app emits edit "
            f"instructions and does not apply them. Offending modules: {offenders}"
        )

    def test_the_string_applier_does_not_appear_as_an_import_in_src(self):
        """A belt-and-braces check that also catches a dynamic import."""
        offenders = [
            str(path.relative_to(_REPO_ROOT))
            for path in _python_files(_SRC)
            if "import applier" in path.read_text(encoding="utf-8")
            or 'import_module("applier' in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_the_entry_points_are_separate_programs(self):
        """``main.py`` starts the GUI; ``python -m applier`` starts the applier.
        Neither may start the other."""
        main_source = (_REPO_ROOT / "main.py").read_text(encoding="utf-8")
        assert "applier" not in main_source
        assert (_APPLIER / "__main__.py").exists()


class TestTheApplierReadsOnlyWhatItMust:
    def test_src_imports_stay_inside_the_allowlist(self):
        actual: set[str] = set()
        for path in _python_files(_APPLIER):
            actual.update(
                name
                for name in _imported_modules(path)
                if name.split(".")[0] == "src"
            )
        unexpected = actual - ALLOWED_SRC_IMPORTS
        assert unexpected == set(), (
            "The applier consumes Spec Critic's artifacts, not its runtime. "
            f"Unexpected imports: {sorted(unexpected)}"
        )

    @pytest.mark.parametrize("forbidden", ["src.gui", "src.orchestration", "src.review"])
    def test_the_applier_never_reaches_the_pipeline(self, forbidden):
        for path in _python_files(_APPLIER):
            for name in _imported_modules(path):
                assert not name.startswith(forbidden), (
                    f"{path.name} imports {name}; the applier runs after a "
                    "review, from its artifacts, never inside one"
                )

    def test_the_allowlist_has_no_stale_entries(self):
        """A permission nobody uses is a permission nobody reviewed."""
        actual: set[str] = set()
        for path in _python_files(_APPLIER):
            actual.update(
                name for name in _imported_modules(path) if name.split(".")[0] == "src"
            )
        stale = ALLOWED_SRC_IMPORTS - actual - {"src.output.edit_sidecar"}
        assert stale == set(), f"unused allowlist entries: {sorted(stale)}"


class TestTheApplierIsHermetic:
    def test_importing_it_needs_no_gui_and_no_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import importlib

        for name in (
            "applier",
            "applier.cli",
            "applier.run",
            "applier.conflicts",
            "applier.locator",
            "applier.policy",
            "applier.sidecar",
            "applier.docx_edit",
            "applier.assist",
            "applier.receipt",
            "applier.textmatch",
        ):
            assert importlib.import_module(name) is not None

    def test_no_applier_module_imports_tkinter(self):
        for path in _python_files(_APPLIER):
            for name in _imported_modules(path):
                assert not name.startswith(("tkinter", "customtkinter"))

    def test_the_package_is_declared_for_packaging(self):
        pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "applier*" in pyproject, (
            "applier/ must be listed in [tool.setuptools.packages.find] or an "
            "installed copy of the project silently lacks the program"
        )
