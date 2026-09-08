"""The bundled trace viewer must be genuinely offline and escape attributes (B-21).

``src/tracing/viewer/trace_viewer.html`` renders local prompts and full spec
text, so it may load nothing from the network — and every trace-derived
string it interpolates into markup goes through ``esc()``, which must cover
attribute quotes because span names land in ``title="…"``.

Two layers:

* Static (always run): regex over the HTML for external references, for the
  escape table, for inline ``on*=`` handlers, and that every utility class
  the markup/JS uses has a rule in the inline stylesheet (the Tailwind CDN
  replacement cannot silently lose a class).
* Browser (skips cleanly when Playwright or headless Chromium is missing):
  write a synthetic trace with the real ``TraceRecorder``, load the viewer
  over ``file://`` with every non-file request aborted, and assert the run
  list / span tree render, styling applied without a CDN, and a span named
  with a double quote and a ``<b>`` tag renders as literal text without
  breaking the ``title`` attribute.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.tracing import LEVEL_DEFAULT, TraceRecorder
from src.tracing.spans import KIND_PIPELINE

VIEWER = Path(__file__).resolve().parents[1] / "src" / "tracing" / "viewer" / "trace_viewer.html"

# Classes the page defines itself (not Tailwind-style utilities).
_VIEWER_OWN_CLASSES = {"mono", "tree-row", "selected", "payload", "scroll-y", "tab"}


@pytest.fixture(scope="module")
def html() -> str:
    return VIEWER.read_text(encoding="utf-8")


def _style_block(html: str) -> str:
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    assert m, "viewer must carry exactly one inline <style> block"
    return m.group(1)


def _class_tokens(html: str) -> set[str]:
    """Every class token in static markup, JS template strings, className
    assignments, and classList.toggle/add/remove calls."""
    tokens: set[str] = set()
    for m in re.finditer(r'class=(?:"([^"]*)"|\'([^\']*)\')', html):
        tokens.update((m.group(1) or m.group(2) or "").split())
    for m in re.finditer(r'className\s*=\s*"([^"]*)"', html):
        tokens.update(m.group(1).split())
    for m in re.finditer(r'classList\.(?:toggle|add|remove)\("([^"]*)"', html):
        tokens.add(m.group(1))
    return {t for t in tokens if "${" not in t}


def _css_selector(token: str) -> str:
    return "." + token.replace(".", "\\.").replace(":", "\\:")


# ---- static -------------------------------------------------------------
class TestStaticOffline:
    def test_no_external_references(self, html: str) -> None:
        assert not re.search(r"<script[^>]*\ssrc=", html), "no external scripts"
        assert not re.search(r"<link\b", html), "no <link> (stylesheets, icons, preloads)"
        assert "@import" not in html
        assert not re.search(r"url\(\s*['\"]?\s*(?:https?:)?//", html), "no external url()"
        assert not re.search(r"https?://", html), "no URL of any kind in the file"
        assert "cdn.tailwindcss.com" not in html
        assert html.count("<script") == 1, "exactly one (inline) script"

    def test_escape_table_covers_text_and_attribute_contexts(self, html: str) -> None:
        m = re.search(r"function esc\(s\)\s*\{(.*?)\n\}", html, re.S)
        assert m, "esc() must exist"
        body = m.group(1)
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            assert entity in body, f"esc() must emit {entity}"
        # `&` must be replaced first so later entities are not double-escaped.
        assert body.index("&amp;") < min(body.index(e) for e in ("&lt;", "&gt;", "&quot;", "&#39;"))

    def test_esc_is_used_in_the_title_attribute(self, html: str) -> None:
        assert re.search(r'title="\$\{esc\(', html), "title attributes must go through esc()"
        # No raw (un-escaped) interpolation into an attribute value.
        raw = re.findall(r'(?:title|data-[a-z-]+)="\$\{(?!esc\()[^}]*\}"', html)
        assert raw == [], f"un-escaped attribute interpolation: {raw}"

    def test_no_inline_event_handler_attributes(self, html: str) -> None:
        assert not re.search(r"\son[a-z]+=", html), "use data attributes + listeners, never on*="

    def test_every_utility_class_has_a_rule(self, html: str) -> None:
        style = _style_block(html)
        missing = sorted(
            t for t in _class_tokens(html) - _VIEWER_OWN_CLASSES
            if _css_selector(t) not in style
        )
        assert missing == [], f"classes used without a stylesheet rule: {missing}"
        for own in _VIEWER_OWN_CLASSES - {"tab", "selected"}:
            assert "." + own in style

    def test_utility_ordering_pins(self, html: str) -> None:
        """Two cascade-order facts the layout depends on (Tailwind's emit order)."""
        style = _style_block(html)
        assert style.index(".flex {") < style.index(".hidden {")
        assert style.index(".grid {") < style.index(".hidden {")
        assert style.index(".border-transparent") < style.index(".border-blue-600")
        assert style.index(".bg-blue-600") < style.index(".hover\\:bg-blue-500:hover")


# ---- browser ------------------------------------------------------------
SPAN_NAME = 'He said "hi" <b>bold</b> & \'single\''
ISSUE = 'Issue with "quotes" <i>tag</i>'
RUN_ID = "run_esc1"


@dataclass
class _Finding:
    finding_id: str = "rf-1"
    severity: str = "HIGH"
    section: str = "23 05 00"
    issue: str = ISSUE
    codeReference: str = ""
    actionType: str = "REPORT_ONLY"
    verification: None = None


def _write_trace(run_dir: Path) -> None:
    rec = TraceRecorder(run_id=RUN_ID, trace_dir=run_dir, capture_level=LEVEL_DEFAULT, spec_critic_version="test")
    rec.start(mode="realtime", model="test-model", cycle_label="Test Cycle", files_reviewed=["a.docx"])
    with rec.span(KIND_PIPELINE, SPAN_NAME):
        with rec.span("verification_initial", "verify rf-1", metadata={"finding_id": "rf-1"}) as v:
            rec.add_event(v, "note", message="hello")
    rec.record_finding_snapshot(_Finding())
    rec.stop()


_CHROMIUM_CANDIDATES = (
    os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
    "/opt/pw-browsers/chromium",
)


@pytest.fixture(scope="module")
def chromium_page():
    """A headless Chromium page, or a clean skip when unavailable."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001 — driver missing/broken → skip, never fail
        pytest.skip(f"Playwright driver unavailable: {exc}")
    browser = None
    try:
        launch_kwargs: dict = {"headless": True}
        exe = next((c for c in _CHROMIUM_CANDIDATES if c and Path(c).exists()), None)
        if exe:
            launch_kwargs["executable_path"] = exe
        try:
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Headless Chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_default_timeout(15_000)
        yield page
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            pw.stop()


class TestHeadlessChromium:
    def test_viewer_renders_offline_and_escapes_attributes(self, chromium_page, tmp_path: Path) -> None:
        page = chromium_page
        run_dir = tmp_path / RUN_ID
        _write_trace(run_dir)

        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        # Abort anything that is not the local file — a CDN fetch would fail
        # loudly here rather than silently styling the page.
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file://") else route.abort())
        page.goto(VIEWER.as_uri())
        page.set_input_files("#dirInput", str(run_dir))
        page.wait_for_function(f"document.getElementById('runSummary').textContent.includes('{RUN_ID}')")

        # Offline: zero network requests, no external asset elements.
        assert requests and all(u.startswith("file://") for u in requests), requests
        assert page.evaluate("document.querySelectorAll('script[src], link[href]').length") == 0
        assert page.text_content("#loadError") == ""

        # Styling came from the inline stylesheet (no CDN): header bg-slate-800,
        # tabs flex, truncation rule live, hover variant live.
        assert page.evaluate("getComputedStyle(document.querySelector('header')).backgroundColor") == "rgb(30, 41, 59)"
        assert page.evaluate("getComputedStyle(document.getElementById('tabs')).display") == "flex"
        assert page.evaluate("getComputedStyle(document.getElementById('emptyState')).display") == "none"
        label = page.locator("header label")
        assert label.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(37, 99, 235)"
        label.hover()
        assert label.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(59, 130, 246)"

        # By Finding (default tab): the run list renders, hostile text is literal.
        rows = page.locator("#leftPane .tree-row")
        assert rows.count() == 1
        assert ISSUE in rows.first.text_content()
        assert page.evaluate("document.querySelectorAll('#leftPane i, #leftPane b').length") == 0

        # By Span: the tree renders; the hostile name is literal text AND the
        # title attribute survives intact (no truncation at the double quote,
        # no stray attributes spilled out of it).
        page.click("button[data-tab=span]")
        row = page.locator("#leftPane .tree-row").first
        title_span = row.locator("span[title]")
        assert title_span.get_attribute("title") == SPAN_NAME
        assert title_span.evaluate("el => Array.from(el.attributes).map(a => a.name).sort()") == ["class", "title"]
        assert SPAN_NAME in row.text_content()
        assert page.evaluate("document.querySelectorAll('#leftPane b').length") == 0
        assert title_span.evaluate("el => getComputedStyle(el).textOverflow") == "ellipsis"
        assert page.locator("#leftPane .tree-row").count() == 2  # pipeline + verification child

        # Finding → lifecycle row → span detail via the delegated listener
        # (no inline onclick).
        page.click("button[data-tab=finding]")
        page.locator("#leftPane .tree-row").first.click()
        lifecycle = page.locator("#middlePane [data-lifecycle-span]")
        assert lifecycle.count() == 1
        lifecycle.first.click()
        assert "verify rf-1" in page.text_content("#rightPane")
        assert page.evaluate("document.querySelectorAll('[onclick]').length") == 0

        # Still no network activity after every interaction.
        assert all(u.startswith("file://") for u in requests), requests
