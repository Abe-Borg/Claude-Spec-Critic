"""Worker-budget parsing for routed-program concurrency."""

from __future__ import annotations

import pytest

from src.core import api_config


@pytest.mark.parametrize(
    ("env_name", "reader", "default", "ceiling"),
    [
        (
            api_config.ENV_RESEARCH_WORKERS,
            api_config.research_max_workers,
            api_config.RESEARCH_MAX_WORKERS_DEFAULT,
            12,
        ),
        (
            api_config.ENV_PROGRAM_PREPARE_WORKERS,
            api_config.program_prepare_max_workers,
            api_config.PROGRAM_PREPARE_MAX_WORKERS_DEFAULT,
            8,
        ),
        (
            api_config.ENV_PROGRAM_COLLECTION_WORKERS,
            api_config.program_collection_max_workers,
            api_config.PROGRAM_COLLECTION_MAX_WORKERS_DEFAULT,
            4,
        ),
        (
            api_config.ENV_REALTIME_COLLECTION_CALLS,
            api_config.realtime_collection_max_calls,
            api_config.REALTIME_COLLECTION_MAX_CALLS_DEFAULT,
            10,
        ),
    ],
)
def test_worker_budget_env_is_fresh_bounded_and_typo_safe(
    monkeypatch, env_name, reader, default, ceiling
):
    monkeypatch.delenv(env_name, raising=False)
    assert reader() == default

    monkeypatch.setenv(env_name, "not-an-integer")
    assert reader() == default

    monkeypatch.setenv(env_name, "0")
    assert reader() == 1

    monkeypatch.setenv(env_name, str(ceiling + 100))
    assert reader() == ceiling

    monkeypatch.setenv(env_name, "3")
    assert reader() == 3
