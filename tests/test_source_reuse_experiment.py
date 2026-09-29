"""Plan EX-04, part B: shared source resolution within a run.

The prototype (``src/verification/source_reuse.py``) hands a later
verification the passages the API cited while verifying an earlier finding
with the same claim context. These tests pin what the plan asks for:

* **key separation** — every field of the claim context separates keys, and a
  bare standard name is not a context;
* **invalidation** — stale, incompatible, insufficient, and unkeyable lookups
  never supply anything;
* **fallback** — every outcome but a supplying hit sends the exact request the
  switch-off path sends, and an escalation always resolves fresh;
* **provenance** — supplied passages are never recorded as this conversation's
  retrieval, never harvested again, never cached, and the report says so;
* **no double billing** — reuse adds no attempt and no usage;
* the switch (``shadow`` / ``supply``), the batch fallback to shadow, and the
  independence of reuse from evidence validation.
"""
from __future__ import annotations

import copy
import logging
from types import SimpleNamespace

import pytest

import src.orchestration.pipeline as pipeline
import src.verification.verifier as V
from src.core import api_config
from src.core.attempt_usage import (
    OPERATION_VERIFICATION,
    ROLE_PRIMARY,
    TRANSPORT_REALTIME,
    attempt_dicts,
    known_attempt,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.modules.registry import get_module
from src.orchestration.diagnostics import DiagnosticsReport, record_verification_findings
from src.output.report_exporter import _retrieval_text
from src.review.reviewer import Finding
from src.verification import source_reuse as sr
from src.verification.verification_cache import (
    _SKIPPED_FIELDS,
    VerificationCache,
    cache_ineligibility_reason,
)
from src.verification.verification_routing import build_verification_request, select_routing
from src.verification.verifier import OUTCOME_VERDICT, VerificationResult
from tests.fixtures.verification_drivers import (
    ScriptedStreamClient,
    message,
    search_blocks,
    verdict_call,
    verdict_payload,
)

ORIGIN_URL = "https://www.nfpa.org/codes-and-standards/nfpa-13/constructed-origin"
PASSAGE = "The clearance below the deflector shall be 18 in. or greater."
CA = get_module("california_k12_mep")


def _finding(**overrides) -> Finding:
    fields = dict(
        severity="MEDIUM",
        fileName="21 13 13 - Wet-Pipe Sprinkler Systems.docx",
        section="3.02",
        issue="The spec allows 12 in. below deflectors; the standard requires 18 in.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13 §8.15.1",
        confidence=0.8,
    )
    fields.update(overrides)
    finding = Finding(**fields)
    finding.finding_id = f"rf-{abs(hash(finding.issue)) % 10**12:012d}"
    return finding


def _cite(url: str = ORIGIN_URL, text: str = PASSAGE, **overrides) -> dict:
    record = {
        "type": "web_search_result_location",
        "recognized": True,
        "tool": "web_search",
        "url": url,
        "title": "NFPA 13",
        "cited_text": text,
        "resolution": "direct",
        "retrieved": True,
        "verdict_cites_source": True,
        "attempt_id": "message:msg_origin",
        "model": "claude-sonnet-5",
    }
    record.update(overrides)
    return record


def _fresh_result(**overrides) -> VerificationResult:
    fields = dict(
        verdict="CONFIRMED",
        sources=[ORIGIN_URL],
        accepted_sources=[ORIGIN_URL],
        searched_sources=[ORIGIN_URL],
        grounded=True,
        source_quote=PASSAGE,
        cache_status="miss",
        outcome=OUTCOME_VERDICT,
        native_citations=[_cite()],
    )
    fields.update(overrides)
    return VerificationResult(**fields)


def _context(finding: Finding, **overrides):
    kwargs = dict(cycle=DEFAULT_CYCLE, module_id=CA.module_id)
    kwargs.update(overrides)
    return sr.context_for(finding, **kwargs)


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store_with_origin(*, clock=None, mode=sr.MODE_SUPPLY):
    store = sr.SourceStore(clock=clock or _Clock())
    origin = _finding(issue="Origin: NFPA 13 clearance below deflectors is 18 in.")
    store.harvest(_context(origin), _fresh_result(), finding_id="rf-origin")
    return store


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class TestSwitch:
    @pytest.mark.parametrize("value, mode", [("shadow", "shadow"), ("SUPPLY", "supply"), (" shadow ", "shadow")])
    def test_modes(self, monkeypatch, value, mode):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", value)
        assert api_config.source_reuse_mode() == mode

    @pytest.mark.parametrize("value", ["", "0", "off", "false", "no"])
    def test_off(self, monkeypatch, value):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", value)
        assert api_config.source_reuse_mode() is None

    @pytest.mark.parametrize("value", ["1", "true", "on", "reuse"])
    def test_a_truthy_value_does_not_choose_a_mode(self, monkeypatch, caplog, value):
        # "on" cannot say whether requests may change, so it is refused.
        monkeypatch.setattr(api_config, "_WARNED_EX04_VALUES", set())
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", value)
        with caplog.at_level(logging.WARNING, logger="src.core.api_config"):
            assert api_config.source_reuse_mode() is None
        assert "use shadow or supply" in caplog.text


# ---------------------------------------------------------------------------
# Key separation
# ---------------------------------------------------------------------------


class TestContext:
    def test_no_recognizable_reference_is_not_keyable(self):
        assert _context(_finding(codeReference="")) is None
        assert _context(_finding(codeReference="Owner design guide")) is None

    def test_edition_named_in_the_claim_wins_over_the_pin(self):
        named = _context(_finding(codeReference="NFPA 13-2019 §8.15.1"))
        pinned = _context(_finding())
        assert dict(named.editions)["NFPA 13"] == "2019"
        assert dict(pinned.editions)["NFPA 13"] == "2025"  # California 2025 pins NFPA 13-2025

    def test_asce_pin_and_named_edition_share_a_form(self):
        pinned = _context(_finding(codeReference="ASCE 7 §13.3"))
        named = _context(_finding(codeReference="ASCE 7-22 §13.3"))
        assert pinned.editions == named.editions == (("ASCE 7", "2022"),)
        assert pinned.key() == named.key()

    @pytest.mark.parametrize(
        "field_change",
        [
            {"finding": {"codeReference": "NFPA 13 §8.15.2"}},  # another section
            {"finding": {"codeReference": "NFPA 14 §8.15.1"}},  # another standard
            {"finding": {"codeReference": "NFPA 13-2019 §8.15.1"}},  # another edition
            {"context": {"jurisdiction_fingerprint": "a1b2c3d4e5f60718"}},
            {"context": {"basis_fingerprint": "basisfp"}},
            {"context": {"module_id": "datacenter_fire"}},
            {"finding": {"issue": "The manufacturer datasheet lists 18 in. clearance for this model number."}},
        ],
    )
    def test_every_field_separates_keys(self, field_change):
        base = _context(_finding())
        finding = _finding(**field_change.get("finding", {}))
        other = _context(finding, **field_change.get("context", {}))
        assert other is not None
        assert other.key() != base.key()
        assert base.differences(other)

    def test_a_different_cycle_separates_keys(self):
        fire = get_module("datacenter_fire")
        base = _context(_finding())
        other = _context(_finding(), cycle=fire.cycle)
        assert "cycle" in base.differences(other)
        assert base.key() != other.key()

    def test_policy_version_is_part_of_the_key(self):
        base = _context(_finding())
        bumped = sr.SourceContext(**{**base.__dict__, "policy_version": "sr0"})
        assert bumped.key() != base.key()

    def test_equal_claim_contexts_share_a_key(self):
        a = _context(_finding(issue="First finding about 18 in. clearance."))
        b = _context(_finding(issue="Second finding, worded differently, about the clearance."))
        assert a == b and a.key() == b.key()


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


class TestHarvest:
    def test_harvests_api_cited_text_of_retrieved_sources(self):
        sources = sr.harvest_sources(_fresh_result(), finding_id="rf-1", now=5.0)
        assert len(sources) == 1
        assert sources[0].url == ORIGIN_URL and sources[0].passages == (PASSAGE,)
        assert sources[0].origin_finding_id == "rf-1" and sources[0].retrieved_at == 5.0
        assert sources[0].origin_attempt_id == "message:msg_origin"

    def test_never_harvests_the_models_own_quote(self):
        result = _fresh_result(source_quote="MODEL-WRITTEN QUOTE", native_citations=[])
        assert sr.harvest_sources(result, now=1.0) == []

    @pytest.mark.parametrize(
        "record",
        [
            _cite(tool="search_result", type="search_result_location"),  # supplied earlier: never re-harvested
            _cite(retrieved=False),
            _cite(resolution="unresolved", url=""),
            _cite(recognized=False),
            _cite(cited_text="   "),
        ],
    )
    def test_skips_what_is_not_this_conversations_retrieval(self, record):
        assert sr.harvest_sources(_fresh_result(native_citations=[record]), now=1.0) == []

    @pytest.mark.parametrize("status", ["hit", "shared", "local_skip", "n/a"])
    def test_only_fresh_results(self, status):
        assert sr.harvest_sources(_fresh_result(cache_status=status), now=1.0) == []

    def test_bounded(self):
        records = [_cite(url=f"https://www.nfpa.org/p{i}", text=f"passage {i} " * 3) for i in range(20)]
        records += [_cite(text=f"another passage {i} of the origin source") for i in range(10)]
        sources = sr.harvest_sources(_fresh_result(native_citations=records), now=1.0)
        assert len(sources) <= sr.MAX_SOURCES_PER_CONTEXT
        assert all(len(s.passages) <= sr.MAX_PASSAGES_PER_SOURCE for s in sources)


# ---------------------------------------------------------------------------
# Lookup: invalidation and fallback
# ---------------------------------------------------------------------------


class TestLookup:
    def test_hit(self):
        store = _store_with_origin()
        lookup = store.lookup(_context(_finding()))
        assert lookup.status == sr.LOOKUP_HIT and lookup.supplies
        assert lookup.supplied_urls() == [ORIGIN_URL]

    def test_absent(self):
        lookup = sr.SourceStore().lookup(_context(_finding()))
        assert lookup.status == sr.LOOKUP_ABSENT and not lookup.supplies

    def test_incompatible_names_the_differing_fields(self):
        store = _store_with_origin()
        lookup = store.lookup(_context(_finding(codeReference="NFPA 13-2019 §8.15.1")))
        assert lookup.status == sr.LOOKUP_INCOMPATIBLE and not lookup.supplies
        assert "editions" in lookup.reason

    def test_stale(self):
        clock = _Clock()
        store = _store_with_origin(clock=clock)
        clock.now += sr.DEFAULT_MAX_AGE_SECONDS + 1
        lookup = store.lookup(_context(_finding()))
        assert lookup.status == sr.LOOKUP_STALE and not lookup.supplies

    def test_fresh_just_inside_the_age_limit(self):
        clock = _Clock()
        store = _store_with_origin(clock=clock)
        clock.now += sr.DEFAULT_MAX_AGE_SECONDS
        assert store.lookup(_context(_finding())).status == sr.LOOKUP_HIT

    def test_insufficient(self):
        store = sr.SourceStore()
        context = _context(_finding())
        store.record(context, [sr.ResolvedSource(url=ORIGIN_URL, title="", passages=(), tool="web_search", retrieved_at=0.0)])
        assert store.lookup(context).status == sr.LOOKUP_INSUFFICIENT

    def test_not_keyable(self):
        lookup = sr.SourceStore().lookup(None)
        assert lookup.status == sr.LOOKUP_NOT_KEYABLE and not lookup.supplies

    def test_shadow_hit_supplies_nothing(self):
        store = _store_with_origin()
        lookup = store.lookup(_context(_finding()), mode=sr.MODE_SHADOW)
        assert lookup.hit and not lookup.supplies
        assert lookup.supplied_urls() == []
        assert sr.user_content("PROMPT", lookup) == "PROMPT"
        record = lookup.to_dict()
        assert record["mode"] == "shadow" and record["supplied"] is False
        assert record["sources"] and record["passages"] == 1

    def test_passage_budget_per_request(self):
        store = sr.SourceStore()
        context = _context(_finding())
        for i in range(sr.MAX_SOURCES_PER_CONTEXT):
            store.record(context, [sr.ResolvedSource(
                url=f"https://www.nfpa.org/s{i}", title="t", passages=tuple(f"p{i}-{j}" for j in range(4)),
                tool="web_search", retrieved_at=1_000_000.0)])
        store._clock = _Clock()
        lookup = store.lookup(context)
        assert sum(len(s.passages) for s in lookup.sources) == sr.MAX_PASSAGES_PER_REQUEST

    def test_store_is_bounded(self, monkeypatch):
        monkeypatch.setattr(sr, "MAX_CONTEXTS", 3)
        store = sr.SourceStore()
        for section in range(5):
            finding = _finding(codeReference=f"NFPA 13 §8.15.{section + 1}")
            store.harvest(_context(finding), _fresh_result())
        assert len(store) == 3


# ---------------------------------------------------------------------------
# Request content
# ---------------------------------------------------------------------------


class TestRequestContent:
    def test_blocks(self):
        lookup = _store_with_origin().lookup(_context(_finding()))
        blocks = sr.search_result_blocks(lookup)
        assert blocks == [
            {
                "type": "search_result",
                "source": ORIGIN_URL,
                "title": "NFPA 13",
                "content": [{"type": "text", "text": PASSAGE}],
                "citations": {"enabled": True},
            }
        ]
        content = sr.user_content("PROMPT", lookup)
        assert content[-2] == {"type": "text", "text": sr.REUSE_NOTE}
        assert content[-1] == {"type": "text", "text": "PROMPT"}

    @pytest.mark.parametrize("lookup", [None, sr.ReuseLookup(sr.LOOKUP_ABSENT), sr.not_keyable()])
    def test_no_supply_keeps_the_prompt_string(self, lookup):
        assert sr.user_content("PROMPT", lookup) == "PROMPT"
        assert sr.search_result_blocks(lookup) == []

    def test_the_builder_is_byte_identical_without_content(self):
        finding = _finding()
        decision = select_routing(finding, escalated=False, local_skip=False, cycle=DEFAULT_CYCLE)
        plain = build_verification_request(decision, prompt="P", system_prompt="S")
        explicit = build_verification_request(decision, prompt="P", system_prompt="S", user_content=None)
        assert plain.params == explicit.params
        assert plain.params["messages"] == [{"role": "user", "content": "P"}]


# ---------------------------------------------------------------------------
# The real-time verifier, driven for real
# ---------------------------------------------------------------------------


def _cited_text_block(url: str, text: str) -> dict:
    return {
        "type": "text",
        "text": "The supplied passage settles it.",
        "citations": [
            {
                "type": "search_result_location",
                "source": url,
                "title": "NFPA 13",
                "cited_text": text,
                "search_result_index": 0,
                "start_block_index": 0,
                "end_block_index": 1,
            }
        ],
    }


def _verify(monkeypatch, route, *, finding=None, lookup=None, cache=None):
    client = ScriptedStreamClient(route if callable(route) else (lambda _k: route))
    monkeypatch.setattr(V, "_get_client", lambda **_: client)
    monkeypatch.setattr(V.time, "sleep", lambda _s: None)
    kwargs = {"source_lookup": lookup} if lookup is not None else {}
    result = V.verify_finding(finding or _finding(), max_retries=0, cycle=DEFAULT_CYCLE, cache=cache, **kwargs)
    return result, client


class TestRealtimeVerifier:
    def test_a_supplied_passage_can_ground_a_verdict_without_a_search(self, monkeypatch):
        lookup = _store_with_origin().lookup(_context(_finding()))
        reply = message(
            [
                _cited_text_block(ORIGIN_URL, PASSAGE),
                verdict_call(verdict_payload(sources=[ORIGIN_URL], source_quote=PASSAGE)),
            ],
            searches=0,
        )
        cache = VerificationCache()
        result, client = _verify(monkeypatch, reply, lookup=lookup, cache=cache)

        content = client.calls[0]["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "search_result" and content[0]["source"] == ORIGIN_URL
        assert content[-1]["type"] == "text" and "<finding" in content[-1]["text"]

        assert result.verdict == "CONFIRMED" and result.grounded
        assert result.accepted_sources == [ORIGIN_URL]
        # Never presented as this conversation's retrieval.
        assert result.searched_sources == [] and result.fetched_sources == []
        assert result.reused_sources == [ORIGIN_URL]
        assert result.source_reuse["supplied_to"] == "initial"
        assert result.source_reuse["accepted_via_reuse"] == [ORIGIN_URL]
        (record,) = result.native_citations
        assert record["tool"] == "search_result" and record["retrieved"] is False
        assert record["verdict_cites_source"] is True
        # And never cached: the provenance would not survive a replay.
        assert cache_ineligibility_reason(result) is not None
        assert cache.get(_finding(), cycle=DEFAULT_CYCLE) is None

    def test_without_a_supplying_lookup_the_request_is_unchanged(self, monkeypatch):
        reply = message(search_blocks() + [verdict_call(verdict_payload())])
        _, plain = _verify(monkeypatch, reply)
        for lookup in (sr.ReuseLookup(sr.LOOKUP_ABSENT), _store_with_origin().lookup(_context(_finding()), mode=sr.MODE_SHADOW)):
            result, client = _verify(monkeypatch, reply, lookup=lookup)
            assert client.calls == plain.calls
            assert result.reused_sources == []
            assert result.source_reuse["supplied"] is False
        assert isinstance(plain.calls[0]["messages"][0]["content"], str)

    def test_no_lookup_records_nothing(self, monkeypatch):
        result, _ = _verify(monkeypatch, message(search_blocks() + [verdict_call(verdict_payload())]))
        assert result.source_reuse is None and result.reused_sources == []

    def test_the_escalation_resolves_fresh(self, monkeypatch):
        finding = _finding(severity="HIGH")
        lookup = _store_with_origin().lookup(_context(finding))
        assert lookup.supplies

        def route(kwargs):
            if "opus" in kwargs["model"]:
                return message(search_blocks() + [verdict_call(verdict_payload())])
            return message(search_blocks() + [verdict_call(verdict_payload(verdict="UNVERIFIED", sources=[], source_quote=""))])

        result, client = _verify(monkeypatch, route, finding=finding, lookup=lookup)
        assert len(client.calls) == 2
        assert isinstance(client.calls[0]["messages"][0]["content"], list)
        assert isinstance(client.calls[1]["messages"][0]["content"], str)
        assert result.escalated and result.reused_sources == []
        assert result.source_reuse["kept_pass"] == "escalation"
        assert result.source_reuse["accepted_via_reuse"] == []

    def test_reuse_adds_no_attempt(self, monkeypatch):
        lookup = _store_with_origin().lookup(_context(_finding()))
        reply = message(search_blocks() + [verdict_call(verdict_payload())])
        supplied, _ = _verify(monkeypatch, reply, lookup=lookup)
        plain, _ = _verify(monkeypatch, reply)
        assert len(supplied.call_usage) == len(plain.call_usage) == 1
        assert supplied.input_tokens == plain.input_tokens


# ---------------------------------------------------------------------------
# The pipeline: two rounds, shadow and supply
# ---------------------------------------------------------------------------


class _FakeVerify:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def __call__(self, finding, **kwargs):
        lookup = kwargs.get("source_lookup")
        self.calls.append((finding.issue, lookup))
        url = f"https://www.nfpa.org/constructed/{abs(hash(finding.issue)) % 1000}"
        result = _fresh_result(
            sources=[url], accepted_sources=[url], searched_sources=[url],
            native_citations=[_cite(url=url, text=f"Passage retrieved for: {finding.issue}")],
            call_usage=attempt_dicts([known_attempt(
                {"input_tokens": 100, "output_tokens": 10},
                operation=OPERATION_VERIFICATION, role=ROLE_PRIMARY, transport=TRANSPORT_REALTIME,
                model="claude-sonnet-5", message_id=f"msg_{abs(hash(finding.issue)) % 10**9}",
            )]),
            input_tokens=100, output_tokens=10, model_used="claude-sonnet-5", transport=TRANSPORT_REALTIME,
        )
        if lookup is not None:
            result.source_reuse = sr.source_reuse_record(lookup, result)
        return result


@pytest.fixture
def fake_verify(monkeypatch):
    fake = _FakeVerify()
    monkeypatch.setattr(pipeline, "verify_finding", fake)
    monkeypatch.setattr(pipeline, "prepare_findings_for_verification", lambda findings, **_k: list(findings))
    return fake


def _round(findings, *, cache, transport="realtime", log=None):
    pipeline.verify_findings_for_run(
        findings, module=CA, transport=transport, cache=cache,
        log=log or (lambda *_a, **_k: None),
    )


class TestPipeline:
    def test_off_passes_nothing_and_records_nothing(self, monkeypatch, fake_verify):
        monkeypatch.delenv("SPEC_CRITIC_SOURCE_REUSE", raising=False)
        cache = VerificationCache()
        first = [_finding(issue="round one")]
        second = [_finding(issue="round two")]
        _round(first, cache=cache)
        _round(second, cache=cache)
        assert [lookup for _, lookup in fake_verify.calls] == [None, None]
        assert second[0].verification.source_reuse is None
        assert cache._source_store is None  # never even created

    def test_supply_second_round_receives_the_first_rounds_passages(self, monkeypatch, fake_verify):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "supply")
        cache = VerificationCache()
        _round([_finding(issue="round one: 18 in. below deflectors")], cache=cache)
        second = [
            _finding(issue="round two: same reference"),
            _finding(issue="round two: another edition", codeReference="NFPA 13-2019 §8.15.1"),
            _finding(issue="round two: no reference", codeReference=""),
        ]
        _round(second, cache=cache)
        statuses = {issue: lookup.status for issue, lookup in fake_verify.calls[1:]}
        assert statuses == {
            "round two: same reference": sr.LOOKUP_HIT,
            "round two: another edition": sr.LOOKUP_INCOMPATIBLE,
            "round two: no reference": sr.LOOKUP_NOT_KEYABLE,
        }
        hit = fake_verify.calls[1][1]
        assert hit.supplies and hit.sources[0].passages == ("Passage retrieved for: round one: 18 in. below deflectors",)

    def test_lookups_are_taken_before_the_round(self, monkeypatch, fake_verify):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "supply")
        cache = VerificationCache()
        _round([_finding(issue="a"), _finding(issue="b")], cache=cache)
        # b ran after a, but a's passages were recorded only after the round.
        assert [lookup.status for _, lookup in fake_verify.calls] == [sr.LOOKUP_ABSENT, sr.LOOKUP_ABSENT]

    def test_shadow_records_and_supplies_nothing(self, monkeypatch, fake_verify):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "shadow")
        cache = VerificationCache()
        _round([_finding(issue="round one")], cache=cache)
        second = [_finding(issue="round two")]
        _round(second, cache=cache)
        assert [lookup for _, lookup in fake_verify.calls] == [None, None]
        record = second[0].verification.source_reuse
        assert record["mode"] == "shadow" and record["status"] == sr.LOOKUP_HIT
        assert record["supplied"] is False and record["supplied_to"] == ""

    def test_shadow_records_the_lookup_taken_before_the_round(self, monkeypatch, fake_verify):
        # A shadow record says what could have been supplied, so it must not
        # count a match that only existed after the round's own harvest.
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "shadow")
        findings = [_finding(issue="a"), _finding(issue="b")]
        _round(findings, cache=VerificationCache())
        assert [f.verification.source_reuse["status"] for f in findings] == [sr.LOOKUP_ABSENT] * 2

    def test_batch_supply_falls_back_to_shadow(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "supply")
        monkeypatch.setattr(pipeline, "_WARNED_SOURCE_REUSE_TRANSPORT", set())
        logs = []
        plan = pipeline._source_reuse_plan(
            [_finding()], module=CA, transport="batch", cache=VerificationCache(),
            jurisdiction_fingerprint=None, governing_basis=None,
            log=lambda msg, **kw: logs.append((kw.get("level"), msg)),
        )
        assert plan.mode == "shadow"
        assert plan.lookup_for(_finding()) is None
        assert any(level == "warning" and "shadow" in msg for level, msg in logs)

    def test_no_cache_means_no_plan(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "shadow")
        plan = pipeline._source_reuse_plan(
            [_finding()], module=CA, transport="realtime", cache=None,
            jurisdiction_fingerprint=None, governing_basis=None, log=lambda *_a, **_k: None,
        )
        assert plan is None

    def test_a_shared_follower_does_not_repeat_the_leaders_lookup(self):
        leader = _fresh_result(verdict="UNVERIFIED", reused_sources=[ORIGIN_URL])
        leader.source_reuse = {"status": sr.LOOKUP_HIT, "supplied": True}
        leader.evidence_assessment = {"assessment": "not_applicable"}
        clone = pipeline._shared_clone(leader)
        assert clone.source_reuse is None and clone.evidence_assessment is None
        assert clone.reused_sources == [ORIGIN_URL]  # how the inherited verdict was reached
        assert leader.source_reuse is not None  # the leader keeps its own

    def test_replays_get_no_record(self, monkeypatch):
        store = _store_with_origin()
        finding = _finding()
        finding.verification = _fresh_result(cache_status="hit")
        plan = pipeline._SourceReusePlan(store, {id(finding): _context(finding)},
                                         {id(finding): store.lookup(_context(finding), mode="shadow")}, "shadow")
        assert plan.stamp_unsupplied([finding]) == 0
        assert finding.verification.source_reuse is None


# ---------------------------------------------------------------------------
# Provenance surfaces and billing
# ---------------------------------------------------------------------------


class TestProvenanceAndBilling:
    def test_the_report_says_the_passages_were_given(self):
        plain = _fresh_result()
        reused = copy.deepcopy(plain)
        reused.reused_sources = [ORIGIN_URL]
        baseline = _retrieval_text(plain)
        assert "earlier in this run" not in baseline  # unchanged without reuse
        text = _retrieval_text(reused)
        assert text.startswith(baseline[:-1] + "; it was also given passages from 1 source(s)")
        assert "which this verification did not retrieve itself." in text

    def test_runtime_only_fields(self):
        assert {"reused_sources", "source_reuse", "evidence_assessment"} <= _SKIPPED_FIELDS

    def test_reused_sources_are_never_cached(self):
        result = _fresh_result(reused_sources=[ORIGIN_URL])
        assert "reused" in cache_ineligibility_reason(result)
        cache = VerificationCache()
        cache.put(_finding(), cycle=DEFAULT_CYCLE, result=result)
        assert cache.get(_finding(), cycle=DEFAULT_CYCLE) is None
        assert cache_ineligibility_reason(_fresh_result()) is None

    def test_no_double_billing(self, monkeypatch, fake_verify):
        monkeypatch.setenv("SPEC_CRITIC_SOURCE_REUSE", "supply")
        cache = VerificationCache()
        first = [_finding(issue="origin")]
        second = [_finding(issue="reuser")]
        _round(first, cache=cache)
        _round(second, cache=cache)
        diag = DiagnosticsReport()
        record_verification_findings(diag, first + second, phase="verification", transport="realtime")
        summary = diag.summary()
        # One attempt each; reuse adds none.
        assert summary["cost_summary"]["estimated_cost_usd"]["priced_calls"] == 2
        assert summary["total_input_tokens"] == 200
        reuse = summary["source_reuse"]
        assert reuse["lookups"] == 2 and reuse["findings_supplied"] == 1
        assert reuse["by_status"] == {sr.LOOKUP_ABSENT: 1, sr.LOOKUP_HIT: 1}
        assert "Source reuse (experiment, sr1): 2 lookup(s)" in diag.to_text()

    def test_diagnostics_unchanged_when_off(self):
        finding = _finding()
        finding.verification = _fresh_result()
        diag = DiagnosticsReport()
        record_verification_findings(diag, [finding], phase="verification", transport="realtime")
        assert "source_reuse" not in diag.events[0].data
        assert "source_reuse" not in diag.summary()


class TestIndependence:
    @pytest.mark.parametrize(
        "validation, reuse",
        [(None, "shadow"), ("observe", None), ("observe", "supply")],
    )
    def test_each_switch_writes_only_its_own_record(self, monkeypatch, fake_verify, validation, reuse):
        for name, value in (("SPEC_CRITIC_EVIDENCE_VALIDATION", validation), ("SPEC_CRITIC_SOURCE_REUSE", reuse)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        cache = VerificationCache()
        _round([_finding(issue="one")], cache=cache)
        second = [_finding(issue="two")]
        _round(second, cache=cache)
        result = second[0].verification
        assert (result.evidence_assessment is not None) == (validation is not None)
        assert (result.source_reuse is not None) == (reuse is not None)
        diag = DiagnosticsReport()
        record_verification_findings(diag, second, phase="verification", transport="realtime")
        summary = diag.summary()
        assert ("evidence_validation" in summary) == (validation is not None)
        assert ("source_reuse" in summary) == (reuse is not None)
