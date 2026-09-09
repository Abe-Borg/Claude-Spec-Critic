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

Only ``grounded=True`` results are stored, preserving the existing safety
guarantee that cached verdicts are always backed by external evidence.

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
    finding, *, cycle: CodeCycle, jurisdiction_fingerprint: str | None = None
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

    The **basis-policy namespace** (implementation plan section 5.11) is
    appended for a module whose verifier prompt no longer presents pinned
    editions as authoritative. Those verdicts answer a *different question*
    from the ones cached under the old wording — a verdict that disputed an
    adoption-deferring finding because the pin was authoritative must not
    replay now that it is not — so they need their own namespace. It is
    derived from the cycle here rather than threaded as a parameter
    deliberately: the key is already a pure function of the cycle, and a
    parameter would have to reach three call sites plus every ``get``/``put``
    caller, where one missed site silently replays a stale verdict.
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
    ) -> "VerificationResult | None":
        key = make_cache_key(
            finding, cycle=cycle, jurisdiction_fingerprint=jurisdiction_fingerprint
        )
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
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
    ) -> None:
        # Don't cache results that explicitly opted out of caching, or
        # results that came from an unsuccessful local skip path. We only
        # want to share *grounded* verdicts across findings.
        if not getattr(result, "grounded", False):
            return
        # Refuse to cache operational-failure
        # results. The ``verification_failed`` sentinel marks UNVERIFIED
        # results that came from a transient cause (rate limit, server
        # error, network failure, parse error, INVALID_REQUEST,
        # BATCH_CANCELED). Caching these would freeze the transient
        # error into a durable verdict and silently suppress
        # re-verification on later runs. The ``grounded`` guard above
        # already drops every UNVERIFIED, so in practice this branch is
        # defense-in-depth against a future call site that constructs a
        # grounded+failed result directly.
        if bool(getattr(result, "verification_failed", False)):
            return
        # Refuse to cache budget-exhausted
        # results. The ``budget_exhausted`` sentinel marks UNVERIFIED
        # outcomes where the verifier consumed its full mode-scaled
        # web_search budget without producing a grounded verdict.
        # Persisting these would freeze a transient evidence-shortfall
        # into a permanent UNVERIFIED — but the same finding might
        # ground on a re-run that allocates more budget (e.g. severity
        # was raised) or after the underlying source becomes
        # discoverable. Same defense-in-depth rationale as
        # ``verification_failed``: ``budget_exhausted=True`` implies
        # ``verdict=UNVERIFIED`` which the grounded guard above
        # already drops; this branch protects against a future call
        # site that constructs a grounded+exhausted result directly.
        if bool(getattr(result, "budget_exhausted", False)):
            return
        # Refuse to cache a CONFIRMED/CORRECTED/DISPUTED that lacks any
        # accepted external citation. The verifier's
        # ``_enforce_grounding_invariant`` would have downgraded such a
        # result to UNVERIFIED before reaching here; this is defense in
        # depth against a test or future call site that puts directly.
        verdict_upper = (getattr(result, "verdict", "") or "").strip().upper()
        if verdict_upper in _CITATION_GATED_VERDICTS and not (
            getattr(result, "accepted_sources", None) or getattr(result, "sources", None)
        ):
            return
        # Refuse to cache a CONFIRMED/CORRECTED without the verbatim
        # ``source_quote`` the v3 shape exists to carry. The verifier's
        # ``_demote_if_missing_source_quote`` downgrades such a result at
        # parse time; this closes the gap for any call site that puts
        # directly, so a hit can never render a grounded verdict with no
        # quote behind it.
        if verdict_upper in _QUOTE_GATED_VERDICTS and not (
            (getattr(result, "source_quote", "") or "").strip()
        ):
            return
        key = make_cache_key(
            finding, cycle=cycle, jurisdiction_fingerprint=jurisdiction_fingerprint
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
        if int(payload.get("version", 0) or 0) != _CACHE_SCHEMA_VERSION:
            return 0
        raw_entries = payload.get("entries") or {}
        if not isinstance(raw_entries, dict):
            return 0

        ttl_days = cache_ttl_days()
        cutoff = time.time() - (ttl_days * 86400) if ttl_days > 0 else 0.0
        loaded = 0
        expired = 0
        accepted: list[tuple[str, _CacheEntry]] = []

        with self._lock:
            for key, raw in raw_entries.items():
                if not isinstance(raw, dict):
                    continue
                created_ts = float(raw.get("created_ts") or 0.0)
                if cutoff and created_ts and created_ts < cutoff:
                    expired += 1
                    continue
                try:
                    last_used_ts = float(raw.get("last_used_ts") or 0.0)
                except (TypeError, ValueError):
                    last_used_ts = 0.0
                result_payload = raw.get("result")
                if not isinstance(result_payload, dict):
                    continue
                try:
                    # Single deserialization path — same allow-list +
                    # defensive coercion the in-memory clones use. Legacy
                    # entries that predate a telemetry field load it at its
                    # default (e.g. fetch / disagreement keys → 0 / False / []).
                    entry_result = _result_from_dict(result_payload, cache_status="miss")
                except Exception:
                    continue
                if not entry_result.grounded:
                    # Defensive: only grounded entries should ever be on
                    # disk, but reject any that slipped in.
                    continue
                # Belt-and-suspenders against an entry that somehow
                # shipped without an accepted citation — silently
                # reusing it would power a source-less CONFIRMED (or
                # DISPUTED — written by a pre-gate version of this app)
                # on a cache hit. Mirrors the invariant in
                # :func:`src.verifier._enforce_grounding_invariant`; this
                # re-check, not a schema bump, is what retires legacy
                # uncited DISPUTED rows.
                verdict_upper = (entry_result.verdict or "").strip().upper()
                if verdict_upper in _CITATION_GATED_VERDICTS and not (
                    entry_result.accepted_sources or entry_result.sources
                ):
                    continue
                # Same re-check for the source-quote invariant: a v4 row
                # hand-edited (or written by a pre-gate build) to hold a
                # quote-less CONFIRMED / CORRECTED is dropped here rather
                # than replayed for the TTL window.
                if verdict_upper in _QUOTE_GATED_VERDICTS and not (
                    (entry_result.source_quote or "").strip()
                ):
                    continue
                accepted.append(
                    (
                        key,
                        _CacheEntry(
                            result=entry_result,
                            created_ts=created_ts or time.time(),
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
    "call_usage",
})


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
        kwargs[name] = [str(s) for s in (payload.get(name) or []) if s]
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
