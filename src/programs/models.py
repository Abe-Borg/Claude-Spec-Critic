"""Domain models for user-facing review programs and per-spec routing.

A program groups several independently versioned review modules behind one
user-facing choice.  Routing remains a separate concern: it decides which of
the program's modules, if any, are relevant to each specification.

The models deliberately retain the automatic assessment after a user
override.  That makes an override auditable and lets a future UI remove it
without re-running the deterministic classifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def _clean_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _module_id_tuple(
    values: tuple[str, ...],
    *,
    field_name: str,
    allow_empty: bool,
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be a sequence of module ids")
    normalized = tuple(
        _clean_identifier(value, field_name=f"{field_name} entry") for value in values
    )
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must contain at least one module id")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate module ids")
    return normalized


class RoutingState(str, Enum):
    """Whether an automatic or effective routing decision can be executed."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


class RoutingEvidenceSource(str, Enum):
    """Document surface from which a deterministic routing signal came."""

    CSI_SECTION = "csi_section"
    SECTION_TITLE = "section_title"
    CONTENT = "content"


@dataclass(frozen=True)
class ProgramDefinition:
    """One user-facing program composed of one or more review modules.

    ``planned_module_ids`` allows program and routing work to land before a
    module is registered with the review engine.  It must be a subset of
    ``module_ids``; the remaining ids are implemented members of the program.
    """

    program_id: str
    display_name: str
    description: str
    module_ids: tuple[str, ...]
    planned_module_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "program_id",
            _clean_identifier(self.program_id, field_name="program_id"),
        )
        object.__setattr__(
            self,
            "display_name",
            _clean_identifier(self.display_name, field_name="display_name"),
        )
        object.__setattr__(
            self,
            "description",
            _clean_identifier(self.description, field_name="description"),
        )
        module_ids = _module_id_tuple(
            self.module_ids, field_name="module_ids", allow_empty=False
        )
        planned = _module_id_tuple(
            self.planned_module_ids,
            field_name="planned_module_ids",
            allow_empty=True,
        )
        unknown_planned = set(planned) - set(module_ids)
        if unknown_planned:
            unknown = ", ".join(sorted(unknown_planned))
            raise ValueError(
                "planned_module_ids must be members of module_ids; "
                f"unknown: {unknown}"
            )
        object.__setattr__(self, "module_ids", module_ids)
        object.__setattr__(self, "planned_module_ids", planned)

    @property
    def implemented_module_ids(self) -> tuple[str, ...]:
        """Program members that are not marked as future/planned."""

        planned = set(self.planned_module_ids)
        return tuple(module_id for module_id in self.module_ids if module_id not in planned)


@dataclass(frozen=True)
class SpecRoutingInput:
    """The stable, text-only inputs used to classify one specification."""

    spec_id: str
    section_number: str = ""
    section_title: str = ""
    content: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "spec_id", _clean_identifier(self.spec_id, field_name="spec_id")
        )
        for field_name in ("section_number", "section_title", "content"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string")


@dataclass(frozen=True)
class RoutingEvidence:
    """One reproducible signal contributing to an automatic assessment."""

    source: RoutingEvidenceSource
    signal: str
    detail: str
    module_id: str | None
    weight: float

    def __post_init__(self) -> None:
        if not isinstance(self.source, RoutingEvidenceSource):
            object.__setattr__(self, "source", RoutingEvidenceSource(self.source))
        object.__setattr__(
            self, "signal", _clean_identifier(self.signal, field_name="signal")
        )
        object.__setattr__(
            self, "detail", _clean_identifier(self.detail, field_name="detail")
        )
        if self.module_id is not None:
            object.__setattr__(
                self,
                "module_id",
                _clean_identifier(self.module_id, field_name="module_id"),
            )
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("weight must be between 0.0 and 1.0")


@dataclass(frozen=True)
class UserRoutingOverride:
    """An explicit user's replacement for an automatic module selection.

    An empty ``module_ids`` tuple is meaningful: the user explicitly decided
    that this specification should not be sent to any module.
    """

    module_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "module_ids",
            _module_id_tuple(
                self.module_ids, field_name="override module_ids", allow_empty=True
            ),
        )
        object.__setattr__(
            self, "reason", _clean_identifier(self.reason, field_name="override reason")
        )


@dataclass(frozen=True)
class SpecRoutingDecision:
    """Automatic routing assessment plus an optional effective override.

    ``automatic_module_ids`` are executable selections when
    ``automatic_state`` is ``SUPPORTED`` and merely candidates when it is
    ``AMBIGUOUS``.  The ``module_ids`` and ``state`` properties expose the
    effective routing outcome, including any user override.

    ``confidence`` always describes the deterministic assessment.  A user
    selection is authoritative but does not retroactively make the
    classifier more certain.
    """

    spec_id: str
    program_id: str
    automatic_state: RoutingState
    automatic_module_ids: tuple[str, ...]
    confidence: float
    evidence: tuple[RoutingEvidence, ...]
    user_override: UserRoutingOverride | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "spec_id", _clean_identifier(self.spec_id, field_name="spec_id")
        )
        object.__setattr__(
            self,
            "program_id",
            _clean_identifier(self.program_id, field_name="program_id"),
        )
        if not isinstance(self.automatic_state, RoutingState):
            object.__setattr__(
                self, "automatic_state", RoutingState(self.automatic_state)
            )
        automatic_ids = _module_id_tuple(
            self.automatic_module_ids,
            field_name="automatic_module_ids",
            allow_empty=True,
        )
        object.__setattr__(self, "automatic_module_ids", automatic_ids)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.automatic_state is RoutingState.UNSUPPORTED and automatic_ids:
            raise ValueError("unsupported decisions cannot select candidate modules")
        if self.automatic_state is not RoutingState.UNSUPPORTED and not automatic_ids:
            raise ValueError("supported/ambiguous decisions require module candidates")

    @property
    def state(self) -> RoutingState:
        """Effective state after applying an optional user override."""

        if self.user_override is None:
            return self.automatic_state
        if self.user_override.module_ids:
            return RoutingState.SUPPORTED
        return RoutingState.UNSUPPORTED

    @property
    def module_ids(self) -> tuple[str, ...]:
        """The 0..N module ids that are safe to execute for this decision."""

        if self.user_override is not None:
            return self.user_override.module_ids
        if self.automatic_state is RoutingState.SUPPORTED:
            return self.automatic_module_ids
        return ()

    @property
    def candidate_module_ids(self) -> tuple[str, ...]:
        """Candidates needing user resolution for an ambiguous assessment."""

        if self.automatic_state is RoutingState.AMBIGUOUS:
            return self.automatic_module_ids
        return ()

    @property
    def is_user_overridden(self) -> bool:
        return self.user_override is not None
