"""JSON fixture loader for the calibration eval.

Every fixture is one file under :data:`FIXTURES_DIR` containing:

* ``fixture_id`` — unique slug; doubles as the file stem.
* ``category`` — verification profile or finding kind (e.g.
  ``jurisdictional``, ``code_standard``, ``manufacturer``,
  ``internal_coordination``). Drives per-profile breakdowns in the
  scorer.
* ``severity`` — CRITICAL / HIGH / MEDIUM / GRIPES.
* ``description`` — short human-readable summary of the case.
* ``finding`` — raw payload matching ``Finding`` field names: severity,
  fileName, section, issue, actionType, existingText, replacementText,
  codeReference, confidence, anchorText, insertPosition,
  evidenceElementId.
* ``spec_context`` — filename + cycle_label + paragraph_map_slice
  (list of ``{index, id, text}`` records) so a human reviewer can
  cross-check the finding against the spec without running the pipeline.
* ``captured_verifier_response`` — what the verifier returned on the
  run that produced this fixture (or a synthetic plausible response when
  bootstrapping). Carries verdict / explanation / sources / correction /
  confidence / model_used / verification_mode / verification_profile /
  web_search_requests / successful_source_count / search_error_count /
  searched_urls / grounded / cache_status.
* ``ground_truth`` — hand-labeled correctness oracle: correct_verdict,
  correct_correction_text (when applicable), expected_status, notes.

The loader is conservative: missing required keys raise so a malformed
fixture fails fast rather than silently scoring as a pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import FIXTURES_DIR


# Verdicts allowed in fixtures. Anything else fails validation so a typo
# in a hand-labeled fixture surfaces immediately.
_VALID_VERDICTS = frozenset({"CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED"})

# Statuses the trust model can assign. Mirrors
# :class:`src.output.report_status.ReportStatus` — duplicated here so the
# loader does not need to import the production module just to validate
# fixture spelling. A drift will surface as a scorer assertion later.
_VALID_STATUSES = frozenset({
    "VERIFIED_SUPPORTED",
    "VERIFIED_CONTRADICTED",
    "DISPUTED",
    "INSUFFICIENT_EVIDENCE",
    "LOCALLY_CLASSIFIED",
    "NOT_CHECKED",
    "MANUAL_REVIEW_REQUIRED",
})


@dataclass(frozen=True)
class FindingPayload:
    """Raw review-finding payload (parsed from fixture JSON)."""

    severity: str
    fileName: str
    section: str
    issue: str
    actionType: str
    existingText: str | None
    replacementText: str | None
    codeReference: str | None
    confidence: float
    anchorText: str | None = None
    insertPosition: str | None = None
    evidenceElementId: str | None = None


@dataclass(frozen=True)
class CapturedVerifierResponse:
    """Snapshot of a real (or synthetic-plausible) verifier outcome."""

    verdict: str
    explanation: str = ""
    sources: list[str] = field(default_factory=list)
    correction: str | None = None
    confidence: float = 0.0
    model_used: str = ""
    verification_mode: str = ""
    verification_profile: str = ""
    web_search_requests: int = 0
    successful_source_count: int = 0
    search_error_count: int = 0
    searched_urls: list[str] = field(default_factory=list)
    grounded: bool = False
    cache_status: str = "miss"


@dataclass(frozen=True)
class SpecContext:
    """Minimal spec context attached for human inspection only."""

    filename: str
    cycle_label: str
    paragraph_map_slice: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class GroundTruth:
    """Hand-labeled correctness oracle for one fixture."""

    correct_verdict: str
    correct_correction_text: str | None = None
    expected_status: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class CalibrationFixture:
    """A single labeled fixture."""

    fixture_id: str
    category: str
    severity: str
    description: str
    finding: FindingPayload
    spec_context: SpecContext
    captured_verifier_response: CapturedVerifierResponse
    ground_truth: GroundTruth
    source_path: Path | None = None


def _require_key(data: dict, key: str, context: str) -> Any:
    if key not in data:
        raise ValueError(f"{context}: missing required key '{key}'")
    return data[key]


def _parse_finding(raw: dict, context: str) -> FindingPayload:
    return FindingPayload(
        severity=str(_require_key(raw, "severity", context)),
        fileName=str(_require_key(raw, "fileName", context)),
        section=str(_require_key(raw, "section", context)),
        issue=str(_require_key(raw, "issue", context)),
        actionType=str(_require_key(raw, "actionType", context)),
        existingText=_optional_str(raw.get("existingText")),
        replacementText=_optional_str(raw.get("replacementText")),
        codeReference=_optional_str(raw.get("codeReference")),
        confidence=float(raw.get("confidence", 0.5)),
        anchorText=_optional_str(raw.get("anchorText")),
        insertPosition=_optional_str(raw.get("insertPosition")),
        evidenceElementId=_optional_str(raw.get("evidenceElementId")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text


def _parse_response(raw: dict, context: str) -> CapturedVerifierResponse:
    verdict = str(_require_key(raw, "verdict", context)).strip().upper()
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"{context}: captured_verifier_response.verdict '{verdict}' "
            f"is not one of {sorted(_VALID_VERDICTS)}"
        )
    return CapturedVerifierResponse(
        verdict=verdict,
        explanation=str(raw.get("explanation", "")),
        sources=[str(s) for s in raw.get("sources", []) if s],
        correction=_optional_str(raw.get("correction")),
        confidence=float(raw.get("confidence", 0.0)),
        model_used=str(raw.get("model_used", "")),
        verification_mode=str(raw.get("verification_mode", "")),
        verification_profile=str(raw.get("verification_profile", "")),
        web_search_requests=int(raw.get("web_search_requests", 0)),
        successful_source_count=int(raw.get("successful_source_count", 0)),
        search_error_count=int(raw.get("search_error_count", 0)),
        searched_urls=[str(s) for s in raw.get("searched_urls", []) if s],
        grounded=bool(raw.get("grounded", False)),
        cache_status=str(raw.get("cache_status", "miss")),
    )


def _parse_spec_context(raw: dict, context: str) -> SpecContext:
    return SpecContext(
        filename=str(_require_key(raw, "filename", context)),
        cycle_label=str(raw.get("cycle_label", "")),
        paragraph_map_slice=list(raw.get("paragraph_map_slice", [])),
    )


def _parse_ground_truth(raw: dict, context: str) -> GroundTruth:
    correct = str(_require_key(raw, "correct_verdict", context)).strip().upper()
    if correct not in _VALID_VERDICTS:
        raise ValueError(
            f"{context}: ground_truth.correct_verdict '{correct}' "
            f"is not one of {sorted(_VALID_VERDICTS)}"
        )
    expected_status = raw.get("expected_status")
    if expected_status is not None:
        expected_status = str(expected_status).strip().upper()
        if expected_status not in _VALID_STATUSES:
            raise ValueError(
                f"{context}: ground_truth.expected_status '{expected_status}' "
                f"is not one of {sorted(_VALID_STATUSES)}"
            )
    return GroundTruth(
        correct_verdict=correct,
        correct_correction_text=_optional_str(raw.get("correct_correction_text")),
        expected_status=expected_status,
        notes=str(raw.get("notes", "")),
    )


def load_fixture(path: Path) -> CalibrationFixture:
    """Parse a single fixture file, raising on missing required keys."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc.msg})") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: fixture root must be a JSON object")
    context = str(path)
    fixture_id = str(_require_key(raw, "fixture_id", context))
    return CalibrationFixture(
        fixture_id=fixture_id,
        category=str(_require_key(raw, "category", context)),
        severity=str(_require_key(raw, "severity", context)),
        description=str(raw.get("description", "")),
        finding=_parse_finding(_require_key(raw, "finding", context), context),
        spec_context=_parse_spec_context(
            _require_key(raw, "spec_context", context), context
        ),
        captured_verifier_response=_parse_response(
            _require_key(raw, "captured_verifier_response", context), context
        ),
        ground_truth=_parse_ground_truth(
            _require_key(raw, "ground_truth", context), context
        ),
        source_path=path,
    )


def discover_fixtures(directory: Path = FIXTURES_DIR) -> list[Path]:
    """Return every ``*.json`` fixture path under ``directory`` (sorted)."""
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.json") if p.is_file())


def load_all_fixtures(directory: Path = FIXTURES_DIR) -> list[CalibrationFixture]:
    """Load every fixture under ``directory`` (sorted by file stem)."""
    return [load_fixture(p) for p in discover_fixtures(directory)]


def find_duplicate_ids(fixtures: Iterable[CalibrationFixture]) -> list[str]:
    """Return any fixture_ids that appear more than once."""
    seen: dict[str, int] = {}
    for fx in fixtures:
        seen[fx.fixture_id] = seen.get(fx.fixture_id, 0) + 1
    return sorted(k for k, v in seen.items() if v > 1)
