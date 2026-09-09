"""Pins for the live-fixture adjudication ledger (plan step 1, section 4.2).

The twelve captures under ``fixtures_live/`` shipped with auto-generated,
explicitly unconfirmed ground truth. These tests lock in the three properties
that make the corrected labels trustworthy:

1. **Coverage** — every capture has an adjudication record, resolved or
   explicitly unresolved. Nothing is quietly dropped.
2. **Evidence identity** — the ledger is bound to the captured evidence by
   digest, so editing a captured model response invalidates the record that
   justified its label, while re-labelling does not.
3. **The runner really consumes it** — ``--reviewed-only`` scores the resolved
   set, names every exclusion, and refuses to run on inconsistent metadata.
   A ledger that scoring ignored would not complete this work.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evals.calibration.oracle_reviews import (
    DEFAULT_LEDGER_PATH,
    IMMUTABLE_FIXTURE_KEYS,
    LEDGER_SCHEMA_VERSION,
    evidence_digest,
    load_ledger,
    validate_against_fixtures,
)

_LIVE_DIR = Path(__file__).resolve().parents[1] / "evals" / "calibration" / "fixtures_live"


def _live_fixture_paths() -> list[Path]:
    return sorted(_LIVE_DIR.glob("*.json"))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Coverage and consistency of the shipped ledger
# ---------------------------------------------------------------------------


class TestShippedLedger:
    def test_ledger_loads(self):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        assert ledger.schema_version == LEDGER_SCHEMA_VERSION
        assert ledger.reviews

    def test_ledger_lives_outside_the_fixture_directory(self):
        """Discovery globs ``fixtures_live/*.json`` — the ledger must not sit there."""
        assert DEFAULT_LEDGER_PATH.parent == _LIVE_DIR.parent
        assert DEFAULT_LEDGER_PATH not in _live_fixture_paths()

    def test_every_live_fixture_is_adjudicated(self):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        fixture_ids = {_read(p)["fixture_id"] for p in _live_fixture_paths()}
        assert fixture_ids == set(ledger.reviews), (
            "every capture must carry an adjudication record"
        )

    def test_shipped_ledger_validates_clean(self):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        validation = validate_against_fixtures(ledger, _live_fixture_paths())
        assert validation.ok, validation.errors
        # The set is deliberately not all-resolved: forcing a label onto a case
        # that cannot be settled on its evidence is what this ledger prevents.
        assert validation.resolved_ids
        assert len(validation.resolved_ids) + len(validation.unresolved) == len(
            _live_fixture_paths()
        )

    def test_every_review_records_a_rationale_and_date(self):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        for review in ledger.reviews.values():
            assert review.rationale.strip(), review.fixture_id
            assert review.adjudicated_on.strip(), review.fixture_id
            assert review.labeled_case_id.strip(), review.fixture_id

    def test_changed_oracles_record_what_they_changed_from(self):
        """A changed label must say what it used to be, so the edit is reviewable."""
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        changed = [r for r in ledger.reviews.values() if r.changed_oracle]
        assert changed, "this ledger exists because labels needed correcting"
        for review in changed:
            assert review.previous_verdict, review.fixture_id
            assert review.previous_status, review.fixture_id

    def test_unresolved_reviews_carry_no_label(self):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        for review in ledger.reviews.values():
            if not review.is_resolved:
                assert review.correct_verdict is None
                assert review.expected_status is None

    def test_captured_responses_are_still_present_and_non_empty(self):
        """Adjudication corrects oracles; it never removes the evidence."""
        for path in _live_fixture_paths():
            raw = _read(path)
            assert raw["captured_verifier_response"].get("verdict")
            assert raw["finding"].get("issue")


# ---------------------------------------------------------------------------
# 2. Evidence identity
# ---------------------------------------------------------------------------


class TestEvidenceDigest:
    def test_relabelling_does_not_change_the_digest(self):
        """Correcting an oracle must not invalidate the record that justifies it."""
        raw = _read(_live_fixture_paths()[0])
        before = evidence_digest(raw)
        raw["ground_truth"]["correct_verdict"] = "DISPUTED"
        raw["ground_truth"]["notes"] = "totally different note"
        assert evidence_digest(raw) == before

    def test_editing_a_captured_response_changes_the_digest(self):
        raw = _read(_live_fixture_paths()[0])
        before = evidence_digest(raw)
        raw["captured_verifier_response"]["verdict"] = "DISPUTED"
        assert evidence_digest(raw) != before

    def test_editing_the_finding_changes_the_digest(self):
        raw = _read(_live_fixture_paths()[0])
        before = evidence_digest(raw)
        raw["finding"]["issue"] = "rewritten to match the new expectation"
        assert evidence_digest(raw) != before

    def test_ground_truth_is_not_an_immutable_key(self):
        assert "ground_truth" not in IMMUTABLE_FIXTURE_KEYS

    def test_digest_is_insensitive_to_key_order(self):
        raw = _read(_live_fixture_paths()[0])
        reordered = dict(reversed(list(raw.items())))
        assert evidence_digest(reordered) == evidence_digest(raw)


# ---------------------------------------------------------------------------
# 3. Validation fails loudly rather than excluding more cases
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "fixtures_live"
    shutil.copytree(_LIVE_DIR, dest)
    return dest


class TestValidationFailures:
    def test_missing_adjudication_record_is_an_error(self, live_copy: Path):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        victim = next(iter(sorted(ledger.reviews)))
        del ledger.reviews[victim]
        validation = validate_against_fixtures(ledger, sorted(live_copy.glob("*.json")))
        assert not validation.ok
        assert any(victim in e and "no adjudication record" in e for e in validation.errors)

    def test_orphan_ledger_entry_is_an_error(self, live_copy: Path):
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        some = next(iter(ledger.reviews.values()))
        ledger.reviews["live_not_a_real_fixture_0"] = some
        validation = validate_against_fixtures(ledger, sorted(live_copy.glob("*.json")))
        assert not validation.ok
        assert any("no matching fixture" in e for e in validation.errors)

    def test_edited_captured_evidence_is_an_error(self, live_copy: Path):
        target = sorted(live_copy.glob("*.json"))[0]
        raw = _read(target)
        raw["captured_verifier_response"]["explanation"] = "rewritten after the fact"
        target.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        validation = validate_against_fixtures(ledger, sorted(live_copy.glob("*.json")))
        assert not validation.ok
        assert any("captured evidence changed" in e for e in validation.errors)

    def test_label_drifting_from_the_ledger_is_an_error(self, live_copy: Path):
        """The fixture file and the record that justifies it must not disagree."""
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        resolved_id = next(r.fixture_id for r in ledger.reviews.values() if r.is_resolved)
        target = next(
            p for p in sorted(live_copy.glob("*.json")) if _read(p)["fixture_id"] == resolved_id
        )
        raw = _read(target)
        raw["ground_truth"]["correct_verdict"] = "DISPUTED"
        target.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        validation = validate_against_fixtures(ledger, sorted(live_copy.glob("*.json")))
        assert not validation.ok
        assert any("disagrees with the ledger" in e for e in validation.errors)

    def test_a_historical_model_error_is_not_itself_a_validation_failure(self):
        """Validation must never require the captured verdict to match the oracle.

        A fixture that preserves a genuine model mistake is a valid fixture; the
        ledger records the disagreement rather than erasing it.
        """
        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        validation = validate_against_fixtures(ledger, _live_fixture_paths())
        assert validation.ok
        # Reachability of the mismatch case: nothing in validation inspects the
        # captured verdict at all.
        for path in _live_fixture_paths():
            raw = _read(path)
            review = ledger.get(raw["fixture_id"])
            if review and review.is_resolved:
                # No assertion tying these together — that is the point.
                assert raw["captured_verifier_response"]["verdict"]


class TestLedgerParsing:
    def _write(self, tmp_path: Path, reviews: dict) -> Path:
        path = tmp_path / "ledger.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": LEDGER_SCHEMA_VERSION,
                    "adjudicated_on": "2026-09-09",
                    "reviews": reviews,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _entry(self, **over) -> dict:
        base = {
            "labeled_case_id": "case",
            "evidence_sha256": "deadbeef",
            "review_state": "resolved",
            "adjudicated_on": "2026-09-09",
            "rationale": "because",
            "correct_verdict": "CONFIRMED",
            "expected_status": "VERIFIED_SUPPORTED",
        }
        base.update(over)
        return base

    def test_resolved_without_a_label_is_rejected(self, tmp_path: Path):
        path = self._write(tmp_path, {"f": self._entry(correct_verdict=None)})
        with pytest.raises(ValueError, match="must carry both"):
            load_ledger(path)

    def test_unresolved_with_a_label_is_rejected(self, tmp_path: Path):
        path = self._write(tmp_path, {"f": self._entry(review_state="unresolved")})
        with pytest.raises(ValueError, match="must not carry"):
            load_ledger(path)

    def test_empty_rationale_is_rejected(self, tmp_path: Path):
        path = self._write(tmp_path, {"f": self._entry(rationale="   ")})
        with pytest.raises(ValueError, match="rationale"):
            load_ledger(path)

    def test_unknown_review_state_is_rejected(self, tmp_path: Path):
        path = self._write(tmp_path, {"f": self._entry(review_state="probably-fine")})
        with pytest.raises(ValueError, match="review_state"):
            load_ledger(path)

    def test_wrong_schema_version_is_rejected(self, tmp_path: Path):
        path = tmp_path / "ledger.json"
        path.write_text(
            json.dumps(
                {"schema_version": 99, "adjudicated_on": "2026-09-09", "reviews": {}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="schema_version"):
            load_ledger(path)


# ---------------------------------------------------------------------------
# 4. The runner actually consumes the ledger
# ---------------------------------------------------------------------------


class TestRunnerConsumesTheLedger:
    def test_reviewed_only_requires_a_ledger(self, capsys):
        from evals.calibration.runner import main

        code = main(["--fixtures-dir", str(_LIVE_DIR), "--reviewed-only"])
        assert code == 2
        assert "requires --oracle-reviews" in capsys.readouterr().err

    def test_reviewed_only_scores_resolved_and_names_exclusions(self, capsys):
        from evals.calibration.runner import main

        ledger = load_ledger(DEFAULT_LEDGER_PATH)
        validation = validate_against_fixtures(ledger, _live_fixture_paths())

        code = main(
            [
                "--fixtures-dir",
                str(_LIVE_DIR),
                "--oracle-reviews",
                str(DEFAULT_LEDGER_PATH),
                "--reviewed-only",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0, out
        assert "Reviewed-only scope" in out
        # The scored denominator is the resolved count, not the fixture count.
        assert f"**Total fixtures:** {len(validation.resolved_ids)}" in out
        # Every exclusion is named with its reason.
        for fixture_id, _ in validation.unresolved:
            assert fixture_id in out

    def test_inconsistent_metadata_fails_instead_of_excluding_more(
        self, live_copy: Path, capsys
    ):
        """A fixture with no record must fail the run, not silently drop out."""
        from evals.calibration.runner import main

        raw = _read(sorted(live_copy.glob("*.json"))[0])
        raw["fixture_id"] = "live_unadjudicated_newcomer_0"
        (live_copy / "live_unadjudicated_newcomer_0.json").write_text(
            json.dumps(raw, indent=2), encoding="utf-8"
        )

        code = main(
            [
                "--fixtures-dir",
                str(live_copy),
                "--oracle-reviews",
                str(DEFAULT_LEDGER_PATH),
                "--reviewed-only",
            ]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "validation failed" in err
        assert "live_unadjudicated_newcomer_0" in err

    def test_default_invocation_ignores_the_ledger(self, capsys):
        """Without the flags the runner is the historical diagnostic replay."""
        from evals.calibration.runner import main

        main(["--fixtures-dir", str(_LIVE_DIR)])
        out = capsys.readouterr().out
        assert "Reviewed-only scope" not in out
        assert f"**Total fixtures:** {len(_live_fixture_paths())}" in out
