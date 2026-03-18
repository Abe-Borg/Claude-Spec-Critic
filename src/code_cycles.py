"""Code cycle definitions for California K-12 DSA projects."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeCycle:
    """A California code cycle with edition references used in prompts."""

    label: str
    cbc: str
    cmc: str
    cpc: str
    energy_code: str
    calgreen: str
    asce7: str
    asce7_previous: str
    cbc_previous: str


CALIFORNIA_2022 = CodeCycle(
    label="2022",
    cbc="2022",
    cmc="2022",
    cpc="2022",
    energy_code="2022",
    calgreen="2022",
    asce7="7-22",
    asce7_previous="7-16",
    cbc_previous="2019",
)

CALIFORNIA_2025 = CodeCycle(
    label="2025",
    cbc="2025",
    cmc="2025",
    cpc="2025",
    energy_code="2025",
    calgreen="2025",
    asce7="7-22",
    asce7_previous="7-16",
    cbc_previous="2022",
)

AVAILABLE_CYCLES = {
    "2022": CALIFORNIA_2022,
    "2025": CALIFORNIA_2025,
}

DEFAULT_CYCLE = CALIFORNIA_2025