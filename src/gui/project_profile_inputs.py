"""Pure (tkinter-free) helpers for the GUI's project-profile input row.

WS-2 §3 of ``docs/hyperscale_datacenter_module_plan.md``. Mirrors the
``gui/context_attachment.py`` precedent: the *logic* of building the
country/state dropdowns, mapping display strings back to canonical codes,
assembling a :class:`ProjectProfile` from raw widget values, and deciding
whether the profile is complete enough to run lives here — unit-testable
without a display — while ``gui.py`` stays a thin widget shell that calls
these functions.

The GUI shows friendly forms ("USA"/"Canada", "Virginia"/"Ontario") and this
module maps them to the canonical codes :class:`ProjectProfile` stores ("US",
"VA"). ``ProjectProfile.__post_init__`` also folds the country, so passing the
display form through is safe either way — the state mapping is the part that
must happen here (the profile stores a state *code*, not a name).
"""
from __future__ import annotations

from ..core.project_profile import (
    CA_PROVINCES,
    COUNTRY_DISPLAY,
    US_STATES,
    ProjectProfile,
    normalize_country,
)

# Country dropdown values, in display form.
COUNTRY_OPTIONS: list[str] = [COUNTRY_DISPLAY["US"], COUNTRY_DISPLAY["CA"]]

# Placeholder shown when no state/province is chosen yet.
STATE_PLACEHOLDER = "Select…"


def state_options_for_country(country_display: str) -> list[str]:
    """Ordered ``"CODE — Name"`` dropdown options for the given country.

    ``"VA — Virginia"`` keeps both the code (what we store) and the name (what
    a human recognizes) visible, and :func:`resolve_state_code` parses the code
    straight back out. Returns a leading placeholder plus the options; an
    unknown country yields just the placeholder.
    """
    table = _table_for(country_display)
    options = [f"{code} — {name}" for code, name in table.items()]
    return [STATE_PLACEHOLDER, *options]


def resolve_state_code(value: str) -> str:
    """Recover the canonical state/province code from a dropdown selection.

    Accepts the ``"VA — Virginia"`` option form (returns ``"VA"``), a bare code
    (returned upper-cased if it is a known code), or the placeholder / empty
    (returns ``""``).
    """
    text = (value or "").strip()
    if not text or text == STATE_PLACEHOLDER:
        return ""
    # "VA — Virginia" -> "VA"
    head = text.split("—", 1)[0].strip()
    code = head.upper()
    if code in US_STATES or code in CA_PROVINCES:
        return code
    return ""


def build_profile(
    *, city: str, state_value: str, country_display: str, client_name: str
) -> ProjectProfile:
    """Assemble a :class:`ProjectProfile` from raw GUI widget values.

    Maps the country display form to a code (via the profile's own
    normalization) and the state dropdown selection to a canonical code.
    """
    return ProjectProfile(
        city=city or "",
        state_or_province=resolve_state_code(state_value),
        country=normalize_country(country_display) or (country_display or ""),
        client_name=client_name or "",
    )


def completeness_error(profile: ProjectProfile) -> str:
    """Return a user-facing message if the profile is incomplete, else ``""``.

    Named the missing field(s) so the operator knows exactly what to fill in
    before a location-aware run is allowed to spend on review.
    """
    if profile.is_complete():
        return ""
    missing: list[str] = []
    if not profile.city:
        missing.append("city")
    if not profile.state_or_province:
        missing.append("state or province")
    if profile.country not in COUNTRY_DISPLAY:
        missing.append("country")
    if not profile.client_name:
        missing.append("client")
    fields = ", ".join(missing) if missing else "project details"
    return (
        f"This review module needs the project {fields}. "
        "Enter them before running."
    )


def _table_for(country_display: str) -> dict[str, str]:
    code = normalize_country(country_display)
    if code == "US":
        return US_STATES
    if code == "CA":
        return CA_PROVINCES
    return {}
