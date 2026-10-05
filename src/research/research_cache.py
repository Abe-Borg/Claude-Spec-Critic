"""Experiment EX-05: reuse a completed requirements-research profile across runs.

Off by default (``SPEC_CRITIC_RESEARCH_CACHE``). The decision record is
``plans/experiments/EX-05-research-reuse.md``; plan Part 4 §26 is the contract.

Research (``requirements_research``) is the one pre-review phase that spends
real money before anything is submitted: one web-search conversation per
module dimension, a dozen or more searches each. A project reviewed again —
the same location, client, module, and specifications — asks the same
questions. This module stores a **completed** profile and hands it back when a
later run would send exactly the same research requests, is still young
enough, and names no date that has passed since it was researched.

What makes a reuse safe, in the order a lookup checks it:

- **The key is the requests.** :class:`ResearchKey` is derived (by
  ``requirements_research.research_reuse_key``) from the requests each
  dimension would send, built by the same builder the fan-out uses: model,
  output cap, thinking and effort, the system prompt (module persona and the
  engine protocol), the tools (the research schema, the web-search and
  web-fetch configuration with its blocklist and budgets, the project
  location it steers searches by), and every user message (the project and
  client exactly as entered, each dimension's brief with the code basis it is
  formatted with, and the corpus signals scraped from this run's
  specifications). Beside them: the module, the date basis, this policy's
  version, the app version, and the continuation cap. Nothing is normalized
  into an assumed equivalent — "Markham" and "markham" are two keys — except
  what :class:`~src.core.project_profile.ProjectProfile` already canonicalizes
  (a country alias, a trimmed field). Settings that change only what is
  *visible* (a deep trace's ``thinking.display``) or where the cache
  breakpoints sit (``cache_control``) are left out, because neither changes
  what research concludes.
- **Only a completed profile is stored.** Every dimension must have
  completed; a partial profile (some dimension failed) and a failed fan-out
  are never stored, and a stored entry claiming otherwise is invalid.
- **Freshness is checked at lookup, not keyed.** The date basis is
  ``current_at_research``: the research prompt asks for *current*
  requirements, so an entry is as-of the day it was researched. A lookup
  reuses it only when its age is at most ``max_age_days`` (default 30,
  ``SPEC_CRITIC_RESEARCH_CACHE_MAX_AGE_DAYS``, 1–90) **and** no date the
  profile names — a full date, a month, or a later year — falls after the day
  it was researched and on or before today (:func:`passed_named_dates`). A
  profile that says "the 2024 code takes effect January 18" is stale on
  January 18, whatever its age. Calendar months are never a unit of
  equivalence.
- **Every row is validated on load.** A row whose key does not match its own
  components, whose profile digest does not match the stored profile, whose
  timestamps are invalid or in the future, whose profile is not completed, or
  that exceeds the size bound is ignored and counted (``rejected``); valid
  rows beside it still load, and the next save writes only what loaded.
- **A reuse is never silent.** The reused profile carries a ``reuse``
  provenance record (:func:`reuse_provenance`) that rides every copy of the
  profile — the pending-batch record, the report, ``.profile.json`` — and
  both reports say when the research was done and that it was not re-run
  (:func:`reuse_notice`). The run log names the profile's age and what it
  governs.

Stored: the profile the run produced (its items, dimension statuses, research
date, and project identity), digests of the key's components, timestamps, and
the research usage a reuse saves. Never stored: the prompts themselves, the
corpus signals (only their digest — they are excerpts of the specifications),
or any specification text.

Stdlib only: the diagnostics rollup and both report exporters read this
module, and none of them may pull in the research runner's streaming stack.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: Bump when anything that decides what a stored profile means changes — the
#: parse, grounding, or merge rules of the research runner, the date rules
#: below, or the key's components. The version is a key component, so a bump
#: retires every stored entry at once (they stop matching; LRU drops them).
POLICY_VERSION = "rr1"

#: The on-disk file's schema. A file with another version is never read and
#: never overwritten (it may be another build's).
FILE_SCHEMA_VERSION = 1

#: The date basis research runs under: the research prompt asks for *current*
#: requirements, so a profile is as-of the day it was researched. A future
#: project-date input (for example, a permit application date) would be a
#: different basis and therefore a different key.
DATE_BASIS_CURRENT = "current_at_research"

DEFAULT_MAX_AGE_DAYS = 30
MIN_MAX_AGE_DAYS = 1
MAX_MAX_AGE_DAYS = 90

#: At most this many stored profiles; the least recently used goes first.
MAX_ENTRIES = 50

#: A stored profile larger than this (serialized) is not stored. Real profiles
#: are tens of kilobytes; the bound keeps one pathological run from growing
#: the file without limit.
MAX_PROFILE_BYTES = 1_000_000

#: How far ahead of the clock a stored timestamp may be (clock adjustments).
_MAX_FUTURE_SKEW_SECONDS = 3600.0

#: The components every key carries, in the order a "changed" explanation
#: names them: the inputs a person would recognize first, then the request
#: parts they feed.
KEY_COMPONENT_ORDER: tuple[str, ...] = (
    "project",
    "module_id",
    "corpus_signals",
    "model",
    "cycle_label",
    "dimension_ids",
    "system_prompt",
    "user_messages",
    "tools",
    "request_settings",
    "date_basis",
    "policy_version",
    "app_version",
    "max_continuations",
)

ENV_RESEARCH_CACHE_PATH = "SPEC_CRITIC_RESEARCH_CACHE_PATH"

# Lookup outcomes.
OUTCOME_HIT = "hit"
OUTCOME_ABSENT = "absent"
OUTCOME_CHANGED = "changed"
OUTCOME_STALE = "stale"
OUTCOME_UNREADABLE = "unreadable"
#: The key could not be built; research ran without the cache.
OUTCOME_KEY_ERROR = "key_error"
#: Not a lookup: ``refresh`` mode skips it on purpose.
OUTCOME_REFRESH = "refresh"
LOOKUP_OUTCOMES = (
    OUTCOME_HIT,
    OUTCOME_ABSENT,
    OUTCOME_CHANGED,
    OUTCOME_STALE,
    OUTCOME_UNREADABLE,
    OUTCOME_KEY_ERROR,
    OUTCOME_REFRESH,
)

# Store outcomes.
STORE_STORED = "stored"
STORE_PARTIAL = "partial"
STORE_TOO_LARGE = "too_large"
STORE_UNREADABLE_FILE = "unreadable_file"
STORE_WRITE_FAILED = "write_failed"
#: The row would not pass the load-time validation (for example, a research
#: date that disagrees with the clock), so it is not written.
STORE_INVALID = "invalid"
STORE_NOT_ATTEMPTED = "not_attempted"

_USAGE_INT_KEYS = (
    "dimension_calls",
    "api_requests",
    "web_search_requests",
    "web_fetch_requests",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# One lock for every read-modify-write of every research cache file in this
# process: a routed program prepares its modules concurrently, and each
# module's research phase may look up and store at once.
_LOCK = threading.RLock()


def default_research_cache_path() -> Path:
    """``~/.spec_critic/research_cache.json``, or ``SPEC_CRITIC_RESEARCH_CACHE_PATH``.

    ``~`` and environment variables in the override are expanded, as for the
    verification cache's path.
    """
    override = os.environ.get(ENV_RESEARCH_CACHE_PATH)
    if override and override.strip():
        return Path(os.path.expandvars(os.path.expanduser(override.strip())))
    return Path.home() / ".spec_critic" / "research_cache.json"


# ---------------------------------------------------------------------------
# Canonical digests
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """The one serialization every digest here is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    """``sha256`` of :func:`canonical_json` (a string is hashed as JSON too)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_key(components: Mapping[str, str]) -> str:
    """The cache key: a digest of every component, so no component can be forged."""
    return digest({name: components[name] for name in KEY_COMPONENT_ORDER})


@dataclass(frozen=True)
class ResearchKey:
    """What one run's research would send, reduced to comparable digests.

    ``components`` maps each :data:`KEY_COMPONENT_ORDER` name to a string: a
    plain value where one is short and readable (the model, the module id),
    else a digest. ``subject`` is the project and module the key is *about*;
    a miss that finds an entry with the same subject names the components
    that differ (``changed``) instead of just ``absent``.
    """

    components: Mapping[str, str]
    module_id: str
    project: Mapping[str, str]
    dimension_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        missing = [name for name in KEY_COMPONENT_ORDER if name not in self.components]
        if missing:
            raise ValueError(f"research key is missing components: {missing}")
        object.__setattr__(
            self, "components", {name: str(self.components[name]) for name in KEY_COMPONENT_ORDER}
        )
        object.__setattr__(self, "project", dict(self.project))

    @property
    def key(self) -> str:
        return derive_key(self.components)

    def subject(self) -> dict:
        return {"module_id": self.module_id, "project": dict(self.project)}


def differing_components(a: Mapping[str, str], b: Mapping[str, str]) -> list[str]:
    """The component names whose values differ, in :data:`KEY_COMPONENT_ORDER`."""
    return [name for name in KEY_COMPONENT_ORDER if a.get(name) != b.get(name)]


# ---------------------------------------------------------------------------
# Date rules
# ---------------------------------------------------------------------------

_MONTHS = {
    name.lower(): index
    for index, name in enumerate(calendar.month_name)
    if name
}
_MONTHS.update(
    {name.lower(): index for index, name in enumerate(calendar.month_abbr) if name}
)
_MONTHS["sept"] = 9
_MONTH_PATTERN = (
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
)
_ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b" + _MONTH_PATTERN + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_DAY_MONTH_YEAR_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_PATTERN + r",?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    r"\b" + _MONTH_PATTERN + r",?\s+((?:19|20)\d{2})\b", re.IGNORECASE
)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

#: The research item fields a date is read from.
_DATED_ITEM_FIELDS = ("requirement", "notes", "topic", "code_reference", "authority")


@dataclass(frozen=True)
class NamedPeriod:
    """A date, month, or year a profile names, and the text that named it."""

    start: date
    text: str
    item_id: str


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def named_periods(text: str, *, item_id: str = "") -> list[NamedPeriod]:
    """Every date, month, and year ``text`` names, as the first day it covers.

    Full dates: ISO (``2026-01-18``), ``January 18, 2026`` / ``Jan. 18 2026``,
    ``18 January 2026``, and numeric ``1/18/2026``. A numeric date is read
    **both** ways (month/day and day/month) — the profile may be Canadian — so
    either reading can make it stale (the conservative direction). A month
    with a year (``January 2027``) starts on its first day, a bare year
    (``2027``) on January 1. A year that is part of a date is not also read as
    a bare year.
    """
    found: list[NamedPeriod] = []
    covered: list[tuple[int, int]] = []

    def add(start: date | None, match: re.Match) -> None:
        covered.append(match.span())
        if start is not None:
            found.append(NamedPeriod(start=start, text=match.group(0), item_id=item_id))

    for m in _ISO_DATE_RE.finditer(text):
        add(_safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m)
    for m in _MONTH_DAY_YEAR_RE.finditer(text):
        add(_safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))), m)
    for m in _DAY_MONTH_YEAR_RE.finditer(text):
        add(_safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))), m)
    for m in _NUMERIC_DATE_RE.finditer(text):
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        readings = {_safe_date(year, a, b), _safe_date(year, b, a)} - {None}
        covered.append(m.span())
        for reading in readings:
            found.append(NamedPeriod(start=reading, text=m.group(0), item_id=item_id))

    def inside_covered(span: tuple[int, int]) -> bool:
        return any(s <= span[0] and span[1] <= e for s, e in covered)

    for m in _MONTH_YEAR_RE.finditer(text):
        if inside_covered(m.span()):
            continue
        add(_safe_date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1), m)
    for m in _YEAR_RE.finditer(text):
        if inside_covered(m.span()):
            continue
        found.append(
            NamedPeriod(start=date(int(m.group(1)), 1, 1), text=m.group(0), item_id=item_id)
        )
    return found


def passed_named_dates(
    profile: Mapping[str, Any], *, researched_on: date, today: date
) -> list[NamedPeriod]:
    """Dates, months, and years the profile names that have begun since it was researched.

    A period counts when its first day is after ``researched_on`` and on or
    before ``today``: the research described it as ahead, and it no longer
    is. A period that began on or before the research day is history the
    research already knew; one still ahead is still ahead. (A month named in
    the month the research ran — "later in September" — starts before the
    research day and is not counted; the age limit bounds that case.)
    """
    passed: list[NamedPeriod] = []
    for raw in profile.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        item_id = str(raw.get("item_id", "") or "")
        for field_name in _DATED_ITEM_FIELDS:
            value = raw.get(field_name)
            if not isinstance(value, str) or not value:
                continue
            for period in named_periods(value, item_id=item_id):
                if researched_on < period.start <= today:
                    passed.append(period)
    return passed


def parse_research_date(value: object) -> date | None:
    """A ``YYYY-MM-DD`` research date, or ``None``."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _local_date(ts: float) -> date:
    # ``research_date`` is written from local time (``time.strftime``), so the
    # comparison day is local too.
    return datetime.fromtimestamp(ts).date()


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """One stored, completed research profile."""

    key: str
    components: dict[str, str]
    module_id: str
    project: dict[str, str]
    created_ts: float
    last_used_ts: float
    research_date: str
    profile: dict
    profile_sha256: str
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "components": dict(self.components),
            "subject": {"module_id": self.module_id, "project": dict(self.project)},
            "created_ts": self.created_ts,
            "last_used_ts": self.last_used_ts,
            "research_date": self.research_date,
            "profile": self.profile,
            "profile_sha256": self.profile_sha256,
            "usage": dict(self.usage),
        }

    def age_seconds(self, now: float) -> float:
        return max(0.0, now - self.created_ts)


def _timestamp(value: object, *, now: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        ts = float(value)
    except OverflowError:
        return None
    if not math.isfinite(ts) or ts <= 0 or ts > now + _MAX_FUTURE_SKEW_SECONDS:
        return None
    return ts


# The serialized ``ResearchItem`` / ``DimensionStatus`` fields a stored profile
# is checked against, so a row that validates always deserializes.
_ITEM_STR_FIELDS = (
    "item_id",
    "dimension_id",
    "topic",
    "category",
    "requirement",
    "authority",
    "code_reference",
    "actionability",
    "notes",
)
_STATUS_STR_FIELDS = ("dimension_id", "status", "error")
_STATUS_COUNT_FIELDS = (
    "item_count",
    "grounded_count",
    "web_search_requests",
    "web_fetch_requests",
)


def _fields_problem(
    record: Mapping[str, Any], str_fields: Iterable[str], count_fields: Iterable[str], what: str
) -> str | None:
    for name in str_fields:
        value = record.get(name)
        if value is not None and not isinstance(value, str):
            return f"{what}'s {name} is not a string"
    for name in count_fields:
        value = record.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            return f"{what}'s {name} is not a non-negative whole number"
    return None


def profile_problem(profile: object, *, dimension_ids: Iterable[str] | None = None) -> str | None:
    """Why ``profile`` (a serialized profile) may not be stored or reused, or ``None``.

    A reusable profile is a completed one: at least one dimension, every
    dimension ``completed``, and — when ``dimension_ids`` is given — exactly
    those dimensions in that order. Items must be objects. A ``reuse`` record
    is never stored (only fresh research is).
    """
    if not isinstance(profile, Mapping):
        return "the profile is not an object"
    statuses = profile.get("dimension_statuses")
    if not isinstance(statuses, list) or not statuses:
        return "the profile records no research dimensions"
    if not all(isinstance(s, Mapping) for s in statuses):
        return "a dimension status is not an object"
    failed = [str(s.get("dimension_id", "")) for s in statuses if s.get("status") != "completed"]
    if failed:
        return f"{len(failed)} of {len(statuses)} research dimension(s) did not complete ({', '.join(failed)})"
    if dimension_ids is not None:
        recorded = tuple(str(s.get("dimension_id", "")) for s in statuses)
        if recorded != tuple(dimension_ids):
            return "the profile's dimensions are not the ones its key names"
    for status in statuses:
        problem = _fields_problem(status, _STATUS_STR_FIELDS, _STATUS_COUNT_FIELDS, "a dimension status")
        if problem:
            return problem
    items = profile.get("items")
    if not isinstance(items, list) or not all(isinstance(i, Mapping) for i in items):
        return "the profile's items are not a list of objects"
    for item in items:
        problem = _fields_problem(item, _ITEM_STR_FIELDS, (), "an item")
        if problem:
            return problem
        for name in ("source_urls", "accepted_sources", "applicable_module_ids"):
            value = item.get(name)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(u, str) for u in value)
            ):
                return f"an item's {name} is not a list of strings"
        if "grounded" in item and not isinstance(item.get("grounded"), bool):
            return "an item's grounded flag is not true or false"
        confidence = item.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
        ):
            return "an item's confidence is not a finite number"
    project = profile.get("project")
    if project is not None and not (
        isinstance(project, Mapping)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in project.items())
    ):
        return "the profile's project is not a string mapping"
    if parse_research_date(profile.get("research_date")) is None:
        return "the profile has no valid research date"
    if profile.get("reuse") is not None:
        return "the profile is itself a reuse"
    return None


def _usage_problem(usage: object) -> str | None:
    if not isinstance(usage, Mapping):
        return "usage is not an object"
    for key, value in usage.items():
        if key == "model":
            if not isinstance(value, str):
                return "usage model is not a string"
            continue
        if key not in _USAGE_INT_KEYS:
            return f"unknown usage field {key!r}"
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"usage field {key!r} is not a non-negative whole number"
    return None


def entry_from_dict(key: object, raw: object, *, now: float) -> tuple[CacheEntry | None, str]:
    """Validate one stored row. Returns ``(entry, "")`` or ``(None, reason)``."""
    if not isinstance(key, str) or not isinstance(raw, Mapping):
        return None, "the row is not a keyed object"
    if raw.get("key") != key:
        return None, "the row's key does not match its slot"
    components = raw.get("components")
    if not isinstance(components, Mapping) or set(components) != set(KEY_COMPONENT_ORDER):
        return None, "the row's key components are not this policy's"
    if not all(isinstance(v, str) for v in components.values()):
        return None, "a key component is not a string"
    if derive_key(components) != key:
        return None, "the row's key does not match its components"
    subject = raw.get("subject")
    if not isinstance(subject, Mapping):
        return None, "the row has no subject"
    module_id = subject.get("module_id")
    project = subject.get("project")
    if not isinstance(module_id, str) or module_id != components.get("module_id"):
        return None, "the row's module does not match its key"
    if not isinstance(project, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in project.items()
    ):
        return None, "the row's project is not a string mapping"
    if digest(dict(project)) != components.get("project"):
        return None, "the row's project does not match its key"
    created_ts = _timestamp(raw.get("created_ts"), now=now)
    if created_ts is None:
        return None, "the row's creation time is missing, invalid, or in the future"
    last_used = raw.get("last_used_ts")
    last_used_ts = created_ts if last_used is None else _timestamp(last_used, now=now)
    if last_used_ts is None:
        return None, "the row's last-used time is invalid or in the future"
    profile = raw.get("profile")
    if not isinstance(profile, Mapping):
        return None, "the row has no profile"
    if raw.get("profile_sha256") != digest(dict(profile)):
        return None, "the row's profile does not match its digest"
    if len(canonical_json(dict(profile)).encode("utf-8")) > MAX_PROFILE_BYTES:
        return None, "the row's profile is larger than the size bound"
    try:
        dimension_ids = tuple(json.loads(components["dimension_ids"]))
    except (TypeError, ValueError):
        return None, "the row's dimension list is unreadable"
    problem = profile_problem(profile, dimension_ids=dimension_ids)
    if problem:
        return None, problem
    research_date = raw.get("research_date")
    researched_on = parse_research_date(research_date)
    if researched_on is None or research_date != profile.get("research_date"):
        return None, "the row's research date is missing or disagrees with its profile"
    if abs((researched_on - _local_date(created_ts)).days) > 1:
        return None, "the row's research date disagrees with its creation time"
    if dict(profile.get("project") or {}) != dict(project):
        return None, "the row's profile is for another project"
    usage = raw.get("usage", {})
    usage_problem = _usage_problem(usage)
    if usage_problem:
        return None, usage_problem
    return (
        CacheEntry(
            key=key,
            components=dict(components),
            module_id=module_id,
            project=dict(project),
            created_ts=created_ts,
            last_used_ts=last_used_ts,
            research_date=str(research_date),
            profile=dict(profile),
            profile_sha256=str(raw.get("profile_sha256")),
            usage=dict(usage),
        ),
        "",
    )


# ---------------------------------------------------------------------------
# Lookup and store
# ---------------------------------------------------------------------------


@dataclass
class ReuseLookup:
    """What a lookup found. ``entry`` is set only on a hit."""

    outcome: str
    reason: str = ""
    entry: CacheEntry | None = None
    differing: list[str] = field(default_factory=list)
    age_days: int | None = None
    passed_dates: list[str] = field(default_factory=list)
    rejected: int = 0


@dataclass
class StoreResult:
    outcome: str
    reason: str = ""
    rejected: int = 0


@dataclass
class _Loaded:
    entries: dict[str, CacheEntry]
    rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    #: Why the file could not be used at all ("" when it could, or is absent).
    unreadable: str = ""


class ResearchCache:
    """The on-disk store. Every method takes the process-wide lock.

    ``clock`` is injected (epoch seconds) so tests never depend on the real
    date. Two processes (the GUI and a command-line run) are not coordinated:
    each write is atomic (temp file + replace), so the file is never torn, but
    the later writer's view wins.
    """

    def __init__(self, path: str | Path | None = None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else default_research_cache_path()
        self.clock = clock

    # -- file ---------------------------------------------------------------

    def _load(self, now: float) -> _Loaded:
        if not self.path.exists():
            return _Loaded(entries={})
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _Loaded(entries={}, unreadable=f"the file could not be read ({type(exc).__name__})")
        if not isinstance(payload, dict):
            return _Loaded(entries={}, unreadable="the file is not a research cache")
        version = payload.get("version")
        if isinstance(version, bool) or version != FILE_SCHEMA_VERSION:
            return _Loaded(
                entries={},
                unreadable=f"the file has schema version {version!r}; this build reads {FILE_SCHEMA_VERSION}",
            )
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, dict):
            return _Loaded(entries={}, unreadable="the file has no entry table")
        loaded = _Loaded(entries={})
        for key, raw in raw_entries.items():
            entry, reason = entry_from_dict(key, raw, now=now)
            if entry is None:
                loaded.rejected += 1
                loaded.rejections.append(reason)
                continue
            loaded.entries[key] = entry
        return loaded

    def _save(self, entries: Mapping[str, CacheEntry], now: float) -> None:
        kept = sorted(entries.values(), key=lambda e: e.last_used_ts)[-MAX_ENTRIES:]
        payload = {
            "version": FILE_SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "saved_at": now,
            "entries": {e.key: e.to_dict() for e in kept},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".research_cache.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- operations ---------------------------------------------------------

    def lookup(self, key: ResearchKey, *, max_age_days: int, now: float | None = None) -> ReuseLookup:
        """Find a reusable profile for ``key`` (see the module docstring)."""
        now = self.clock() if now is None else now
        with _LOCK:
            loaded = self._load(now)
            if loaded.unreadable:
                return ReuseLookup(outcome=OUTCOME_UNREADABLE, reason=loaded.unreadable)
            entry = loaded.entries.get(key.key)
            if entry is None:
                same_subject = [
                    e
                    for e in loaded.entries.values()
                    if e.module_id == key.module_id and e.project == dict(key.project)
                ]
                if not same_subject:
                    return ReuseLookup(outcome=OUTCOME_ABSENT, rejected=loaded.rejected)
                latest = max(same_subject, key=lambda e: e.created_ts)
                differing = differing_components(latest.components, key.components)
                return ReuseLookup(
                    outcome=OUTCOME_CHANGED,
                    reason="research inputs differ from the stored profile's: " + ", ".join(differing),
                    differing=differing,
                    age_days=int(latest.age_seconds(now) // 86400),
                    rejected=loaded.rejected,
                )
            age_seconds = entry.age_seconds(now)
            age_days = int(age_seconds // 86400)
            if age_seconds > max_age_days * 86400:
                return ReuseLookup(
                    outcome=OUTCOME_STALE,
                    reason=f"researched {entry.research_date}, older than {max_age_days} day(s)",
                    age_days=age_days,
                    rejected=loaded.rejected,
                )
            researched_on = parse_research_date(entry.research_date)
            passed = passed_named_dates(
                entry.profile, researched_on=researched_on, today=_local_date(now)
            )
            if passed:
                named = sorted({p.text for p in passed})
                return ReuseLookup(
                    outcome=OUTCOME_STALE,
                    reason=(
                        f"researched {entry.research_date}; a date it names has begun since: "
                        + ", ".join(named[:5])
                    ),
                    age_days=age_days,
                    passed_dates=named,
                    rejected=loaded.rejected,
                )
            entry.last_used_ts = now
            try:
                self._save(loaded.entries, now)
            except OSError:
                # The hit stands; only the LRU stamp is lost.
                pass
            return ReuseLookup(
                outcome=OUTCOME_HIT, entry=entry, age_days=age_days, rejected=loaded.rejected
            )

    def store(
        self,
        key: ResearchKey,
        profile: Mapping[str, Any],
        *,
        usage: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> StoreResult:
        """Store a completed profile under ``key``. Partial profiles are refused."""
        now = self.clock() if now is None else now
        profile = dict(profile)
        problem = profile_problem(profile, dimension_ids=key.dimension_ids)
        if problem:
            return StoreResult(outcome=STORE_PARTIAL, reason=problem)
        if dict(profile.get("project") or {}) != dict(key.project):
            return StoreResult(outcome=STORE_PARTIAL, reason="the profile is for another project")
        if len(canonical_json(profile).encode("utf-8")) > MAX_PROFILE_BYTES:
            return StoreResult(outcome=STORE_TOO_LARGE, reason="the profile is larger than the size bound")
        clean_usage = {k: v for k, v in dict(usage or {}).items() if k in _USAGE_INT_KEYS or k == "model"}
        if _usage_problem(clean_usage):
            clean_usage = {}
        entry = CacheEntry(
            key=key.key,
            components=dict(key.components),
            module_id=key.module_id,
            project=dict(key.project),
            created_ts=now,
            last_used_ts=now,
            research_date=str(profile.get("research_date")),
            profile=profile,
            profile_sha256=digest(profile),
            usage=clean_usage,
        )
        # A row is written only if the load-time validation would accept it,
        # so the store and the reader can never disagree about one.
        _checked, problem = entry_from_dict(entry.key, entry.to_dict(), now=now)
        if problem:
            return StoreResult(outcome=STORE_INVALID, reason=problem)
        with _LOCK:
            loaded = self._load(now)
            if loaded.unreadable:
                return StoreResult(outcome=STORE_UNREADABLE_FILE, reason=loaded.unreadable)
            loaded.entries[entry.key] = entry
            try:
                self._save(loaded.entries, now)
            except OSError as exc:
                return StoreResult(outcome=STORE_WRITE_FAILED, reason=f"{type(exc).__name__}: {exc}")
            return StoreResult(outcome=STORE_STORED, rejected=loaded.rejected)

    def entries(self, now: float | None = None) -> tuple[list[CacheEntry], int, str]:
        """``(valid entries newest first, rejected row count, unreadable reason)``."""
        now = self.clock() if now is None else now
        with _LOCK:
            loaded = self._load(now)
        ordered = sorted(loaded.entries.values(), key=lambda e: e.created_ts, reverse=True)
        return ordered, loaded.rejected, loaded.unreadable

    def delete(self, keys: Iterable[str] | None = None, now: float | None = None) -> int:
        """Delete the given entries (all when ``keys`` is ``None``); returns how many.

        Deleting every entry removes the file itself, even one this build
        cannot read — the one way to reset it.
        """
        now = self.clock() if now is None else now
        with _LOCK:
            if keys is None:
                if not self.path.exists():
                    return 0
                loaded = self._load(now)
                self.path.unlink()
                return len(loaded.entries)
            loaded = self._load(now)
            if loaded.unreadable:
                return 0
            wanted = set(keys)
            removed = [k for k in list(loaded.entries) if k in wanted]
            for k in removed:
                del loaded.entries[k]
            if removed:
                self._save(loaded.entries, now)
            return len(removed)


# ---------------------------------------------------------------------------
# Provenance and wording
# ---------------------------------------------------------------------------


def reuse_provenance(entry: CacheEntry, *, now: float, max_age_days: int) -> dict:
    """The ``reuse`` record a reused profile carries everywhere it goes."""
    return {
        "source": "research_cache",
        "policy_version": POLICY_VERSION,
        "key": entry.key,
        "researched_at": _iso_utc(entry.created_ts),
        "research_date": entry.research_date,
        "age_days": int(entry.age_seconds(now) // 86400),
        "max_age_days": int(max_age_days),
        "reused_at": _iso_utc(now),
        "date_basis": DATE_BASIS_CURRENT,
    }


def _reuse_record(profile: object) -> Mapping[str, Any] | None:
    record = profile.get("reuse") if isinstance(profile, Mapping) else getattr(profile, "reuse", None)
    return record if isinstance(record, Mapping) else None


def age_phrase(age_days: object) -> str:
    try:
        days = int(age_days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "an unknown time"
    if days <= 0:
        return "less than a day"
    return f"{days} day{'s' if days != 1 else ''}"


def reuse_notice(profile: object) -> str | None:
    """The one sentence both reports print for a reused profile, else ``None``."""
    record = _reuse_record(profile)
    if record is None:
        return None
    research_date = str(record.get("research_date", "") or "unknown date")
    if record.get("scopes"):
        return (
            "Research components reused from the research cache for this review: "
            + ", ".join(record["scopes"])
            + f". Oldest reused component researched {research_date} "
            f"({age_phrase(record.get('age_days'))} before this run). "
            "Other components may be fresh; component dates and keys are saved "
            "in the profile. Requirements that changed since reuse are not "
            "reflected. Set SPEC_CRITIC_RESEARCH_CACHE=refresh to research again."
        )
    return (
        "This research was reused from the research cache, not re-run for this "
        f"review: it was researched {research_date} ({age_phrase(record.get('age_days'))} "
        "before this run) for the same location, client, module, and research "
        "inputs. Requirements that changed since then are not reflected. "
        "Set SPEC_CRITIC_RESEARCH_CACHE=refresh to research again."
    )


def reuse_age_days(profile: object) -> int | None:
    """The reused profile's age in whole days, or ``None`` for fresh research."""
    record = _reuse_record(profile)
    if record is None:
        return None
    try:
        return max(0, int(record.get("age_days")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Diagnostics rollup
# ---------------------------------------------------------------------------

_SAVED_KEYS = (
    "dimension_calls",
    "api_requests",
    "web_search_requests",
    "web_fetch_requests",
    "input_tokens",
    "output_tokens",
)
#: What a saved-cost estimate needs, per model.
_PRICED_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "web_search_requests",
)


def summarize_research_reuse(records: Iterable[Mapping[str, Any]]) -> dict | None:
    """Roll the per-lookup records up for ``DiagnosticsReport.summary()``.

    ``None`` when there are none, so a summary is otherwise byte-identical.
    ``saved`` sums the recorded usage of the research each hit replaced — what
    the original run spent on it, not a measurement of this run.
    """
    records = [r for r in records if isinstance(r, Mapping)]
    if not records:
        return None
    by_outcome: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    changed_components: dict[str, int] = {}
    stale_reasons: dict[str, int] = {}
    store_outcomes: dict[str, int] = {}
    saved = {k: 0 for k in _SAVED_KEYS}
    saved_by_model: dict[str, dict[str, int]] = {}
    hit_ages: list[int] = []
    rejected = 0
    for record in records:
        outcome = str(record.get("outcome", "") or "unknown")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        mode = str(record.get("mode", "") or "unknown")
        by_mode[mode] = by_mode.get(mode, 0) + 1
        for name in record.get("differing") or []:
            changed_components[str(name)] = changed_components.get(str(name), 0) + 1
        if outcome == OUTCOME_STALE:
            kind = "named_date" if record.get("passed_dates") else "age"
            stale_reasons[kind] = stale_reasons.get(kind, 0) + 1
        store = record.get("store")
        if isinstance(store, str) and store:
            store_outcomes[store] = store_outcomes.get(store, 0) + 1
        try:
            rejected += max(0, int(record.get("rejected", 0) or 0))
        except (TypeError, ValueError):
            pass
        if outcome == OUTCOME_HIT:
            usage = record.get("saved") if isinstance(record.get("saved"), Mapping) else {}
            model = str(usage.get("model", "") or "unknown")
            per_model = saved_by_model.setdefault(model, {k: 0 for k in _PRICED_KEYS})
            for key in _SAVED_KEYS:
                try:
                    saved[key] += max(0, int(usage.get(key, 0) or 0))
                except (TypeError, ValueError):
                    continue
            for key in _PRICED_KEYS:
                try:
                    per_model[key] += max(0, int(usage.get(key, 0) or 0))
                except (TypeError, ValueError):
                    continue
            if record.get("age_days") is not None:
                try:
                    hit_ages.append(int(record["age_days"]))
                except (TypeError, ValueError):
                    pass
    lookups = sum(n for o, n in by_outcome.items() if o != OUTCOME_REFRESH)
    hits = by_outcome.get(OUTCOME_HIT, 0)
    return {
        "records": len(records),
        "lookups": lookups,
        "hits": hits,
        "hit_rate": round(hits / lookups, 4) if lookups else None,
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_mode": dict(sorted(by_mode.items())),
        "changed_components": dict(sorted(changed_components.items())),
        "stale_reasons": dict(sorted(stale_reasons.items())),
        "store_outcomes": dict(sorted(store_outcomes.items())),
        "rejected_entries": rejected,
        "saved": saved,
        "saved_by_model": dict(sorted(saved_by_model.items())),
        "oldest_hit_age_days": max(hit_ages) if hit_ages else None,
        "policy_version": POLICY_VERSION,
    }


def summary_line(rollup: Mapping[str, Any] | None) -> str | None:
    """One line for ``DiagnosticsReport.to_text``, or ``None``."""
    if not rollup:
        return None
    outcomes = ", ".join(f"{n} {o}" for o, n in (rollup.get("by_outcome") or {}).items())
    saved = rollup.get("saved") or {}
    return (
        f"Research reuse (EX-05): {outcomes}; saved {saved.get('dimension_calls', 0)} "
        f"research dimension(s), {saved.get('web_search_requests', 0)} search(es) "
        "(as recorded when first researched)"
    )
