"""User-facing review programs and deterministic per-spec routing."""

from .catalog import (
    AVAILABLE_PROGRAMS,
    CALIFORNIA_K12_MODULE_ID,
    CALIFORNIA_K12_PROGRAM,
    DATACENTER_ARCHITECTURE_MODULE_ID,
    DATACENTER_ELECTRICAL_MODULE_ID,
    DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    DATACENTER_FIRE_MODULE_ID,
    DEFAULT_PROGRAM,
    HYPERSCALE_DATACENTER_PROGRAM,
    get_program,
    require_program,
    resolve_saved_program,
)
from .models import (
    ProgramDefinition,
    RoutingEvidence,
    RoutingEvidenceSource,
    RoutingState,
    SpecRoutingDecision,
    SpecRoutingInput,
    UserRoutingOverride,
)
from .assignments import (
    SpecAssignment,
    assignments_for_specs,
    partition_assignments,
    routed_module_ids,
)
from .routing import (
    apply_user_override,
    remove_user_override,
    route_spec,
    route_specs,
)

__all__ = [
    "AVAILABLE_PROGRAMS",
    "CALIFORNIA_K12_MODULE_ID",
    "CALIFORNIA_K12_PROGRAM",
    "DATACENTER_ARCHITECTURE_MODULE_ID",
    "DATACENTER_ELECTRICAL_MODULE_ID",
    "DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID",
    "DATACENTER_FIRE_MODULE_ID",
    "DEFAULT_PROGRAM",
    "HYPERSCALE_DATACENTER_PROGRAM",
    "get_program",
    "require_program",
    "resolve_saved_program",
    "ProgramDefinition",
    "RoutingEvidence",
    "RoutingEvidenceSource",
    "RoutingState",
    "SpecRoutingDecision",
    "SpecRoutingInput",
    "SpecAssignment",
    "UserRoutingOverride",
    "assignments_for_specs",
    "partition_assignments",
    "routed_module_ids",
    "apply_user_override",
    "remove_user_override",
    "route_spec",
    "route_specs",
]
