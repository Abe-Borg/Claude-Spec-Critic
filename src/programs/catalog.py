"""Built-in user-facing review programs.

Program membership is intentionally independent of the module registry.  A
program can therefore describe its planned end state while individual review
modules are implemented and registered incrementally.
"""
from __future__ import annotations

from .models import ProgramDefinition


DATACENTER_FIRE_MODULE_ID = "datacenter_fire"
DATACENTER_ARCHITECTURE_MODULE_ID = "datacenter_architecture"
DATACENTER_ELECTRICAL_MODULE_ID = "datacenter_electrical"
CALIFORNIA_K12_MODULE_ID = "california_k12_mep"


CALIFORNIA_K12_PROGRAM = ProgramDefinition(
    program_id="california_k12",
    display_name="California K-12 (DSA) — Mechanical & Plumbing",
    description=(
        "California K-12 mechanical and plumbing specification review under "
        "DSA jurisdiction."
    ),
    module_ids=(CALIFORNIA_K12_MODULE_ID,),
)


HYPERSCALE_DATACENTER_PROGRAM = ProgramDefinition(
    program_id="hyperscale_datacenter",
    display_name="Hyperscale Data Centers — USA and Canada",
    description=(
        "A user-facing hyperscale data-center review program that routes each "
        "specification to the applicable discipline modules."
    ),
    module_ids=(
        DATACENTER_FIRE_MODULE_ID,
        DATACENTER_ARCHITECTURE_MODULE_ID,
        DATACENTER_ELECTRICAL_MODULE_ID,
    ),
    planned_module_ids=(),
)


_ALL_PROGRAMS: tuple[ProgramDefinition, ...] = (
    CALIFORNIA_K12_PROGRAM,
    HYPERSCALE_DATACENTER_PROGRAM,
)

AVAILABLE_PROGRAMS: dict[str, ProgramDefinition] = {
    program.program_id: program for program in _ALL_PROGRAMS
}
DEFAULT_PROGRAM = CALIFORNIA_K12_PROGRAM

_PROGRAM_BY_MODULE_ID: dict[str, ProgramDefinition] = {}
for _program in _ALL_PROGRAMS:
    for _module_id in _program.module_ids:
        # The data-center discipline modules intentionally map to the same
        # user-facing program. Program ids themselves are also accepted by
        # ``get_program`` below.
        _PROGRAM_BY_MODULE_ID[_module_id] = _program


def get_program(program_or_legacy_module_id: str | None) -> ProgramDefinition:
    """Resolve a saved program id, accepting historical module selections.

    Legacy UI state stored ``datacenter_fire`` directly. Once the selector is
    program-based, that value migrates naturally to the hyperscale program.
    Unknown values retain the application's historical safe default.
    """
    value = (program_or_legacy_module_id or "").strip()
    if not value:
        return DEFAULT_PROGRAM
    return AVAILABLE_PROGRAMS.get(
        value, _PROGRAM_BY_MODULE_ID.get(value, DEFAULT_PROGRAM)
    )


def require_program(program_id: str) -> ProgramDefinition:
    """Resolve an explicit program id without a silent fallback."""
    normalized = (program_id or "").strip()
    try:
        return AVAILABLE_PROGRAMS[normalized]
    except KeyError as exc:
        raise KeyError(f"Unknown review program: {program_id!r}") from exc


def resolve_saved_program(
    program_id: str | None, legacy_module_id: str | None
) -> ProgramDefinition:
    """Resolve persisted state without letting a stale new key mask legacy data."""
    program_value = (program_id or "").strip()
    if program_value in AVAILABLE_PROGRAMS:
        return AVAILABLE_PROGRAMS[program_value]
    legacy_value = (legacy_module_id or "").strip()
    if legacy_value in _PROGRAM_BY_MODULE_ID:
        return _PROGRAM_BY_MODULE_ID[legacy_value]
    return DEFAULT_PROGRAM
