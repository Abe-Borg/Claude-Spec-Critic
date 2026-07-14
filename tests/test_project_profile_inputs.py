"""Unit pins for the tkinter-free project-profile GUI helpers.

WS-2d of ``docs/hyperscale_datacenter_module_plan.md``. These test the *logic*
the GUI's project-profile row delegates to (dropdown options, code resolution,
profile assembly, completeness messaging) without a display — mirroring how
``test_context_attachments.py`` tests ``gui/context_attachment.py``. Hermetic:
no tkinter, no network.
"""
from __future__ import annotations

import pytest

from src.core.project_profile import ProjectProfile
from src.gui.project_profile_inputs import (
    COUNTRY_OPTIONS,
    STATE_PLACEHOLDER,
    build_profile,
    completeness_error,
    resolve_state_code,
    state_options_for_country,
)


class TestCountryOptions:
    def test_values(self):
        assert COUNTRY_OPTIONS == ["USA", "Canada"]


class TestStateOptions:
    def test_us_options_lead_with_placeholder_and_use_code_name_form(self):
        options = state_options_for_country("USA")
        assert options[0] == STATE_PLACEHOLDER
        assert "VA — Virginia" in options
        assert "CA — California" in options
        assert "DC — District of Columbia" in options
        # 51 US entries + placeholder.
        assert len(options) == 52

    def test_canada_options(self):
        options = state_options_for_country("Canada")
        assert options[0] == STATE_PLACEHOLDER
        assert "ON — Ontario" in options
        assert len(options) == 14  # 13 provinces/territories + placeholder

    def test_unknown_country_only_placeholder(self):
        assert state_options_for_country("Mexico") == [STATE_PLACEHOLDER]


class TestResolveStateCode:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("VA — Virginia", "VA"),
            ("ON — Ontario", "ON"),
            ("VA", "VA"),
            ("va", "VA"),
            (STATE_PLACEHOLDER, ""),
            ("", ""),
            ("Not a code", ""),
        ],
    )
    def test_resolve(self, value, expected):
        assert resolve_state_code(value) == expected


class TestBuildProfile:
    def test_maps_display_forms_to_codes(self):
        p = build_profile(
            city="Ashburn", state_value="VA — Virginia",
            country_display="USA", client_name="ExampleCo",
        )
        assert isinstance(p, ProjectProfile)
        assert p.city == "Ashburn"
        assert p.state_or_province == "VA"
        assert p.country == "US"
        assert p.client_name == "ExampleCo"
        assert p.is_complete()

    def test_canadian(self):
        p = build_profile(
            city="Markham", state_value="ON — Ontario",
            country_display="Canada", client_name="ExampleCo",
        )
        assert p.country == "CA"
        assert p.state_or_province == "ON"
        assert p.is_complete()

    def test_placeholder_state_yields_incomplete(self):
        p = build_profile(
            city="Ashburn", state_value=STATE_PLACEHOLDER,
            country_display="USA", client_name="ExampleCo",
        )
        assert p.state_or_province == ""
        assert not p.is_complete()


class TestCompletenessError:
    def test_complete_returns_empty(self):
        p = build_profile(
            city="Ashburn", state_value="VA — Virginia",
            country_display="USA", client_name="ExampleCo",
        )
        assert completeness_error(p) == ""

    def test_names_the_missing_fields(self):
        p = build_profile(
            city="", state_value=STATE_PLACEHOLDER,
            country_display="USA", client_name="",
        )
        msg = completeness_error(p)
        assert "city" in msg
        assert "state or province" in msg
        assert "client" in msg
        assert msg.endswith("Enter them before running.")

    def test_country_missing_flagged(self):
        p = build_profile(
            city="Ashburn", state_value="VA — Virginia",
            country_display="Mexico", client_name="ExampleCo",
        )
        assert "country" in completeness_error(p)
