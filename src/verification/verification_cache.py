"""Persistent verification result cache (plan section 7.2).

Caches verdicts by a normalized claim key so two findings that ask the same
external question only verify once. Crucially, the key includes more than
``codeReference`` alone — two findings that cite the same standard but make
different claims (e.g. "is current" vs "was withdrawn") still verify
separately.

Phase 10: the cache persists to disk between runs. Cycle label is part of
the key, so switching code cycles naturally invalidates everything from the
prior cycle. On top of that, entries expire by age: the
``SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`` override defaults to **60 days**
(pruned on load — see :func:`cache_ttl_days`); an explicit ``0`` restores the
legacy no-expiry "database" behavior. Growth is bounded independently of age
by an LRU entry cap, ``SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES`` (default
5000, ``0`` disables — see :func:`cache_max_entries`): a hit touches the
entry's ``last_used_ts`` and the least-recently-used entries are evicted when
a put / save / load leaves the store over the cap. ``last_used_ts`` is an
additive on-disk field (a legacy row loads with ``created_ts`` as its
fallback — no schema bump), and the file is written compact (no indentation)
through the same atomic temp-file + replace.

Only **grounded conclusive verdicts** are stored and reused: a CONFIRMED,
CORRECTED, or DISPUTED backed by a substantive accepted citation (and, for
CONFIRMED / CORRECTED, a verbatim source quote), from a verification that
neither failed nor ran out of budget. One predicate,
:func:`cache_ineligibility_reason`, decides that at every boundary — ``put``,
``get``, and disk load — so a result the cache would refuse to write can never
be read back either. An UNVERIFIED is never reused, grounded or not: it is the
verifier saying it could not settle the claim, and replaying that for the TTL
window stopped every later run from trying again (plan WP-10). Uncertainty is
shared only in-process, among equivalent findings of the same run
(``pipeline._verify_findings_singleflight``).

The verifier model is intentionally omitted from the cache key. Cache entries
represent grounded verdict semantics for a finding/cycle/action/claim, not the
particular model that produced the verdict. ``model_used`` is still persisted
as entry provenance for reports and future maintenance tools, but changing
``SPEC_CRITIC_VERIFICATION_MODEL`` or ``SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL``
does not invalidate existing hits; clear the cache file (see
``default_cache_path``) or set ``SPEC_CRITIC_CACHE_PATH`` to a fresh file to
force re-verification with a new model policy.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .verifier import VerificationResult

from ..core.code_cycles import CodeCycle
from .source_grounding import substantive_sources


_WHITESPACE_RE = re.compile(r"\s+")

# JSON schema version for the on-disk cache file. Bumped when the entry
# shape changes incompatibly so older readers can refuse to load instead of
# silently mis-deserializing.
#
# v2 — invalidates pre-v2 entries that may have stored a CONFIRMED/CORRECTED
# verdict without an accepted external citation. The strengthened
# :func:`src.verifier._enforce_grounding_invariant` would now downgrade
# those verdicts, so silently reusing them would let the old behavior
# leak through. Bumping the version drops every v1 cache file on first
# load; users get fresh verifications under the new invariant.
#
# v3 — adds ``source_quote`` (the verbatim snippet
# the model said it read) to every entry. v2 entries don't carry the
# quote, so silently reusing them would produce CONFIRMED / CORRECTED
# hits whose report rendering has no source_quote to show — a regression
# against the new invariant. Bumping the version drops every v2 cache
# file on first load; users get fresh verifications under the new shape.
#
# v4 — folds a pinned-standards-edition fingerprint into ``make_cache_key``
# (see :func:`_standards_fingerprint`). v3 keys were computed without it, so
# a v3 file's keys can never collide with a v4 lookup anyway — but the entries
# would linger as dead weight until the TTL pruned them, and more importantly a
# v3 entry was grounded against whatever editions were pinned at write time with
# no record of which. Bumping drops every v3 file on first load so the cache
# repopulates with edition-fingerprinted keys.
_CACHE_SCHEMA_VERSION = 4

# Verdicts that may only be persisted with at least one accepted external
# citation. Mirrors ``verifier._GROUNDING_GATED_VERDICTS`` (not imported —
# the verifier imports this module; a module-level import would be circular).
# DISPUTED joined CONFIRMED / CORRECTED without a schema bump: the load-time
# re-check in :meth:`VerificationCache.load_from_disk` drops any legacy row
# that violates it, so a v4 file written before the gate cannot replay an
# uncited "the claim is wrong" verdict for 60 days.
_CITATION_GATED_VERDICTS = ("CONFIRMED", "CORRECTED", "DISPUTED")

# Verdicts that may only be persisted with a non-empty ``source_quote`` — the
# verbatim snippet the model said it relied on (the v3 invariant above).
# Mirrors ``verifier._demote_if_missing_source_quote``, which demotes exactly
# these two verdicts at parse time; a DISPUTED may legitimately carry no quote
# (the evidence conflicts rather than supports), so it is citation-gated but
# not quote-gated. Enforced in :meth:`VerificationCache.put` and re-checked in
# :meth:`VerificationCache.load_from_disk`, so a quote-less CONFIRMED /
# CORRECTED can neither be written by a future call site nor resurrected from
# a hand-edited file — before this the parse-time demotion was the only guard.
_QUOTE_GATED_VERDICTS = ("CONFIRMED", "CORRECTED")

# The verdicts the cache may reuse: the conclusive ones. Every one of them is
# citation-gated, and UNVERIFIED is deliberately absent — see
# :func:`cache_ineligibility_reason`.
_CONCLUSIVE_VERDICTS = _CITATION_GATED_VERDICTS

# How far in the future a persisted timestamp may sit before the record is
# treated as invalid rather than as a clock difference. A cache file copied
# between machines may be a little ahead; one stamped weeks ahead is not a
# time, and would never expire.
_MAX_FUTURE_SKEW_SECONDS = 86400.0


def cache_ineligibility_reason(result) -> str | None:
    """Why ``result`` may not be persisted or reused — or ``None`` when it may.

    The one cache-eligibility predicate (plan WP-10), applied at every
    boundary: :meth:`VerificationCache.put` refuses what it rejects,
    :meth:`VerificationCache.get` drops an in-memory entry it rejects instead
    of replaying it, and :meth:`VerificationCache.load_from_disk` ignores a
    stored row it rejects — individually, never by flushing the file.

    Reusable means a grounded conclusive verdict from a clean verification:

    * never a local classification (it is per-finding and free);
    * never a replay (``hit`` / ``shared``) — re-storing one would reset its
      age and launder a stale verdict as fresh;
    * never an operational failure or a budget-exhausted result — transient,
      and a re-run must try again;
    * only CONFIRMED / CORRECTED / DISPUTED. UNVERIFIED is the verifier
      saying it could not settle the claim; replaying it for the TTL window
      suppressed every later attempt, grounded or not;
    * grounded, with at least one *substantive* accepted citation
      (``source_grounding.is_substantive_source``: ``[""]`` is not one);
    * CONFIRMED / CORRECTED also need a non-blank ``source_quote``. DISPUTED
      is citation-gated but not quote-gated, mirroring the verifier's parse
      rule (``_demote_if_missing_source_quote``).

    Returns a short reason for tests and diagnostics; the policy is the
    ``None`` / not-``None`` distinction.
    """
    if result is None:
        return "no result"
    cache_status = (getattr(result, "cache_status", "") or "").strip()
    if cache_status == "local_skip" or (
        (getattr(result, "verification_mode", "") or "").strip() == "local_skip"
    ):
        return "local classification"
    if cache_status in (CACHE_STATUS_HIT, CACHE_STATUS_SHARED):
        return f"a {cache_status} replay, not a fresh verification"
    if bool(getattr(result, "verification_failed", False)):
        return "operational failure"
    if bool(getattr(result, "budget_exhausted", False)):
        return "search budget exhausted"
    verdict = (getattr(result, "verdict", "") or "").strip().upper()
    if verdict not in _CONCLUSIVE_VERDICTS:
        return f"inconclusive verdict ({verdict or 'none'})"
    if not bool(getattr(result, "grounded", False)):
        return "not grounded"
    if not (
        substantive_sources(getattr(result, "accepted_sources", None))
        or substantive_sources(getattr(result, "sources", None))
    ):
        return "no substantive accepted citation"
    if verdict in _QUOTE_GATED_VERDICTS and not (
        (getattr(result, "source_quote", "") or "").strip()
    ):
        return "no source quote"
    return None


def is_cache_eligible(result) -> bool:
    """True when :func:`cache_ineligibility_reason` finds nothing against ``result``."""
    return cache_ineligibility_reason(result) is None

# Closed set of the ``VerificationResult.cache_status`` values the run-local
# reuse layers stamp (``"n/a"`` / ``"local_skip"`` are the verifier's own).
# ``miss`` — the verifier ran fresh; ``hit`` — replayed from a cache entry
# (the only value that carries ``cache_entry_created_ts`` and earns the
# report's cache-age badge); ``shared`` — a single-flight follower inherited
# its leader's clean *ungrounded* verdict in-process
# (``pipeline._verify_findings_singleflight``). A shared verdict never touches
# the entry store in either direction, so it must never be counted as a disk
# replay: the badge, the force-refresh hint, and the diagnostics hit/miss
# counters all key on ``hit`` / ``miss`` exactly.
CACHE_STATUS_MISS = "miss"
CACHE_STATUS_HIT = "hit"
CACHE_STATUS_SHARED = "shared"

# Cache-key claim digest length (hex chars). 24 hex chars = 96 bits of entropy,
# enough that two distinct claims colliding is astronomically unlikely even
# across a corpus of millions of findings; the previous 16 hex chars / 64-bit
# digest was thin under birthday-bound math (50%+ collision risk at ~5B keys
# and observable collision risk at ~1M). Lookups that present an old 16-char
# digest will simply miss in the new cache, which is the safe failure mode —
# the next verification call re-grounds the claim and writes a 24-char entry.
# Going higher (32+) would add latency and disk-write bytes for diminishing
# safety; 24 hex chars is the deliberate sweet spot.
_CLAIM_DIGEST_LEN = 24


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def _digest(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_CLAIM_DIGEST_LEN]


def _claim_summary(finding) -> str:
    """Compact text capturing the claim of a finding for cache keying.

    The plan calls out that the cache must distinguish two findings that
    quote the same code reference but assert different things. We hash the
    issue text plus existing/replacement text so different claims diverge.
    """
    parts = [
        _normalize(getattr(finding, "issue", "")),
        _normalize(getattr(finding, "existingText", "")),
        _normalize(getattr(finding, "replacementText", "")),
    ]
    return "\n".join(p for p in parts if p)


def _standards_fingerprint(cycle) -> str:
    """Digest of the cycle's pinned-standard editions, for the cache key.

    Folded into ``make_cache_key`` so that correcting an edition string *within*
    a cycle (e.g. fixing an ASHRAE edition that was wrong) invalidates the
    verdicts that were grounded against the *old* edition, even though
    ``cycle.label`` is unchanged. Built from ``edition_summary_lines()`` — the
    same stable, declaration-ordered render the verifier prompt pins — so the
    fingerprint tracks the editions the model actually reasoned against. It
    deliberately ignores provenance-only metadata (``StandardEdition.source`` /
    the UNVERIFIED flag): confirming that an already-correct edition was right
    all along does not change the verification question, so those entries should
    stay warm. Returns ``""`` for a cycle that exposes no editions (degrades to
    the prior label-only behavior for test doubles / standards-less cycles).
    """
    lines = getattr(cycle, "edition_summary_lines", None)
    rendered = "\n".join(lines()) if callable(lines) else ""
    return _digest(rendered)


#: Bumped when the verifier's edition-authority wording changes again, so
#: verdicts rendered under the superseded wording are never reused.
BASIS_POLICY_NAMESPACE = "bp1"


def _basis_policy_namespace(cycle: CodeCycle) -> str:
    """``BASIS_POLICY_NAMESPACE`` for a location-aware module, else ``""``.

    Empty for California, whose verifier wording is unchanged — its keys stay
    byte-identical and its entries stay warm. The lookup degrades to ``""`` on
    any resolution failure: a cache key must never be the thing that raises.
    """
    try:
        from ..modules import module_for_cycle

        module = module_for_cycle(cycle)
    except Exception:  # pragma: no cover - defensive; keys must not raise
        return ""
    return BASIS_POLICY_NAMESPACE if getattr(module, "project_profile_enabled", False) else ""


def make_cache_key(
    finding,
    *,
    cycle: CodeCycle,
    jurisdiction_fingerprint: str | None = None,
    basis_fingerprint: str | None = None,
) -> str:
    """Build a stable cache key for a finding under a given code cycle.

    The key includes the normalized cycle label, a fingerprint of the cycle's
    pinned-standard editions, the action type, the code reference, and a digest
    of the finding claim summary. It intentionally does *not* include the
    verifier model: the cache is keyed by the grounded verification question and
    code-cycle semantics, while ``VerificationResult.model_used`` is stored only
    as provenance. Changing verifier models therefore reuses compatible grounded
    cache entries.

    The standards fingerprint (``_standards_fingerprint``) is the lever that
    closes a latent footgun: the cycle *label* alone ("2025") does not change
    when an edition string *inside* the cycle is corrected, so before this was
    added a fix to a pinned edition left every verdict grounded against the old
    edition silently cached. Folding the rendered editions into the key means an
    edition correction now produces fresh keys, re-grounding the affected
    findings against the corrected edition. (Switching cycles still invalidates
    too, since the label leads the key.)

    ``jurisdiction_fingerprint`` (WS-4, D-9) appends a sixth segment **only
    when non-None** — the ``ProjectProfile.jurisdiction_fingerprint()`` of a
    profile-bearing run — so a compliance/jurisdictional verdict grounded
    against one city's codes can never replay for a different city. Without
    a profile the key shape is byte-identical to the five-segment format (no
    ``_no_loc`` sentinel), so every existing CA cache entry stays warm and no
    schema bump is needed: profile-present keys are simply new keys.

    The **basis-policy namespace** (CLAUDE.md, "Cache namespace") is
    appended for a module whose verifier prompt no longer presents pinned
    editions as authoritative. Those verdicts answer a *different question*
    from the ones cached under the old wording — a verdict that disputed an
    adoption-deferring finding because the pin was authoritative must not
    replay now that it is not — so they need their own namespace. It is
    derived from the cycle here rather than threaded as a parameter
    deliberately: the key is already a pure function of the cycle, and a
    parameter would have to reach three call sites plus every ``get``/``put``
    caller, where one missed site silently replays a stale verdict.

    ``basis_fingerprint`` (CLAUDE.md, "Verification cache key") appends a final
    ``gb:<fp>`` segment **only when the run's governing basis was actually
    rendered into the verifier prompt**. That "actually rendered" wording is
    the whole contract: the fingerprint must describe the context the verifier
    saw, not the context that happened to be available, because a verdict
    reached *with* researched adoption facts answers a different question from
    one reached without them. It is threaded rather than derived because the
    basis is per-run state the cycle knows nothing about; the safety net is
    that a caller which omits it produces the exact pre-existing key, so the
    only reachable failure is a redundant re-verification, never a
    basis-informed verdict replaying for a basis-less run.
    """
    code_ref = _normalize(getattr(finding, "codeReference", "")) or "_no_ref"
    action = _normalize(getattr(finding, "actionType", "")) or "_no_action"
    cycle_label = _normalize(getattr(cycle, "label", "")) or "_no_cycle"
    std_fp = _standards_fingerprint(cycle) or "_no_std"
    claim = _claim_summary(finding)
    key = f"{cycle_label}|{std_fp}|{action}|{code_ref}|{_digest(claim)}"
    if jurisdiction_fingerprint:
        key = f"{key}|{jurisdiction_fingerprint}"
    namespace = _basis_policy_namespace(cycle)
    if namespace:
        key = f"{key}|{namespace}"
    if basis_fingerprint:
        key = f"{key}|gb:{basis_fingerprint}"
    return key


# Canonical "disable" tokens for boolean env-var flags. Anything else —
# including an unset variable — leaves the default-enabled behavior in place.
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def _env_flag_disabled(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _DISABLE_TOKENS


def cache_persist_enabled() -> bool:
    """Whether verification cache should persist to disk between runs.

    Enabled by default. Set ``SPEC_CRITIC_VERIFICATION_CACHE_PERSIST=0`` to
    keep the cache in-memory only — useful for one-off runs and tests that
    don't want to touch the user's on-disk cache.
    """
    return not _env_flag_disabled("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST")


_DEFAULT_CACHE_TTL_DAYS = 60


def cache_ttl_days() -> int:
    """Age-based pruning in days. Default 60 days.

    The default is 60 days, balancing reuse
    against staleness for code references that may have new amendments
    or interpretations published quarterly. A cached "current edition is
    NFPA 13-2022" verdict older than two months may be wrong if the
    California Building Standards Commission adopted a newer edition in
    the interim — re-verification at that age catches the drift.

    Override via ``SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS``. Explicit
    ``0`` restores the legacy "no expiry" behavior for operators who
    want the cache to act as a permanent database. Malformed or
    negative values fall back to the 60-day default so a typo never
    accidentally invalidates the entire cache or disables expiry.
    """
    raw = os.environ.get("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS")
    if raw is None or not raw.strip():
        return _DEFAULT_CACHE_TTL_DAYS
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_CACHE_TTL_DAYS
    # ``0`` is an explicit operator override meaning "no expiry"; preserve
    # the legacy semantics. Negative values are nonsensical — fall back
    # to the default rather than silently disabling expiry.
    if value == 0:
        return 0
    if value < 0:
        return _DEFAULT_CACHE_TTL_DAYS
    return value


_DEFAULT_CACHE_MAX_ENTRIES = 5000


def cache_max_entries() -> int:
    """LRU entry cap for the cache. Default 5000 entries; ``0`` disables.

    Age-based pruning alone does not bound the file: a busy operator can
    accumulate tens of thousands of grounded verdicts inside the TTL window,
    every one of which is re-serialized on each save. The cap keeps the
    store (and the save) bounded by evicting the least-recently-used entries
    — recency is ``_CacheEntry.last_used_ts``, touched on every hit — when
    a put / save / load leaves the store over the cap.

    Override via ``SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES``. Explicit
    ``0`` disables the cap (unbounded, the pre-cap behavior). Malformed or
    negative values fall back to the default so a typo never silently
    disables the bound or evicts the whole cache.
    """
    raw = os.environ.get("SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES")
    if raw is None or not raw.strip():
        return _DEFAULT_CACHE_MAX_ENTRIES
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_CACHE_MAX_ENTRIES
    if value == 0:
        return 0
    if value < 0:
        return _DEFAULT_CACHE_MAX_ENTRIES
    return value


_DEFAULT_SINGLEFLIGHT_WAIT_SECONDS = 900.0


def singleflight_wait_seconds() -> float:
    """How long a single-flight follower waits on its leader. Default 900 s.

    A leader that dies without calling :meth:`VerificationSingleFlight.complete`
    (a crashed worker thread, a killed process sharing the coordinator) would
    otherwise park every follower of that generation forever. After the bound
    a follower proceeds with its own independent verification instead
    (``pipeline._verify_findings_singleflight`` logs the takeover).

    Override via ``SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS``
    (fractional seconds accepted). Explicit ``0`` waits forever (the legacy
    unbounded behavior). Malformed, negative, or non-finite values fall back
    to the default.
    """
    raw = os.environ.get("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS")
    if raw is None or not raw.strip():
        return _DEFAULT_SINGLEFLIGHT_WAIT_SECONDS
    try:
        value = float(raw.strip())
    except ValueError:
        return _DEFAULT_SINGLEFLIGHT_WAIT_SECONDS
    if value != value or value in (float("inf"), float("-inf")):
        return _DEFAULT_SINGLEFLIGHT_WAIT_SECONDS
    if value == 0:
        return 0.0
    if value < 0:
        return _DEFAULT_SINGLEFLIGHT_WAIT_SECONDS
    return value


def default_cache_path() -> Path:
    """Return the on-disk cache file path.

    Overridable via ``SPEC_CRITIC_CACHE_PATH``. The default is
    ``~/.spec_critic/verification_cache.json``. ``~`` and environment
    variables in the override are expanded so users can point at e.g.
    ``$XDG_CACHE_HOME/spec_critic/cache.json``.
    """
    override = os.environ.get("SPEC_CRITIC_CACHE_PATH")
    if override and override.strip():
        return Path(os.path.expandvars(os.path.expanduser(override.strip())))
    return Path.home() / ".spec_critic" / "verification_cache.json"


@dataclass
class _CacheEntry:
    """Stored verdict with sidecar metadata for future maintenance tools.

    ``last_used_ts`` is the LRU recency stamp — refreshed on every hit and
    persisted additively (a legacy row without it loads with ``created_ts``).
    ``created_ts`` stays the age reference for the TTL and the report's
    cache-age badge; a hit never rewrites it.
    """
    result: "VerificationResult"
    created_ts: float
    last_used_ts: float = 0.0

    def __post_init__(self) -> None:
        if not self.last_used_ts:
            self.last_used_ts = self.created_ts


@dataclass
class _VerificationFlightState:
    """One in-progress generation for a normalized verification question."""

    generation: int
    done: threading.Event = field(default_factory=threading.Event)
    # Leader-published payload for this generation's followers (see
    # :meth:`VerificationSingleFlight.share`). In-process only — it is the
    # channel for verdicts the entry store deliberately refuses (ungrounded
    # terminals), so it never goes near ``VerificationCache.put``.
    shared_payload: Any = None


@dataclass(frozen=True)
class VerificationFlightClaim:
    """Opaque ownership token returned by :class:`VerificationSingleFlight`.

    A leader must call :meth:`VerificationSingleFlight.complete` exactly once
    after it has either populated the cache or abandoned the attempt.  Followers
    wait on the token and then consult the cache; a cache miss means the result
    was deliberately non-cacheable (or the leader failed), so one follower may
    claim the next generation and take over.
    """

    key: str
    generation: int
    leader: bool
    _state: _VerificationFlightState = field(repr=False, compare=False)


class VerificationSingleFlight:
    """Coordinate one remote verification at a time per cache key.

    Claims for a group of keys are registered under one short-lived global
    lock, in sorted key order.  Callers never hold that lock while performing
    API work or waiting, so two modules that present the same keys in opposite
    orders cannot deadlock.  A completed generation is removed before its
    waiters wake.  If no grounded cache entry was produced, the next atomic
    claimant becomes leader for a fresh generation while every other waiter
    follows it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, _VerificationFlightState] = {}
        self._next_generation = 1

    def claim_many(
        self, keys: Iterable[str]
    ) -> dict[str, VerificationFlightClaim]:
        """Atomically claim distinct ``keys`` and return one token per key."""

        ordered = sorted(set(keys))
        claims: dict[str, VerificationFlightClaim] = {}
        with self._lock:
            created: list[tuple[str, _VerificationFlightState]] = []
            try:
                for key in ordered:
                    state = self._active.get(key)
                    leader = state is None
                    if state is None:
                        state = _VerificationFlightState(self._next_generation)
                        self._next_generation += 1
                        self._active[key] = state
                        created.append((key, state))
                    claims[key] = VerificationFlightClaim(
                        key=key,
                        generation=state.generation,
                        leader=leader,
                        _state=state,
                    )
            except BaseException:
                # Keep group acquisition atomic even under allocation/control-
                # flow failure: remove only generations created by this call and
                # wake any follower that managed to observe one defensively.
                for key, state in created:
                    if self._active.get(key) is state:
                        self._active.pop(key, None)
                    state.done.set()
                raise
        return claims

    @staticmethod
    def wait(
        claim: VerificationFlightClaim, timeout: float | None = None
    ) -> bool:
        """Wait for the generation represented by a follower ``claim``.

        Returns ``True`` once the leader completed, ``False`` if the bound
        elapsed first. ``timeout`` defaults to
        :func:`singleflight_wait_seconds` (``0`` / ``None`` from that seam
        means wait forever). A ``False`` return leaves the generation
        registered — the caller must not re-claim the key expecting to lead;
        it should proceed independently (see
        ``pipeline._verify_findings_singleflight``).
        """

        if claim.leader:
            raise ValueError("A single-flight leader cannot wait on itself")
        if timeout is None:
            timeout = singleflight_wait_seconds()
        if not timeout or timeout <= 0:
            claim._state.done.wait()
            return True
        return claim._state.done.wait(timeout)

    @staticmethod
    def share(claim: VerificationFlightClaim, payload: Any) -> None:
        """Publish a leader's non-cacheable terminal result to its followers.

        The cache's grounded invariant means an ungrounded verdict never
        enters the entry store, so without this channel every follower of a
        clean-UNVERIFIED leader would have to take over a fresh generation
        and pay for its own call. The payload is opaque to the coordinator
        (the pipeline decides what is shareable and who may inherit it) and
        lives only on this generation's state: it is visible to followers
        that waited on *this* claim and to nobody else, and it is never
        persisted. Must be called by the leader before :meth:`complete`.
        """

        if not claim.leader:
            raise ValueError("Only a single-flight leader can share a result")
        claim._state.shared_payload = payload

    @staticmethod
    def shared_payload(claim: VerificationFlightClaim) -> Any:
        """Return what the leader shared for a follower ``claim`` (or None).

        Meaningful only after :meth:`wait` returned for the claim.
        """

        return claim._state.shared_payload

    def complete(self, claim: VerificationFlightClaim) -> None:
        """Release a leader generation and wake all of its followers.

        Completion is idempotent and generation-safe: a stale leader can wake
        its own followers, but can never remove a newer takeover generation.
        """

        if not claim.leader:
            raise ValueError("Only a single-flight leader can complete a flight")
        with self._lock:
            if self._active.get(claim.key) is claim._state:
                self._active.pop(claim.key, None)
            claim._state.done.set()

    def active_count(self) -> int:
        """Return the number of in-progress keys (diagnostics/tests)."""

        with self._lock:
            return len(self._active)


@dataclass
class VerificationCache:
    """Thread-safe cache shared across a pipeline run.

    Per-run hits/misses are tracked in memory for diagnostics. Persistent
    metadata (creation timestamp per entry) is preserved across save/load
    so an external maintenance tool can prune by age or model version.
    """
    # Insertion order doubles as LRU order: a hit re-inserts the entry at the
    # end, so the least-recently-used entry is always the first key.
    _entries: dict[str, _CacheEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    hits: int = 0
    misses: int = 0
    loaded_from_disk: int = 0
    expired_on_load: int = 0
    # Rows the last load ignored one by one: ineligible (a legacy UNVERIFIED,
    # an uncited verdict, ...) or invalid (a bad timestamp, a non-finite or
    # malformed field). Counted apart from ``expired_on_load``.
    rejected_on_load: int = 0
    evicted: int = 0
    _singleflight: VerificationSingleFlight = field(
        default_factory=VerificationSingleFlight,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def singleflight(self) -> VerificationSingleFlight:
        """Run-local coordinator shared by every caller using this cache."""

        return self._singleflight

    def get(
        self,
        finding,
        *,
        cycle: CodeCycle,
        jurisdiction_fingerprint: str | None = None,
        basis_fingerprint: str | None = None,
    ) -> "VerificationResult | None":
        key = make_cache_key(
            finding,
            cycle=cycle,
            jurisdiction_fingerprint=jurisdiction_fingerprint,
            basis_fingerprint=basis_fingerprint,
        )
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                self.misses += 1
                return None
            if cache_ineligibility_reason(entry.result) is not None:
                # The read boundary of the one eligibility predicate. ``put``
                # and ``load_from_disk`` already refuse such an entry, so this
                # only fires if one reached the store some other way — and
                # then it is dropped, not replayed.
                self.misses += 1
                return None
            self.hits += 1
            # Touch: refresh the recency stamp and move to the LRU tail.
            entry.last_used_ts = time.time()
            self._entries[key] = entry
        return _clone_for_hit(entry)

    def put(
        self,
        finding,
        *,
        cycle: CodeCycle,
        result: "VerificationResult",
        jurisdiction_fingerprint: str | None = None,
        basis_fingerprint: str | None = None,
    ) -> None:
        # The write boundary of the one eligibility predicate. It refuses,
        # among others, every UNVERIFIED (grounded or not — the verifier's
        # uncertainty is not a reusable answer), operational failures and
        # budget shortfalls (transient: a re-run must try again), local
        # classifications, and a conclusive verdict without a substantive
        # citation or, for CONFIRMED / CORRECTED, a source quote. The
        # verifier already produces none of those as cacheable, so this is
        # the single place the rule is written down, not a second opinion.
        if cache_ineligibility_reason(result) is not None:
            return
        key = make_cache_key(
            finding,
            cycle=cycle,
            jurisdiction_fingerprint=jurisdiction_fingerprint,
            basis_fingerprint=basis_fingerprint,
        )
        with self._lock:
            stored = _clone_for_store(result)
            now = time.time()
            # Pop first so an overwrite lands at the LRU tail too.
            self._entries.pop(key, None)
            self._entries[key] = _CacheEntry(
                result=stored, created_ts=now, last_used_ts=now
            )
            self._evict_over_cap_locked()

    def _evict_over_cap_locked(self, cap: int | None = None) -> int:
        """Drop least-recently-used entries until the store fits the cap.

        Caller holds ``_lock``. Returns the number evicted (0 when the cap is
        disabled). Because :meth:`get` re-inserts a hit at the tail, the
        first key is always the least-recently-used entry.
        """
        if cap is None:
            cap = cache_max_entries()
        if cap <= 0:
            return 0
        evicted = 0
        while len(self._entries) > cap:
            oldest_key = next(iter(self._entries))
            del self._entries[oldest_key]
            evicted += 1
        self.evicted += evicted
        return evicted

    def stats(self) -> dict[str, int]:
        with self._lock:
            oldest_ts = min((e.created_ts for e in self._entries.values()), default=0.0)
            return {
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self._entries),
                "loaded_from_disk": self.loaded_from_disk,
                "expired_on_load": self.expired_on_load,
                "rejected_on_load": self.rejected_on_load,
                "evicted": self.evicted,
                "max_entries": cache_max_entries(),
                "oldest_entry_ts": int(oldest_ts) if oldest_ts else 0,
            }

    # ------------------------------------------------------------------
    # Disk persistence
    # ------------------------------------------------------------------

    def load_from_disk(self, path: str | Path | None = None) -> int:
        """Load entries from a JSON cache file.

        Returns the number of entries loaded. Silent on missing file —
        first-run users have no cache yet, and that is a normal state.
        Corrupt or schema-mismatched files are skipped with the in-memory
        cache left empty rather than crashing the run.

        Honors the optional TTL: entries older than
        ``SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`` are dropped on load.
        Surviving entries are installed in ``last_used_ts`` order (a legacy
        row without the field takes its ``created_ts``) so the in-memory LRU
        order reflects on-disk recency, and the LRU cap is applied once at
        the end (``evicted`` counts them; ``loaded_from_disk`` counts only
        the entries that survived).

        Every row is judged **on its own** (plan WP-10). A row is ignored —
        counted in ``rejected_on_load``, never replayed — when it fails the
        eligibility predicate every ``put`` applies
        (:func:`cache_ineligibility_reason`: a legacy UNVERIFIED, an uncited
        or quote-less verdict, ...) or when its data is invalid: a timestamp
        that is missing, not a number, not finite, not positive, or in the
        future; a count that is negative, fractional, or not finite; a
        field of the wrong type. Valid conclusive rows beside it still load,
        so no policy change needs a schema bump or a flush. (A single bad
        timestamp used to raise out of this method, and the pipeline then
        started with an empty cache — one hand-edited row discarded them
        all.)
        """
        target = Path(path) if path is not None else default_cache_path()
        if not target.exists():
            return 0
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        version = payload.get("version", 0)
        if isinstance(version, bool) or version != _CACHE_SCHEMA_VERSION:
            return 0
        raw_entries = payload.get("entries") or {}
        if not isinstance(raw_entries, dict):
            return 0

        now = time.time()
        ttl_days = cache_ttl_days()
        cutoff = now - (ttl_days * 86400) if ttl_days > 0 else 0.0
        loaded = 0
        expired = 0
        rejected = 0
        accepted: list[tuple[str, _CacheEntry]] = []

        with self._lock:
            for key, raw in raw_entries.items():
                if not isinstance(key, str) or not isinstance(raw, dict):
                    rejected += 1
                    continue
                created_ts = _record_timestamp(raw.get("created_ts"), now=now)
                if created_ts is None:
                    # Without a creation time a row cannot be aged, so the
                    # TTL could never retire it: invalid, not "fresh".
                    rejected += 1
                    continue
                if cutoff and created_ts < cutoff:
                    expired += 1
                    continue
                raw_last_used = raw.get("last_used_ts")
                if _is_absent_timestamp(raw_last_used):
                    # A legacy row that predates the LRU stamp.
                    last_used_ts = created_ts
                else:
                    last_used_ts = _record_timestamp(raw_last_used, now=now)
                    if last_used_ts is None:
                        rejected += 1
                        continue
                result_payload = raw.get("result")
                if not isinstance(result_payload, dict) or (
                    _persisted_payload_problem(result_payload) is not None
                ):
                    rejected += 1
                    continue
                try:
                    # Single deserialization path — same allow-list +
                    # coercion the in-memory clones use. Legacy entries that
                    # predate a telemetry field load it at its default
                    # (e.g. fetch / disagreement keys → 0 / False / []).
                    entry_result = _result_from_dict(result_payload, cache_status="miss")
                except Exception:
                    rejected += 1
                    continue
                # The load boundary of the one eligibility predicate: the
                # same rule ``put`` applies, re-checked per row, so a row
                # written by an older build (a grounded UNVERIFIED, an
                # uncited DISPUTED) or edited by hand is ignored here rather
                # than replayed for the TTL window.
                if cache_ineligibility_reason(entry_result) is not None:
                    rejected += 1
                    continue
                accepted.append(
                    (
                        key,
                        _CacheEntry(
                            result=entry_result,
                            created_ts=created_ts,
                            last_used_ts=last_used_ts,
                        ),
                    )
                )
            # Least-recently-used first, so the dict's insertion order is the
            # LRU order the eviction relies on. Stable sort keeps the file's
            # order for equal stamps (legacy rows all fall back to created_ts).
            accepted.sort(key=lambda kv: kv[1].last_used_ts)
            for key, entry in accepted:
                self._entries.pop(key, None)
                self._entries[key] = entry
            evicted = self._evict_over_cap_locked()
            loaded = len(accepted) - evicted
            self.loaded_from_disk = loaded
            self.expired_on_load = expired
            self.rejected_on_load = rejected
        return loaded

    def save_to_disk(self, path: str | Path | None = None) -> int:
        """Atomically write the cache to JSON.

        Returns the number of entries written. Atomic via temp-file +
        rename so a crash mid-write cannot corrupt an existing cache file.
        The LRU cap is applied before serializing, and the JSON is written
        compact (``separators=(",", ":")``, no indentation) — the file is
        machine-read only, and the whitespace was a large share of its bytes.
        """
        target = Path(path) if path is not None else default_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._evict_over_cap_locked()
            entries_payload = {
                key: {
                    "created_ts": entry.created_ts,
                    "last_used_ts": entry.last_used_ts,
                    "result": _result_to_dict(entry.result),
                }
                for key, entry in self._entries.items()
            }
            count = len(entries_payload)
        payload = {
            "version": _CACHE_SCHEMA_VERSION,
            "saved_at": time.time(),
            "entries": entries_payload,
        }
        # Atomic write: temp file in the same directory + rename.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".verification_cache.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, separators=(",", ":"))
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return count


# ---------------------------------------------------------------------------
# VerificationResult serialization policy
# ---------------------------------------------------------------------------
#
# The cache persists only a *subset* of VerificationResult's fields — the
# durable verdict semantics plus the evidence/telemetry needed to re-render a
# cached hit identically. ``_PERSISTED_*`` below is the single source of truth
# for that subset: ``_result_to_dict`` and ``_result_from_dict`` both drive
# off it, replacing the four hand-maintained field-by-field projections this
# module used to carry (to-dict, from-dict on load, clone-for-store,
# clone-for-hit). A field is split by JSON type only so the loader can coerce
# legacy / hand-edited entries defensively; the persisted *names* are the
# combined set.
#
# Every field NOT persisted is listed in ``_SKIPPED_FIELDS`` with the reason.
# ``test_verification_cache_serialization`` asserts that
# ``_PERSISTED_FIELDS | _SKIPPED_FIELDS`` covers every dataclass field, so
# adding a field to VerificationResult fails the test until it is explicitly
# classified here — the drift that this unification exists to prevent.

# ``verdict`` defaults to "UNVERIFIED"; every other string field defaults to "".
_PERSISTED_STR_FIELDS = (
    "verdict",
    "explanation",
    "model_used",
    "verification_profile",
    "verification_mode",
    "source_quote",
)
_PERSISTED_BOOL_FIELDS = ("grounded", "escalated", "models_disagreed")
_PERSISTED_INT_FIELDS = (
    "web_search_requests",
    "successful_source_count",
    "search_error_count",
    "web_fetch_requests",
)
_PERSISTED_STR_LIST_FIELDS = (
    "sources",
    "searched_sources",
    "cited_sources",
    "accepted_sources",
    "fetched_sources",
    "initial_sources",
)
# ``correction`` (str | None), ``rejected_sources`` (list[dict]) and
# ``rejected_source_reasons`` (dict[str, str]) need bespoke coercion, so they
# sit outside the typed tuples above.
_PERSISTED_FIELD_ORDER = (
    *_PERSISTED_STR_FIELDS,
    "correction",
    *_PERSISTED_BOOL_FIELDS,
    *_PERSISTED_INT_FIELDS,
    *_PERSISTED_STR_LIST_FIELDS,
    "rejected_sources",
    "rejected_source_reasons",
)
_PERSISTED_FIELDS = frozenset(_PERSISTED_FIELD_ORDER)

# Fields the cache deliberately does NOT persist, each with its reason. Kept
# as an explicit set (not an implicit omission) so the round-trip test can
# prove the union with _PERSISTED_FIELDS is exhaustive.
_SKIPPED_FIELDS = frozenset({
    # Replay state — stamped fresh on every store ("miss") / hit ("hit").
    "cache_status",
    "cache_entry_created_ts",
    # Raw in-memory payloads — diagnostics only, never persisted.
    "structured_payload",
    "retry_telemetry",
    # Transient signals the cache refuses to store (see ``put``): a re-run
    # must re-attempt these rather than replay a frozen shortfall.
    "verification_failed",
    "budget_exhausted",
    # Local-skip-only telemetry; local-skip results are never grounded, so
    # they never reach the cache in the first place.
    "requires_elevated_confidence",
    # Escalation history. A cache hit replays the final verdict and the
    # ``models_disagreed`` / ``initial_sources`` signal, but not
    # the full before/after escalation trace — preserved behavior from the
    # original projections.
    "escalation_attempted",
    "initial_model",
    "initial_verdict",
    "escalation_changed_verdict",
    "escalation_reason",
    # Operational token counts — diagnostics only, not persisted. The
    # per-call ``call_usage`` list (an escalated result's two conversations)
    # is the same spend telemetry in per-call form.
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    # Per-TTL cache-write split + its accounting status. Spend telemetry for
    # *this* run's calls, so a replayed verdict (which made no call) must not
    # inherit them — same reason the aggregates above are excluded. No cache
    # schema bump: these keys are never written, and a legacy row that lacks
    # them loads at the dataclass defaults.
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
    "cache_creation_breakdown_status",
    # One attempt record per paid conversation (plan WP-15) and the transport
    # the kept verdict's call ran on: spend telemetry for *this* run's calls.
    # A replayed verdict made no call, so it carries neither.
    "call_usage",
    "transport",
    # How the verification ended (the verifier's ``OUTCOME_*``). Runtime
    # classification, not verdict semantics: only conclusive verdicts are
    # ever stored, and a replay is identified by ``cache_status="hit"``, so
    # a hit carries the default ``""``. No schema bump — never written.
    "outcome",
})


def _is_absent_timestamp(value) -> bool:
    """A missing LRU stamp — a legacy row that predates ``last_used_ts``.

    ``None`` and a numeric zero read as absent (the writer's own default);
    anything else must pass :func:`_record_timestamp`.
    """
    if value is None:
        return True
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value == 0
    )


def _record_timestamp(value, *, now: float) -> float | None:
    """A persisted epoch timestamp, or ``None`` when it is not a usable one.

    Usable means a real number (never a bool or a string), finite, positive,
    and not more than :data:`_MAX_FUTURE_SKEW_SECONDS` ahead of ``now``. A
    NaN or infinite stamp used to load and then poison the TTL comparison,
    the LRU ordering, and the report's cache-age badge; a future one would
    never expire.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        ts = float(value)
    except OverflowError:
        return None
    if not math.isfinite(ts) or ts <= 0 or ts > now + _MAX_FUTURE_SKEW_SECONDS:
        return None
    return ts


def _persisted_payload_problem(payload: dict) -> str | None:
    """Why a persisted result dict is not valid data — ``None`` when it is.

    Load-only. The in-memory clone paths hand :func:`_result_from_dict` a
    dict built from a live result, so they need no check; a file, however,
    may have been hand-edited or written by another build. A missing key is
    fine (legacy rows load it at its default), but a present one must have
    its field's type, and a count must be a finite, non-negative whole
    number — ``int(float("nan"))`` would otherwise raise, and a negative or
    fractional count would load as a false one. This is what "treat invalid
    or non-finite data as an invalid record" means here.
    """
    for name in _PERSISTED_STR_FIELDS:
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            return f"{name} is not a string"
    for name in _PERSISTED_BOOL_FIELDS:
        value = payload.get(name)
        if value is not None and not isinstance(value, bool):
            return f"{name} is not a boolean"
    for name in _PERSISTED_INT_FIELDS:
        value = payload.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} is not a number"
        if isinstance(value, float) and (
            not math.isfinite(value) or not value.is_integer()
        ):
            return f"{name} is not a finite whole number"
        if value < 0:
            return f"{name} is negative"
    for name in _PERSISTED_STR_LIST_FIELDS:
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            return f"{name} is not a list of strings"
    correction = payload.get("correction")
    if correction is not None and not isinstance(correction, str):
        return "correction is not a string"
    return None


def _coerce_rejected(raw) -> list[dict]:
    out: list[dict] = []
    for r in raw or []:
        if isinstance(r, dict):
            out.append(
                {"url": str(r.get("url") or ""), "reason": str(r.get("reason") or "")}
            )
    return out


def _coerce_reasons(raw) -> dict[str, str]:
    """``{url: explanation}`` from a persisted row; legacy rows (no key) → ``{}``."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v}


def _result_to_dict(result: "VerificationResult") -> dict:
    """Project a VerificationResult onto its persisted-field dict.

    Uses :func:`dataclasses.asdict` (which deep-copies nested lists / dicts)
    and filters to the explicit ``_PERSISTED_FIELD_ORDER`` allow-list, so a
    newly added dataclass field is never silently written to disk — it has to
    be classified in ``_PERSISTED_*`` or ``_SKIPPED_FIELDS`` first.
    """
    full = asdict(result)
    return {name: full[name] for name in _PERSISTED_FIELD_ORDER}


def _result_from_dict(
    payload: dict,
    *,
    cache_status: str,
    cache_entry_created_ts: float = 0.0,
) -> "VerificationResult":
    """Rebuild a VerificationResult from a persisted-field dict.

    The inverse of :func:`_result_to_dict`, driven by the same allow-list.
    Coerces defensively so legacy / hand-edited cache files (missing keys,
    wrong JSON types) load to the field defaults rather than crashing. Skipped
    fields take their dataclass defaults; ``cache_status`` /
    ``cache_entry_created_ts`` are stamped by the caller (store vs. hit).
    """
    from .verifier import VerificationResult

    kwargs: dict = {}
    for name in _PERSISTED_STR_FIELDS:
        default = "UNVERIFIED" if name == "verdict" else ""
        kwargs[name] = str(payload.get(name) or default)
    for name in _PERSISTED_BOOL_FIELDS:
        kwargs[name] = bool(payload.get(name, False))
    for name in _PERSISTED_INT_FIELDS:
        kwargs[name] = int(payload.get(name, 0) or 0)
    for name in _PERSISTED_STR_LIST_FIELDS:
        # A blank entry carries nothing in any of these lists, and in the
        # evidence lists it would read as a citation; drop it on every path
        # (store, hit, load) so a replay never renders an empty source.
        kwargs[name] = [
            s for s in (payload.get(name) or []) if isinstance(s, str) and s.strip()
        ]
    kwargs["correction"] = (
        str(payload["correction"]) if payload.get("correction") is not None else None
    )
    kwargs["rejected_sources"] = _coerce_rejected(payload.get("rejected_sources"))
    kwargs["rejected_source_reasons"] = _coerce_reasons(
        payload.get("rejected_source_reasons")
    )
    return VerificationResult(
        cache_status=cache_status,
        cache_entry_created_ts=cache_entry_created_ts,
        **kwargs,
    )


def _clone_for_store(result: "VerificationResult") -> "VerificationResult":
    """In-memory store clone — round-trips through the persisted-field policy."""
    return _result_from_dict(_result_to_dict(result), cache_status=CACHE_STATUS_MISS)


def _clone_for_hit(entry: _CacheEntry) -> "VerificationResult":
    """Clone a stored result for a cache hit.

    Stamps ``cache_entry_created_ts`` from the
    sidecar ``_CacheEntry.created_ts`` so the report can render the cache-age
    badge ("Cache replay — Nd old") without re-reading the cache file. The
    age field lives on ``VerificationResult`` as runtime telemetry —
    distinct from the entry creation timestamp which remains the cache's
    source of truth.
    """
    return _result_from_dict(
        _result_to_dict(entry.result),
        cache_status=CACHE_STATUS_HIT,
        cache_entry_created_ts=entry.created_ts,
    )
