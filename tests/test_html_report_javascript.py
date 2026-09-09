"""``node --check`` over the exact JavaScript bytes the HTML exporter ships.

Plan step 3 (`docs/spec_critic_review_implementation_plan.md` §6). The
exported report is a single self-contained file whose one executable inline
script drives every interaction: filtering, navigation, and the Ask AI chat.
Nothing in the Python test suite parses that script, so a syntax error in it
would ship green — the exporter tests assert the script is *present*, escaped,
and CSP-hashed, none of which requires it to be valid JavaScript. The reader
would open the report and find a dead page.

**Scope: the bytes, not the design.** This module does not rewrite, reformat,
or evaluate the exporter's JavaScript. It reads what `write_html_report`
actually wrote, extracts the executable script with the *same* regex the
exporter tests use, and asks Node whether it parses.

**What a pass does and does not prove.** ``node --check`` is a parse, not an
execution: it proves the script is syntactically valid, never that it behaves
correctly in a browser. Two specific gaps are worth stating rather than
leaving to be discovered:

* Node parses a ``.js`` file as CommonJS, which is *sloppy mode* like a classic
  browser ``<script>`` — the closest available match — but not identical. A
  top-level ``return`` parses here and fails in a browser. The exporter does
  not use one; this test would not catch it if it did.
* Browser globals (``document``, ``window``, ``fetch``) are undefined under
  Node. That is irrelevant to a syntax check and is why this runs ``--check``
  rather than executing anything.

The headless-browser initialization smoke that would close both gaps is
deliberately deferred (§6, WP5.2).

**Tooling policy.** Node is a test-time tool only: it is not in
`requirements.txt`, not in the frozen Windows application, and not needed to
produce a report. Locally, a missing Node skips. In CI it must not —
``SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`` turns a missing or broken Node into
a failure, so the check cannot silently stop running on the one machine whose
result gates the merge.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

# Reuse the exporter tests' extractor rather than writing a second one (§6.3).
# Two extractors would drift, and the failure mode of that drift is this test
# happily syntax-checking something the security tests never hashed.
from test_html_report_exporter import (
    _EXEC_SCRIPT_RE,
    build_empty_pipeline_result,
    build_full_pipeline_result,
    build_hostile_pipeline_result,
    build_profile_pipeline_result,
    build_program_result,
)

from src.output.html_report_exporter import write_html_report

# Pinned so report bytes are identical run to run; a syntax check has no
# business depending on the wall clock.
_STAMP = datetime(2026, 1, 1, 12, 0, 0)

_NODE_TIMEOUT_SECONDS = 60

_REQUIRE_TOOLS_ENV = "SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS"

_CSP_HASH_RE = re.compile(r"script-src '(sha256-[A-Za-z0-9+/=]+)'")

# `_EXEC_SCRIPT_RE` matches a bare `<script>` only, so the
# `<script type="application/json" id="sc-report-data">` payload is excluded by
# construction (§6.3). That exclusion is asserted below rather than assumed —
# it is the difference between syntax-checking code and syntax-checking a JSON
# blob that would never parse as JavaScript.
_DATA_SCRIPT_OPEN = '<script type="application/json"'


def _tools_required() -> bool:
    raw = os.environ.get(_REQUIRE_TOOLS_ENV, "")
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _node_executable() -> str | None:
    return shutil.which("node")


def _require_node() -> str:
    """Return the Node path, or skip/fail according to the CI policy."""
    node = _node_executable()
    if node:
        return node
    if _tools_required():
        pytest.fail(
            f"node is required when {_REQUIRE_TOOLS_ENV} is set, but no `node` "
            "executable was found. The exported report's JavaScript is then "
            "unverified — CI must not pass in that state."
        )
    pytest.skip("node not installed; set %s=1 to make this a failure" % _REQUIRE_TOOLS_ENV)


def _render(result, *, include_chat: bool, tmp_path: Path, name: str) -> str:
    """Write a report through the production writer and read its bytes back.

    Goes through `write_html_report` (not `render_html_report`) and reads with
    `read_bytes().decode("utf-8")` deliberately: the point is to check what is
    *on disk*, decoded without universal-newline translation, because that is
    what the CSP hash covers and what a browser will load.
    """
    path = tmp_path / f"{name}.html"
    write_html_report(result, path, generated_at=_STAMP, include_chat=include_chat)
    return path.read_bytes().decode("utf-8")


def _check_syntax(node: str, script: str, tmp_path: Path, name: str) -> None:
    """Assert ``node --check`` accepts ``script``; fail with its own diagnostics.

    The body is written as bytes so nothing between the exporter and the parser
    can alter it — a newline translation here would check something other than
    what shipped.
    """
    js_path = tmp_path / f"{name}.js"
    js_path.write_bytes(script.encode("utf-8"))
    try:
        proc = subprocess.run(
            [node, "--check", str(js_path)],
            capture_output=True,
            text=True,
            timeout=_NODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - environment failure
        pytest.fail(
            f"node --check timed out after {_NODE_TIMEOUT_SECONDS}s on the "
            f"{name} report's script; treat as a failure, not a skip."
        )
    assert proc.returncode == 0, (
        f"the {name} report ships JavaScript that does not parse "
        f"(node --check exit {proc.returncode}):\n{proc.stderr.strip()}"
    )
    # A zero exit with warnings on stderr would still mean something is off
    # about the shipped bytes, so it is not treated as clean.
    assert not proc.stderr.strip(), (
        f"node --check emitted diagnostics for the {name} report:\n"
        f"{proc.stderr.strip()}"
    )


# Every report shape the exporter can produce, with the number of executable
# scripts each must contain. The count is asserted explicitly (§6.3) so this
# module cannot silently check only the first match — or, worse, pass because
# it found none.
_VARIANTS = [
    ("single_module", build_full_pipeline_result, True, 1),
    ("program", build_program_result, True, 1),
    ("hostile", build_hostile_pipeline_result, True, 1),
    ("empty", build_empty_pipeline_result, True, 1),
    ("profile", build_profile_pipeline_result, True, 1),
    ("chat_disabled", build_full_pipeline_result, False, 1),
]


class TestShippedJavaScriptParses:
    """The exported report's own script, exactly as written to disk."""

    @pytest.mark.parametrize(
        "name,builder,include_chat,expected_scripts",
        _VARIANTS,
        ids=[v[0] for v in _VARIANTS],
    )
    def test_node_check_accepts_the_shipped_script(
        self, name, builder, include_chat, expected_scripts, tmp_path
    ):
        node = _require_node()
        html = _render(builder(), include_chat=include_chat, tmp_path=tmp_path, name=name)
        scripts = _EXEC_SCRIPT_RE.findall(html)
        assert len(scripts) == expected_scripts, (
            f"{name}: expected {expected_scripts} executable script(s), found "
            f"{len(scripts)} — the extraction assumption this check rests on "
            "has changed"
        )
        for index, script in enumerate(scripts):
            assert script.strip(), f"{name}: executable script {index} is empty"
            _check_syntax(node, script, tmp_path, f"{name}_{index}")


class TestExtractionIsHonest:
    """The check is worthless if it parses the wrong bytes.

    Three ways this could pass while proving nothing: extract zero scripts,
    extract the JSON data block instead of the code, or check a body different
    from the one the CSP hash covers. Each is pinned.
    """

    def test_the_json_payload_is_not_treated_as_javascript(self, tmp_path):
        html = _render(
            build_full_pipeline_result(),
            include_chat=True,
            tmp_path=tmp_path,
            name="payload",
        )
        assert _DATA_SCRIPT_OPEN in html, "expected an application/json data block"
        for script in _EXEC_SCRIPT_RE.findall(html):
            assert not script.lstrip().startswith("{"), (
                "the extractor captured the JSON payload; it would be "
                "syntax-checked as JavaScript"
            )

    def test_the_payload_cannot_smuggle_a_script_tag(self, tmp_path):
        """Hostile finding text must not be able to forge a script boundary.

        The exporter `\\u`-escapes `<` and `>` inside the data blocks. If that
        ever regressed, a finding containing a literal `</script><script>`
        would split the payload and this module would start checking attacker
        text — so the guarantee is asserted here too, on the report whose
        fixtures carry exactly that string.
        """
        html = _render(
            build_hostile_pipeline_result(),
            include_chat=True,
            tmp_path=tmp_path,
            name="hostile_payload",
        )
        assert len(_EXEC_SCRIPT_RE.findall(html)) == 1

    def test_the_checked_bytes_are_the_hashed_bytes(self, tmp_path):
        """CSP hash and syntax check must cover the same script.

        If they diverged, a report could ship a script that parses but is
        blocked by its own CSP, or one that loads but was never parsed here.
        """
        html = _render(
            build_full_pipeline_result(),
            include_chat=True,
            tmp_path=tmp_path,
            name="hash",
        )
        scripts = _EXEC_SCRIPT_RE.findall(html)
        assert len(scripts) == 1
        digest = hashlib.sha256(scripts[0].encode("utf-8")).digest()
        computed = "sha256-" + base64.b64encode(digest).decode("ascii")
        declared = _CSP_HASH_RE.search(html)
        assert declared is not None, "CSP script hash missing"
        assert declared.group(1) == computed

    def test_the_chat_free_variant_really_drops_the_chat_code(self, tmp_path):
        """Not merely hidden: a chat-free export must not carry the chat script.

        Both variants have exactly one script, so a count alone cannot tell
        them apart — the size difference is the observable that can.
        """
        with_chat = _EXEC_SCRIPT_RE.findall(
            _render(
                build_full_pipeline_result(),
                include_chat=True,
                tmp_path=tmp_path,
                name="chat_on",
            )
        )[0]
        without = _EXEC_SCRIPT_RE.findall(
            _render(
                build_full_pipeline_result(),
                include_chat=False,
                tmp_path=tmp_path,
                name="chat_off",
            )
        )[0]
        assert len(without) < len(with_chat) / 2, (
            "the chat-free variant's script is not materially smaller; the "
            "chat code may still be shipping"
        )
        assert "api.anthropic.com" not in without


class TestRejectionPathWorks:
    """A syntax check that cannot fail is not a check (§6.7)."""

    def test_malformed_javascript_is_rejected(self, tmp_path):
        node = _require_node()
        bad = tmp_path / "broken.js"
        bad.write_bytes(b"function broken( { return 1;")
        proc = subprocess.run(
            [node, "--check", str(bad)],
            capture_output=True,
            text=True,
            timeout=_NODE_TIMEOUT_SECONDS,
        )
        assert proc.returncode != 0, "node --check accepted malformed JavaScript"
        assert proc.stderr.strip(), "node --check reported no diagnostics for a syntax error"

    def test_a_truncated_real_script_is_rejected(self, tmp_path):
        """Closer to the real regression: valid code cut short mid-block.

        A hand-written `function broken(` proves the harness rejects garbage.
        Truncating the actual shipped script proves it rejects the shape an
        exporter bug would actually produce.
        """
        node = _require_node()
        html = _render(
            build_full_pipeline_result(),
            include_chat=True,
            tmp_path=tmp_path,
            name="truncate",
        )
        script = _EXEC_SCRIPT_RE.findall(html)[0]
        truncated = script[: len(script) // 2]
        bad = tmp_path / "truncated.js"
        bad.write_bytes(truncated.encode("utf-8"))
        proc = subprocess.run(
            [node, "--check", str(bad)],
            capture_output=True,
            text=True,
            timeout=_NODE_TIMEOUT_SECONDS,
        )
        assert proc.returncode != 0, (
            "a truncated copy of the shipped script still parsed; the check "
            "would not catch a real truncation bug"
        )


class TestToolPolicy:
    """Missing Node skips locally and fails in CI — never silently passes."""

    # ``pytest.fail`` / ``pytest.skip`` raise ``Failed`` / ``Skipped``, which
    # derive from BaseException, not Exception. Catching ``Exception`` here
    # would let them propagate and pytest would report these very tests as
    # failed and skipped — which is exactly what happened on the first run.
    # ``pytest.fail.Exception`` / ``pytest.skip.Exception`` are the canonical
    # handles for the two types.

    def test_required_flag_turns_a_missing_tool_into_a_failure(self, monkeypatch):
        monkeypatch.setenv(_REQUIRE_TOOLS_ENV, "1")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(pytest.fail.Exception):
            _require_node()

    def test_required_flag_does_not_merely_skip(self, monkeypatch):
        """The distinction the CI gate rests on: a skip would pass blind."""
        monkeypatch.setenv(_REQUIRE_TOOLS_ENV, "1")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(BaseException) as excinfo:
            _require_node()
        assert not isinstance(excinfo.value, pytest.skip.Exception), (
            "a missing Node skipped while the required-tools flag was set; CI "
            "would report success with the shipped JavaScript unverified"
        )

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_without_the_flag_a_missing_tool_skips(self, monkeypatch, value):
        monkeypatch.setenv(_REQUIRE_TOOLS_ENV, value)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(pytest.skip.Exception):
            _require_node()

    def test_the_flag_does_not_fabricate_a_tool(self, monkeypatch):
        """With Node present, the flag changes nothing."""
        monkeypatch.setenv(_REQUIRE_TOOLS_ENV, "1")
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/node")
        assert _require_node() == "/usr/bin/node"
