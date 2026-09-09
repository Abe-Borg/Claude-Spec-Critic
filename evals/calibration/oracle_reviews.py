"""Adjudication ledger for the live-capture calibration fixtures.

The twelve fixtures under ``fixtures_live/`` were originally written by
``evals/live_capture.py`` with **auto-generated** ground truth — every one
carried the note "Confirm correct_verdict / expected_status before trusting
this fixture," and none had been confirmed. Eleven of the twelve disagreed
with the verdict the captured verifier actually produced, so the set could
not be cited as a quality result in either direction.

This module records the human adjudication of each capture and enforces
that the record stays tied to the evidence it was made against.

Three things are kept strictly apart:

* **Immutable evidence** — the fixture's finding, spec context, and captured
  verifier response. Adjudication never rewrites these. A historical model
  mistake stays visible as a mistake.
* **The oracle** — ``ground_truth.correct_verdict`` / ``expected_status`` in
  the fixture file. This is what adjudication may change.
* **The ledger** — this module's JSON sidecar, recording *why* each oracle
  reads the way it does, what it used to say, and what evidence it was
  judged against.

The integrity check deliberately does **not** require a captured response to
agree with the corrected ground truth. A fixture whose model output was wrong
is a valid, useful fixture; the ledger just has to say so.

``evidence_digest`` hashes only :data:`IMMUTABLE_FIXTURE_KEYS`, so re-labelling
a fixture never invalidates its own ledger entry, while editing the captured
evidence always does.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LEDGER_SCHEMA_VERSION = 1

#: Default ledger location. Deliberately *outside* ``fixtures_live/`` so
#: fixture discovery (``loader.discover_fixtures``) never tries to parse it
#: as a fixture.
DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent / "oracle_reviews.json"

#: Fixture keys that adjudication must never change. The digest covers these
#: and nothing else, so ``ground_truth`` edits (the oracle) do not disturb it.
IMMUTABLE_FIXTURE_KEYS = (
    "fixture_id",
    "category",
    "severity",
    "description",
    "finding",
    "spec_context",
    "captured_verifier_response",
)

_RESOLVED = "resolved"
_UNRESOLVED = "unresolved"
_VALID_STATES = frozenset({_RESOLVED, _UNRESOLVED})


def evidence_digest(raw: dict) -> str:
    """Return a stable digest of a fixture's immutable evidence.

    Keys outside :data:`IMMUTABLE_FIXTURE_KEYS` — notably ``ground_truth`` —
    are excluded, so correcting an oracle does not invalidate the ledger entry
    that justifies the correction. Serialization is sorted and separator-pinned
    so the digest depends on content, not formatting.
    """
    payload = {k: raw[k] for k in IMMUTABLE_FIXTURE_KEYS if k in raw}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


@dataclass(frozen=True)
class OracleReview:
    """One adjudicated fixture."""

    fixture_id: str
    labeled_case_id: str
    evidence_sha256: str
    review_state: str
    rationale: str
    adjudicated_on: str
    correct_verdict: str | None = None
    expected_status: str | None = None
    previous_verdict: str | None = None
    previous_status: str | None = None
    confidence: str | None = None
    sources: tuple[str, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.review_state == _RESOLVED

    @property
    def changed_oracle(self) -> bool:
        """True when adjudication moved the verdict or the status."""
        if not self.is_resolved:
            return False
        return (
            self.correct_verdict != self.previous_verdict
            or self.expected_status != self.previous_status
        )


@dataclass(frozen=True)
class OracleLedger:
    schema_version: int
    adjudicated_on: str
    method: str
    reviews: dict[str, OracleReview]

    def get(self, fixture_id: str) -> OracleReview | None:
        return self.reviews.get(fixture_id)


@dataclass
class LedgerValidation:
    """Outcome of checking a ledger against a fixture directory."""

    errors: list[str] = field(default_factory=list)
    resolved_ids: list[str] = field(default_factory=list)
    #: ``(fixture_id, reason)`` for every fixture excluded from scoring.
    unresolved: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _require(raw: dict, key: str, context: str) -> Any:
    if key not in raw:
        raise ValueError(f"{context}: missing required key '{key}'")
    return raw[key]


def _parse_review(fixture_id: str, raw: dict) -> OracleReview:
    context = f"oracle review '{fixture_id}'"
    if not isinstance(raw, dict):
        raise ValueError(f"{context}: entry must be an object")

    state = str(_require(raw, "review_state", context)).strip().lower()
    if state not in _VALID_STATES:
        raise ValueError(
            f"{context}: review_state '{state}' is not one of "
            f"{sorted(_VALID_STATES)}"
        )

    rationale = str(_require(raw, "rationale", context)).strip()
    if not rationale:
        raise ValueError(f"{context}: rationale must not be empty")

    verdict = raw.get("correct_verdict")
    status = raw.get("expected_status")
    if state == _RESOLVED:
        # A resolved entry is what scoring consumes, so it must actually
        # carry an oracle. Silently scoring a resolved-but-empty entry is
        # precisely the "quietly excluded case" this ledger exists to stop.
        if not verdict or not status:
            raise ValueError(
                f"{context}: a resolved review must carry both "
                "correct_verdict and expected_status"
            )
    else:
        if verdict or status:
            raise ValueError(
                f"{context}: an unresolved review must not carry "
                "correct_verdict / expected_status — it is excluded from "
                "scoring and a label would be misleading"
            )

    sources = raw.get("sources") or []
    if not isinstance(sources, list):
        raise ValueError(f"{context}: sources must be a list when present")

    return OracleReview(
        fixture_id=fixture_id,
        labeled_case_id=str(_require(raw, "labeled_case_id", context)),
        evidence_sha256=str(_require(raw, "evidence_sha256", context)),
        review_state=state,
        rationale=rationale,
        adjudicated_on=str(_require(raw, "adjudicated_on", context)),
        correct_verdict=str(verdict).strip().upper() if verdict else None,
        expected_status=str(status).strip().upper() if status else None,
        previous_verdict=(
            str(raw["previous_verdict"]).strip().upper()
            if raw.get("previous_verdict")
            else None
        ),
        previous_status=(
            str(raw["previous_status"]).strip().upper()
            if raw.get("previous_status")
            else None
        ),
        confidence=(
            str(raw["confidence"]).strip().lower() if raw.get("confidence") else None
        ),
        sources=tuple(str(s) for s in sources),
    )


def load_ledger(path: Path | str = DEFAULT_LEDGER_PATH) -> OracleLedger:
    """Load and structurally validate the adjudication ledger.

    Raises ``FileNotFoundError`` when the path does not exist and
    ``ValueError`` for any malformed entry — never a partial ledger.
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    context = f"oracle ledger {path.name}"

    version = _require(raw, "schema_version", context)
    if version != LEDGER_SCHEMA_VERSION:
        raise ValueError(
            f"{context}: schema_version {version!r} is not the supported "
            f"version {LEDGER_SCHEMA_VERSION}"
        )

    reviews_raw = _require(raw, "reviews", context)
    if not isinstance(reviews_raw, dict):
        raise ValueError(f"{context}: 'reviews' must be an object")

    reviews = {
        fixture_id: _parse_review(fixture_id, entry)
        for fixture_id, entry in reviews_raw.items()
    }
    return OracleLedger(
        schema_version=int(version),
        adjudicated_on=str(_require(raw, "adjudicated_on", context)),
        method=str(raw.get("method", "")),
        reviews=reviews,
    )


def validate_against_fixtures(
    ledger: OracleLedger, fixture_paths: Iterable[Path]
) -> LedgerValidation:
    """Check the ledger describes exactly this fixture set, unchanged.

    Every failure below is an *error*, never a quiet exclusion:

    * a fixture with no ledger entry,
    * a ledger entry naming a fixture that is not present,
    * an evidence digest that no longer matches (the capture was edited
      after it was adjudicated),
    * a resolved entry whose oracle disagrees with the fixture file it
      is supposed to justify.
    """
    result = LedgerValidation()
    seen: set[str] = set()

    for path in sorted(fixture_paths):
        raw = json.loads(path.read_text(encoding="utf-8"))
        fixture_id = str(raw.get("fixture_id") or path.stem)
        seen.add(fixture_id)

        review = ledger.get(fixture_id)
        if review is None:
            result.errors.append(
                f"{fixture_id}: no adjudication record in the ledger. Every "
                "fixture must be adjudicated (resolved or explicitly "
                "unresolved) before --reviewed-only can score this set."
            )
            continue

        actual = evidence_digest(raw)
        if actual != review.evidence_sha256:
            result.errors.append(
                f"{fixture_id}: captured evidence changed since adjudication "
                f"(ledger {review.evidence_sha256}, fixture {actual}). "
                "Re-adjudicate rather than editing the recorded hash."
            )
            continue

        if review.is_resolved:
            gt = raw.get("ground_truth") or {}
            file_verdict = str(gt.get("correct_verdict") or "").strip().upper()
            file_status = str(gt.get("expected_status") or "").strip().upper()
            if file_verdict != review.correct_verdict:
                result.errors.append(
                    f"{fixture_id}: ground_truth.correct_verdict "
                    f"'{file_verdict}' disagrees with the ledger's "
                    f"'{review.correct_verdict}'."
                )
                continue
            if file_status != review.expected_status:
                result.errors.append(
                    f"{fixture_id}: ground_truth.expected_status "
                    f"'{file_status}' disagrees with the ledger's "
                    f"'{review.expected_status}'."
                )
                continue
            result.resolved_ids.append(fixture_id)
        else:
            result.unresolved.append((fixture_id, review.rationale))

    for fixture_id in sorted(set(ledger.reviews) - seen):
        result.errors.append(
            f"{fixture_id}: ledger entry has no matching fixture in this "
            "directory."
        )

    return result
