"""Persisted font scale + cross-check checkbox (B-26b) and the write lock (B-26c).

``src/core/ui_state.py`` is dependency-free; ``SPEC_CRITIC_UI_STATE_PATH`` is
not used here — every helper takes an explicit ``path=`` into ``tmp_path``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.core import ui_state


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "ui_state.json"


class TestFontScale:
    def test_default_when_unset(self, state_path):
        assert ui_state.load_font_scale(path=state_path) == 1.0

    @pytest.mark.parametrize("scale", ui_state.FONT_SCALE_CHOICES)
    def test_round_trip(self, state_path, scale):
        ui_state.save_font_scale(scale, path=state_path)
        assert ui_state.load_font_scale(path=state_path) == scale

    def test_round_trip_survives_float_noise(self, state_path):
        ui_state.save_font_scale(1.1000000001, path=state_path)
        assert ui_state.load_font_scale(path=state_path) == 1.1

    @pytest.mark.parametrize("bad", [0.5, 3.0, "1.1", True, None, [1.1]])
    def test_unknown_values_are_not_written(self, state_path, bad):
        ui_state.save_font_scale(bad, path=state_path)  # type: ignore[arg-type]
        assert not state_path.exists()

    @pytest.mark.parametrize("stored", [0.5, "large", True, None, 7])
    def test_hand_edited_values_degrade_to_default(self, state_path, stored):
        state_path.write_text(json.dumps({"font_scale": stored}), encoding="utf-8")
        assert ui_state.load_font_scale(path=state_path) == 1.0

    def test_corrupt_file_reads_as_default(self, state_path):
        state_path.write_text("{not json", encoding="utf-8")
        assert ui_state.load_font_scale(path=state_path) == 1.0

    def test_save_preserves_other_keys(self, state_path):
        ui_state.save_selected_program_id("prog", path=state_path)
        ui_state.save_font_scale(1.2, path=state_path)
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["program_id"] == "prog"
        assert data["font_scale"] == 1.2


class TestCrossCheckToggle:
    def test_default_off(self, state_path):
        assert ui_state.load_cross_check_enabled(path=state_path) is False

    @pytest.mark.parametrize("value", [True, False])
    def test_round_trip(self, state_path, value):
        ui_state.save_cross_check_enabled(value, path=state_path)
        assert ui_state.load_cross_check_enabled(path=state_path) is value

    def test_truthy_non_bool_is_coerced_on_save(self, state_path):
        ui_state.save_cross_check_enabled(1, path=state_path)  # type: ignore[arg-type]
        assert json.loads(state_path.read_text(encoding="utf-8"))["cross_check_enabled"] is True

    @pytest.mark.parametrize("stored", ["yes", 1, None, [True]])
    def test_hand_edited_non_bool_reads_as_off(self, state_path, stored):
        state_path.write_text(json.dumps({"cross_check_enabled": stored}), encoding="utf-8")
        assert ui_state.load_cross_check_enabled(path=state_path) is False

    def test_save_preserves_other_keys(self, state_path):
        ui_state.save_review_transport("realtime", path=state_path)
        ui_state.save_cross_check_enabled(True, path=state_path)
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["review_transport"] == "realtime"
        assert data["cross_check_enabled"] is True


class TestWriteLock:
    def test_write_key_holds_the_state_lock_around_read_modify_write(self, state_path, monkeypatch):
        seen: list[bool] = []
        real_load = ui_state._load

        def _load_spy(path=None):
            seen.append(ui_state._STATE_LOCK.locked())
            return real_load(path)

        def _write_spy(state, *, path=None):
            seen.append(ui_state._STATE_LOCK.locked())

        monkeypatch.setattr(ui_state, "_load", _load_spy)
        monkeypatch.setattr(ui_state, "_write_state", _write_spy)
        ui_state._write_key("k", 1, path=state_path)
        assert seen == [True, True]  # load AND write happened under the lock

    def test_save_project_profile_holds_the_lock(self, state_path, monkeypatch):
        seen: list[bool] = []
        monkeypatch.setattr(
            ui_state, "_write_state", lambda state, *, path=None: seen.append(ui_state._STATE_LOCK.locked())
        )
        ui_state.save_project_profile("m", {"city": "x"}, path=state_path)
        assert seen == [True]

    def test_lock_is_released_after_a_write(self, state_path):
        ui_state._write_key("k", 1, path=state_path)
        assert not ui_state._STATE_LOCK.locked()

    def test_two_threads_never_lose_each_others_keys(self, state_path):
        rounds = 150
        start = threading.Barrier(2)

        def _writer(key: str):
            start.wait()
            for i in range(rounds):
                ui_state._write_key(key, i, path=state_path)

        threads = [
            threading.Thread(target=_writer, args=("alpha",)),
            threading.Thread(target=_writer, args=("beta",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        # Without the lock a read-modify-write interleaving drops one key.
        assert data == {"alpha": rounds - 1, "beta": rounds - 1}

    def test_concurrent_mixed_savers_all_land(self, state_path):
        start = threading.Barrier(3)

        def _run(fn):
            start.wait()
            fn()

        threads = [
            threading.Thread(target=_run, args=(lambda: ui_state.save_font_scale(1.2, path=state_path),)),
            threading.Thread(target=_run, args=(lambda: ui_state.save_cross_check_enabled(True, path=state_path),)),
            threading.Thread(target=_run, args=(lambda: ui_state.save_project_profile("m", {"city": "c"}, path=state_path),)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert ui_state.load_font_scale(path=state_path) == 1.2
        assert ui_state.load_cross_check_enabled(path=state_path) is True
        assert ui_state.load_project_profile("m", path=state_path) == {"city": "c"}
