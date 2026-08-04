"""Contract tests for the standalone Spec Critic HTML report.

The suite is intentionally hermetic: it builds real review dataclasses with
synthetic content, does not open a browser, and never performs a network call.
"""
from __future__ import annotations

import base64
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest

from src.html_report_exporter import build_html_report, export_html_report
from src.pipeline import PipelineResult
from src.reviewer import Finding, ReviewResult
from src.verifier import VerificationResult


PROJECT = "Central Plant Renewal — Project Δ"
FILES = ["23 21 13 – Hydronic Piping.docx", "23 05 00 & Common Work.pdf"]
REVIEWER_NOTES = "Reviewer narrative marker: coordinate the pump schedule.\n\nSecond paragraph survives."
CROSS_NOTES = "Coordination narrative marker: P-101 and M-501 disagree on P-7."
SOURCE_URLS = [
    "https://codes.example.test/cmc/marker-310",
    "https://agency.example.test/bulletin/marker-42",
]


def _alert(kind: str, index: int) -> dict:
    return {
        "filename": FILES[index % len(FILES)],
        "type": f"{kind} type {index}",
        "match": f"{kind.upper()}-MATCH-{index}",
        "context": f"{kind.upper()} full alert context marker {index}",
        "position": index * 17,
    }


def _finding(
    severity: str = "CRITICAL",
    *,
    suffix: str = "primary",
    verification: VerificationResult | None = None,
) -> Finding:
    return Finding(
        severity=severity,
        fileName=f"{FILES[0]} [{suffix}]",
        section=f"23 21 13 § 3.4.{suffix}",
        issue=f"Issue marker {suffix}: relief path is undersized.",
        actionType="EDIT",
        existingText=f"Existing text marker {suffix}: provide a 1 inch line.",
        replacementText=f"Replacement marker {suffix}: provide a 2 inch line.",
        codeReference=f"CMC 2025 § 310.{suffix}",
        confidence=0.937,
        verification=verification,
    )


def _result(*, findings: bool = True) -> PipelineResult:
    verification = VerificationResult(
        verdict="CORRECTED",
        explanation="Verification explanation marker: intent is sound, dimension changes.",
        sources=list(SOURCE_URLS),
        correction="Verification correction marker: use the listed pressure class.",
    )
    review_findings = []
    if findings:
        review_findings = [
            _finding("CRITICAL", suffix="critical", verification=verification),
            _finding("HIGH", suffix="high"),
            _finding("MEDIUM", suffix="medium"),
            _finding("GRIPES", suffix="gripe"),
        ]
    review = ReviewResult(
        findings=review_findings,
        raw_response="raw response is deliberately not a displayed report field",
        thinking=REVIEWER_NOTES,
        model="claude-opus-test-model",
        input_tokens=12_345,
        output_tokens=678,
        elapsed_seconds=91.25,
    )
    cross = None
    if findings:
        cross = ReviewResult(
            findings=[_finding("HIGH", suffix="cross-spec")],
            thinking=CROSS_NOTES,
            model="claude-sonnet-test-model",
        )
    return PipelineResult(
        review_result=review,
        files_reviewed=list(FILES),
        leed_alerts=[_alert("leed", i) for i in range(8)],
        placeholder_alerts=[_alert("placeholder", i) for i in range(8)],
        cross_check_result=cross,
    )


class _VisibleText(HTMLParser):
    """Collect human-facing text while ignoring CSS, JS, and JSON islands."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


class _TagAudit(HTMLParser):
    """Collect parsed tag and attribute names, ignoring encoded text in values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append(tag.lower())
        self.attributes.extend(name.lower() for name, _value in attrs)


def _visible_text(document: str) -> str:
    parser = _VisibleText()
    parser.feed(document)
    return " ".join(" ".join(parser.parts).split())


_SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.I | re.S)


def _script_body(document: str, block_id: str) -> str:
    for match in _SCRIPT_RE.finditer(document):
        attrs = match.group("attrs")
        if re.search(rf'\bid=["\']{re.escape(block_id)}["\']', attrs, re.I):
            return match.group("body")
    raise AssertionError(f"missing script data block #{block_id}")


def _report_payload(document: str) -> dict:
    body = _script_body(document, "spec-critic-report-data")
    if "<" in body:
        raise AssertionError("report JSON can close its inert script element")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise AssertionError("report payload must be a JSON object")
    return payload


def _csp(document: str) -> str:
    match = re.search(
        r'<meta\s+[^>]*http-equiv=["\']Content-Security-Policy["\'][^>]*>',
        document,
        re.I,
    )
    if not match:
        raise AssertionError("missing Content-Security-Policy meta tag")
    content = re.search(
        r'\bcontent=(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
        match.group(0),
        re.I | re.S,
    )
    if not content:
        raise AssertionError("CSP meta tag has no content")
    return unescape(content.group("value"))


class HtmlReportExporterTests(unittest.TestCase):
    def test_every_docx_visible_section_and_finding_field_is_present(self) -> None:
        document = build_html_report(_result(), project_context=PROJECT)
        text = _visible_text(document)

        for expected in (
            "Spec Critic",
            "M&P Specification Review Report",
            "Generated",
            "claude-opus-test-model",
            PROJECT,
            "Files Reviewed",
            *FILES,
            "About This Review",
            "Summary",
            "Review Stage Tokens",
            "12,345",
            "678",
            "Processing Time",
            "91.2",
            "Alerts",
            "LEED References Detected",
            "Unresolved Placeholders",
            "Findings",
            "CRITICAL",
            "HIGH",
            "MEDIUM",
            "GRIPES",
            "23 21 13 § 3.4.critical",
            "Issue marker critical",
            "EDIT",
            "Existing text marker critical",
            "Replacement marker critical",
            "CMC 2025 § 310.critical",
            "94%",
            "CORRECTED",
            "Verification explanation marker",
            "Verification correction marker",
            "Cross-Spec Coordination",
            "Issue marker cross-spec",
            "Coordination Summary",
            CROSS_NOTES,
            "Reviewer's Notes",
            REVIEWER_NOTES.split("\n\n")[0],
            REVIEWER_NOTES.split("\n\n")[1],
        ):
            self.assertIn(expected, text)

        for url in SOURCE_URLS:
            self.assertIn(url, document)
        self.assertIn('rel="noopener noreferrer"', document)

    def test_alerts_are_not_truncated_after_five(self) -> None:
        document = build_html_report(_result(), project_context=PROJECT)
        text = _visible_text(document)
        for index in range(8):
            self.assertIn(f"LEED full alert context marker {index}", text)
            self.assertIn(f"PLACEHOLDER full alert context marker {index}", text)
        self.assertNotIn("... and 3 more", text)

    def test_no_findings_report_is_valid_and_explicit(self) -> None:
        document = build_html_report(_result(findings=False), project_context=PROJECT)
        text = _visible_text(document)
        self.assertTrue(document.startswith("<!DOCTYPE html>"))
        self.assertIn("No issues found", text)
        self.assertIn("Findings", text)
        self.assertNotIn("Cross-Spec Coordination", text)
        self.assertIn(REVIEWER_NOTES.split("\n\n")[0], text)

    def test_report_is_one_self_contained_html_document(self) -> None:
        document = build_html_report(_result(), project_context=PROJECT)
        self.assertTrue(document.startswith("<!DOCTYPE html>"))
        self.assertIn("<style", document)
        self.assertIn("<script", document)
        self.assertNotRegex(document, r"<link\b")
        self.assertNotRegex(document, r"<script\b[^>]*\bsrc\s*=")
        self.assertNotRegex(document, r"@import\s")
        self.assertNotRegex(document, r"<(?:img|iframe)\b[^>]*\bsrc\s*=\s*[\"']https?://")

    def test_chat_is_grounded_and_default_key_is_never_serialized(self) -> None:
        secret = "sk-ant-DO-NOT-SERIALIZE-this-secret"
        document = build_html_report(_result(), project_context=PROJECT, api_key=secret)
        low = document.lower()

        self.assertIn("Ask AI", document)
        self.assertIn("https://api.anthropic.com/v1/messages", document)
        self.assertIn("sessionStorage", document)
        self.assertNotIn(secret, document)
        self.assertNotIn('"apiKey"', document)
        for capability in (
            "web_search_20260209",
            "web_fetch_20260209",
            "query_findings",
            "filter_report",
            "scroll_to_report",
            "get_report_summary",
            "highlight_term",
            "calculate",
            "cache_control",
            "adaptive",
            "Ask AI about this",
        ):
            self.assertIn(capability.lower(), low)

    def test_embedding_a_key_requires_opt_in_and_displays_warning(self) -> None:
        secret = "sk-ant-explicitly-embedded-test-key"
        document = build_html_report(
            _result(),
            project_context=PROJECT,
            api_key=secret,
            embed_api_key=True,
        )
        self.assertIn(secret, document)
        self.assertIn('"apiKey"', document)
        visible = _visible_text(document).lower()
        self.assertIn("embedded", visible)
        self.assertRegex(visible, r"(?:do not|don't|never) share")

    def test_include_chat_false_has_no_anthropic_or_network_surface(self) -> None:
        result = _result(findings=False)
        result.leed_alerts = []
        result.placeholder_alerts = []
        document = build_html_report(result, project_context=PROJECT, include_chat=False)
        low = document.lower()
        self.assertNotIn("ask ai", low)
        self.assertNotIn("anthropic", low)
        self.assertNotIn("http://", low)
        self.assertNotIn("https://", low)
        self.assertNotIn("fetch(", low)
        self.assertNotIn("web_search", low)
        self.assertNotIn("web_fetch", low)
        self.assertIn("connect-src 'none'", _csp(document))

    def test_hostile_values_are_inert_but_round_trip_in_report_json(self) -> None:
        hostile = '</script><script>sentinel()</script><img src=x onerror="sentinel()">'
        verification = VerificationResult(
            verdict="DISPUTED",
            explanation=hostile,
            sources=["javascript:sentinel()"],
            correction=hostile,
        )
        finding = Finding(
            severity="HIGH",
            fileName=hostile,
            section=hostile,
            issue=hostile,
            actionType="EDIT",
            existingText=hostile,
            replacementText=hostile,
            codeReference=hostile,
            confidence=0.5,
            verification=verification,
        )
        result = PipelineResult(
            review_result=ReviewResult(findings=[finding], thinking=hostile),
            files_reviewed=[hostile],
            leed_alerts=[{"filename": hostile, "context": hostile}],
            placeholder_alerts=[{"filename": hostile, "context": hostile}],
            cross_check_result=ReviewResult(findings=[finding], thinking=hostile),
        )
        document = build_html_report(result, project_context=hostile)
        low = document.lower()

        self.assertNotIn("<script>sentinel", low)
        self.assertNotIn("sentinel()</script>", low)
        self.assertNotIn("<img src=x", low)
        audit = _TagAudit()
        audit.feed(document)
        self.assertNotIn("img", audit.tags)
        self.assertFalse(any(name.startswith("on") for name in audit.attributes))
        self.assertNotRegex(low, r'href=["\']javascript:')

        payload_body = _script_body(document, "spec-critic-report-data")
        self.assertNotIn("<", payload_body)
        self.assertIn("\\u003c", payload_body.lower())
        payload = json.loads(payload_body)
        self.assertEqual(hostile, payload["project_context"])
        self.assertEqual(hostile, payload["files_reviewed"][0])
        self.assertEqual(hostile, payload["findings"][0]["issue"])
        self.assertEqual(hostile, payload["findings"][0]["verification"]["explanation"])
        self.assertEqual(hostile, payload["reviewer_notes"])

    def test_csp_hashes_every_executable_script_and_restricts_connections(self) -> None:
        document = build_html_report(_result(), project_context=PROJECT)
        policy = _csp(document)
        executable_bodies: list[str] = []
        for match in _SCRIPT_RE.finditer(document):
            attrs = match.group("attrs")
            type_match = re.search(r'\btype=["\']([^"\']+)', attrs, re.I)
            if type_match and type_match.group(1).lower() not in {
                "text/javascript",
                "application/javascript",
                "module",
            }:
                continue
            executable_bodies.append(match.group("body"))

        self.assertGreaterEqual(len(executable_bodies), 1)
        for body in executable_bodies:
            digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
            self.assertIn("sha256-" + digest, policy)
        self.assertIn("connect-src https://api.anthropic.com", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        self.assertIn("form-action 'none'", policy)
        script_policy = re.search(r"(?:^|;)\s*script-src\s+([^;]+)", policy)
        self.assertIsNotNone(script_policy)
        self.assertNotIn("'unsafe-inline'", script_policy.group(1))

    def test_embedded_report_payload_is_complete_and_plain_text_is_lossless(self) -> None:
        document = build_html_report(_result(), project_context=PROJECT)
        payload = _report_payload(document)
        plain = payload.get("plain_text")
        self.assertIsInstance(plain, str)

        for marker in (
            PROJECT,
            *FILES,
            *[f"LEED full alert context marker {i}" for i in range(8)],
            *[f"PLACEHOLDER full alert context marker {i}" for i in range(8)],
            "Issue marker critical",
            "Existing text marker critical",
            "Replacement marker critical",
            "CMC 2025 § 310.critical",
            "Verification explanation marker",
            "Verification correction marker",
            *SOURCE_URLS,
            "Issue marker cross-spec",
            CROSS_NOTES,
            REVIEWER_NOTES,
        ):
            self.assertIn(marker, plain)

    def test_export_writes_utf8_and_returns_requested_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "spec critic Δ report.html"
            returned = export_html_report(_result(), output, project_context=PROJECT)
            self.assertEqual(output, returned)
            self.assertTrue(output.is_file())
            raw = output.read_bytes()
            self.assertFalse(raw.startswith(b"\xff\xfe"))
            decoded = raw.decode("utf-8")
            self.assertTrue(decoded.startswith("<!DOCTYPE html>"))
            self.assertIn("Reviewer's Notes", decoded)
            self.assertIn("Project Δ", decoded)

            # Validate hashes against the bytes that a browser will execute,
            # catching platform newline translation after CSP construction.
            policy = _csp(decoded)
            executable_bodies: list[str] = []
            for match in _SCRIPT_RE.finditer(decoded):
                attrs = match.group("attrs")
                type_match = re.search(r'\btype=["\']([^"\']+)', attrs, re.I)
                if type_match and type_match.group(1).lower() not in {
                    "text/javascript",
                    "application/javascript",
                    "module",
                }:
                    continue
                executable_bodies.append(match.group("body"))
            self.assertGreaterEqual(len(executable_bodies), 1)
            for body in executable_bodies:
                digest = base64.b64encode(
                    hashlib.sha256(body.encode("utf-8")).digest()
                ).decode("ascii")
                self.assertIn("sha256-" + digest, policy)


if __name__ == "__main__":
    unittest.main()
