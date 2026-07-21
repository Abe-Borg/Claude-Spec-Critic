"""Hermetic concurrency contracts for routed-program requirements research."""
from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

from src.core.project_profile import ProjectProfile
from src.modules import DEFAULT_MODULE, ResearchDimension
from src.research import DimensionStatus, ResearchItem, run_requirements_research
from src.research import requirements_research as rr


def _profile() -> ProjectProfile:
    return ProjectProfile(
        city="Markham",
        state_or_province="ON",
        country="Canada",
        client_name="ExampleCo",
    )


def _dimension(dimension_id: str) -> ResearchDimension:
    return ResearchDimension(
        dimension_id=dimension_id,
        title=dimension_id.title(),
        prompt_template=f"{dimension_id.upper()} research brief for {{city}}.",
    )


def _module(module_id: str, *dimension_ids: str):
    return dataclasses.replace(
        DEFAULT_MODULE,
        module_id=module_id,
        display_name=module_id,
        project_profile_enabled=True,
        research_persona="You are a test research assistant.",
        research_dimensions=tuple(_dimension(item) for item in dimension_ids),
        compliance_persona="You are a test compliance reviewer.",
        compliance_severity_definitions="- CRITICAL - permit-blocking omission.",
    )


def _outcome(dimension_id: str) -> rr._DimensionOutcome:
    return rr._DimensionOutcome(
        status=DimensionStatus(
            dimension_id=dimension_id,
            status="completed",
            item_count=1,
            grounded_count=1,
        ),
        items=[
            ResearchItem(
                item_id=f"r-{dimension_id:0<12}"[:14],
                dimension_id=dimension_id,
                topic=dimension_id.title(),
                category="governing_code",
                requirement=f"Requirement from {dimension_id}.",
                accepted_sources=[f"https://example.test/{dimension_id}"],
                grounded=True,
            )
        ],
    )


class _TrackingSemaphore:
    """Context-manager semaphore that exposes observed permit use."""

    def __init__(self, permits: int) -> None:
        self._semaphore = threading.BoundedSemaphore(permits)
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.acquire_count = 0

    def __enter__(self):
        self._semaphore.acquire()
        with self._lock:
            self.active += 1
            self.acquire_count += 1
            self.max_active = max(self.max_active, self.active)
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        with self._lock:
            self.active -= 1
        self._semaphore.release()
        return False


def test_shared_research_permit_caps_simultaneous_fanouts(monkeypatch):
    """Two module fan-outs share one account-wide research-call budget."""

    permits = _TrackingSemaphore(1)
    callers_ready = threading.Barrier(2)
    first_call_entered = threading.Event()
    release_first_call = threading.Event()
    fake_lock = threading.Lock()
    fake_calls = 0

    def fake_run_dimension(_client, **kwargs):
        nonlocal fake_calls
        with fake_lock:
            fake_calls += 1
            call_number = fake_calls
        if call_number == 1:
            first_call_entered.set()
            assert release_first_call.wait(timeout=2)
        return _outcome(kwargs["dimension"].dimension_id)

    monkeypatch.setattr(rr, "_run_dimension", fake_run_dimension)

    def run(module_id: str):
        callers_ready.wait(timeout=2)
        return run_requirements_research(
            _module(module_id, f"{module_id}_dimension"),
            _profile(),
            client=object(),
            call_semaphore=permits,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, module_id) for module_id in ("module_a", "module_b")]
        assert first_call_entered.wait(timeout=2)
        release_first_call.set()
        profiles = [future.result(timeout=2) for future in futures]

    assert permits.acquire_count == 2
    assert permits.max_active == 1
    assert permits.active == 0
    assert fake_calls == 2
    assert [profile.completed_dimensions for profile in profiles] == [1, 1]


def test_research_merge_is_dimension_ordered_after_reversed_completion(monkeypatch):
    """Worker completion order must not leak into the persisted profile."""

    permits = _TrackingSemaphore(2)
    alpha_started = threading.Event()
    beta_finished = threading.Event()
    completion_order: list[str] = []
    completion_lock = threading.Lock()

    def fake_run_dimension(_client, **kwargs):
        dimension_id = kwargs["dimension"].dimension_id
        if dimension_id == "alpha":
            alpha_started.set()
            assert beta_finished.wait(timeout=2)
        else:
            assert alpha_started.wait(timeout=2)
        with completion_lock:
            completion_order.append(dimension_id)
        if dimension_id == "beta":
            beta_finished.set()
        return _outcome(dimension_id)

    monkeypatch.setattr(rr, "_run_dimension", fake_run_dimension)

    profile = run_requirements_research(
        _module("ordered_module", "alpha", "beta"),
        _profile(),
        client=object(),
        call_semaphore=permits,
    )

    assert completion_order == ["beta", "alpha"]
    assert [status.dimension_id for status in profile.dimension_statuses] == [
        "alpha",
        "beta",
    ]
    assert [item.dimension_id for item in profile.items] == ["alpha", "beta"]
    assert permits.acquire_count == 2
    assert permits.max_active == 2
