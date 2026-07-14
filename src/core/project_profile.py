"""Per-run project identity for location- and client-aware review.

A :class:`ProjectProfile` is **per-run input**, not module data: the review
module (a frozen, registry-validated domain object) is stable, but the project
city / state-or-province / country / client vary every run. This module is the
single dependency-free home for that value (WS-2 of
``docs/hyperscale_datacenter_module_plan.md``, design decision D-1).

Nothing here touches the pipeline on its own — later workstreams thread the
profile through research (WS-3), the compliance pass and location-aware
verification (WS-4), and the GUI (WS-2 §3). The profile is only ever collected
and acted on when the selected module opts in via
``ReviewModule.project_profile_enabled``; with the flag off the profile is
``None`` and every downstream surface is byte-identical to today.

**Normalization is load-bearing, not cosmetic (D-1 [FT]).** The
:meth:`jurisdiction_fingerprint` keys the verification cache and
:meth:`web_search_user_location` steers every search, so a typo'd or
inconsistently-cased city silently misroutes both. Construction trims every
field and folds the country to a canonical ``"US"`` / ``"CA"`` code; the
state/province is a canonical code chosen from a closed table; the city stays
free text but is trimmed (and casefolded only when computing the fingerprint).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Canonical state / province tables (code -> display name)
# ---------------------------------------------------------------------------
# The GUI dropdown stores these codes; :meth:`ProjectProfile.state_display` and
# the report render the names. One source of truth for both surfaces.

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

CA_PROVINCES: dict[str, str] = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}

# Country storage code -> display form (GUI shows the display form).
COUNTRY_DISPLAY: dict[str, str] = {"US": "USA", "CA": "Canada"}

# Everything we will accept and fold to a canonical country code. Keys are
# casefolded on lookup, so "usa" / "United States" / "canada" all resolve.
_COUNTRY_ALIASES: dict[str, str] = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "united states": "US", "united states of america": "US", "america": "US",
    "ca": "CA", "can": "CA", "canada": "CA",
}


def normalize_country(value: str) -> str:
    """Fold a free-form country string to a canonical ``"US"`` / ``"CA"`` code.

    Returns ``""`` for anything unrecognized so :meth:`ProjectProfile.is_complete`
    can reject it rather than silently mis-routing searches.
    """
    key = (value or "").strip().casefold()
    return _COUNTRY_ALIASES.get(key, "")


def states_for_country(country: str) -> dict[str, str]:
    """Return the ``code -> name`` table for the given country code (``{}`` if unknown)."""
    code = normalize_country(country)
    if code == "US":
        return US_STATES
    if code == "CA":
        return CA_PROVINCES
    return {}


@dataclass(frozen=True)
class ProjectProfile:
    """One run's project identity (city / state-or-province / country / client).

    Frozen and JSON-friendly (``to_dict`` / ``from_dict``). Every field is
    trimmed at construction and ``country`` is folded to a canonical
    ``"US"`` / ``"CA"`` code; ``state_or_province`` is expected to be a
    canonical code from :data:`US_STATES` / :data:`CA_PROVINCES` (free-form
    input is preserved as-is so nothing is silently dropped, but
    :meth:`is_complete` and the fingerprint treat whatever is stored verbatim).
    """

    city: str
    state_or_province: str
    country: str
    client_name: str

    def __post_init__(self) -> None:
        # Normalize on every construction path (direct, from_dict, replace) so
        # the fingerprint and user_location can never see stray whitespace or a
        # display-form country. Frozen dataclass -> object.__setattr__.
        object.__setattr__(self, "city", (self.city or "").strip())
        object.__setattr__(
            self, "state_or_province", (self.state_or_province or "").strip()
        )
        object.__setattr__(self, "client_name", (self.client_name or "").strip())
        # Fold the country to a canonical code; keep "" if unrecognized so
        # is_complete() rejects it instead of guessing.
        normalized = normalize_country(self.country)
        object.__setattr__(
            self, "country", normalized or (self.country or "").strip()
        )

    # -- Derived display forms ------------------------------------------------

    @property
    def country_display(self) -> str:
        """The GUI/report display form of the country (``"USA"`` / ``"Canada"``)."""
        return COUNTRY_DISPLAY.get(self.country, self.country)

    @property
    def state_display(self) -> str:
        """The full state/province name for the stored code (falls back to the code)."""
        return states_for_country(self.country).get(
            self.state_or_province, self.state_or_province
        )

    def display_line(self) -> str:
        """One-line human summary, e.g. ``"Ashburn, Virginia, USA — Client: ExampleCo"``."""
        return (
            f"{self.city}, {self.state_display}, {self.country_display} "
            f"— Client: {self.client_name}"
        )

    def project_meta_lines(self) -> list[str]:
        """The two centered report title lines (D-13), display forms."""
        return [
            f"Project: {self.city}, {self.state_display}, {self.country_display}",
            f"Client: {self.client_name}",
        ]

    # -- Routing inputs -------------------------------------------------------

    def prompt_format_kwargs(self) -> dict[str, str]:
        """The per-run values for research prompt-template placeholders (D-6/§6.1).

        Display forms throughout — a research prompt says "Markham, Ontario,
        Canada", not "Markham, ON, CA" — matching the dummy values module
        registration format-checks templates against
        (``modules.base._DUMMY_PROFILE_FORMAT_KWARGS``).
        """
        return {
            "city": self.city,
            "state_or_province": self.state_display,
            "country": self.country_display,
            "client_name": self.client_name,
        }

    def web_search_user_location(self) -> dict[str, str]:
        """The ``user_location`` dict for the web_search tool.

        Uses the display country code (``"US"`` / ``"CA"``) and the *full*
        region name (matching the engine's existing hardcoded
        ``{"country": "US", "region": "California"}`` default shape).
        """
        return {
            "type": "approximate",
            "country": self.country,
            "region": self.state_display,
            "city": self.city,
        }

    def jurisdiction_fingerprint(self) -> str:
        """Stable 16-hex fingerprint of ``country|state|city`` (casefolded).

        Keys the verification cache's jurisdiction segment (WS-4) so a verdict
        grounded against one city's codes can never replay for another city.
        Casefolds at compute time so ``"Markham"`` and ``"markham"`` fingerprint
        identically while the stored city keeps its display case.
        """
        raw = "|".join(
            part.casefold()
            for part in (self.country, self.state_or_province, self.city)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    # -- Validation -----------------------------------------------------------

    def is_complete(self) -> bool:
        """True when every field is present and the country is a known code."""
        return bool(
            self.city
            and self.state_or_province
            and self.client_name
            and self.country in COUNTRY_DISPLAY
        )

    # -- Serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, str]:
        """JSON-friendly dict (persisted on submission / pending state)."""
        return {
            "city": self.city,
            "state_or_province": self.state_or_province,
            "country": self.country,
            "client_name": self.client_name,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ProjectProfile | None":
        """Defensive inverse of :meth:`to_dict`; ``None`` for missing/garbage.

        Returns ``None`` for a non-dict or an all-empty payload so a legacy
        persisted state with no profile key degrades to "profile-less" rather
        than a hollow profile.
        """
        if not isinstance(data, dict):
            return None
        profile = cls(
            city=str(data.get("city", "") or ""),
            state_or_province=str(data.get("state_or_province", "") or ""),
            country=str(data.get("country", "") or ""),
            client_name=str(data.get("client_name", "") or ""),
        )
        if not (
            profile.city
            or profile.state_or_province
            or profile.country
            or profile.client_name
        ):
            return None
        return profile
