"""Evidence validation in observation mode (plan EX-04, part A).

The verifier's grounding gate establishes **retrieval**: a conclusive verdict
must cite a page the tools actually returned in its conversation
(:mod:`source_grounding`). Native citations add **attribution**: which
retrieved passage the verifier's words came from (:mod:`native_citations`).
Neither says whether the source **supports** the verdict — whether the passage
the verifier quoted states what the finding claims, for the edition the
finding names, from an authority that governs it, without an exception or a
negation the finding left out. This module asks those questions of every
conclusive verdict and records the answers beside it.

**Observation only.** An assessment never changes a verdict, its grounding,
its sources, its cache eligibility, its report status, or anything a later
stage reads. It is written to ``VerificationResult.evidence_assessment``
(runtime only, never cached), to the diagnostics event, and to the trace, so a
person can read where it disagrees with the verifier. Nothing enforces it:
promoting any part of it to a rule needs the adjudicated comparison in
``evals/evidence_validation.py`` first (plan EX-04). The switch is
``SPEC_CRITIC_EVIDENCE_VALIDATION=observe`` (``api_config``), off by default.

**What each check can say.** Every check reports one status relative to the
verdict:

* ``consistent`` — the feature agrees with the verdict;
* ``concern`` — the feature disagrees with it (a specific, nameable reason);
* ``unknown`` — the check could not tell;
* ``not_applicable`` — nothing for the check to look at.

The assessment is ``concerns`` when any check raises one, ``consistent`` when
none does and at least one **content** check (edition, numbers, negation,
exception) agrees, and ``insufficient`` otherwise. Source identity, authority,
and quoted support can raise a concern (or, for quoted support, say a passage
is on topic) but cannot make a verdict consistent on their own: where a
passage came from, and what it is about, is not what it says.

A concern is always a specific mismatch — a number the quote states
differently, a negation reversed, an exception the finding omits, an edition
the source names differently, a quote attributed to a source the verdict does
not cite, or evidence only from low-authority hosts. **Lexical similarity is
never one of them.** The overlap between the claim's words and the quote's is
reported as a feature (``features.lexical_overlap``) for a person to read,
and no status depends on it: a correct paraphrase shares few words with its
source, and treating low overlap as a concern would mark exactly the careful
verdicts as suspect.

**Direction.** CONFIRMED and CORRECTED assert that the source supports a
statement — the finding's claim, or for CORRECTED the verifier's correction —
so a mismatch between that statement and the quote is a concern. DISPUTED
asserts that the source *contradicts* the claim, so there the same mismatch is
consistent with the verdict and an exact agreement is only ``unknown`` (the
dispute may rest on applicability or authority, which a text comparison cannot
see). UNVERIFIED, a failure, and a local classification have no verdict to
validate: ``not_applicable``.

**Policy version.** Every assessment carries :data:`POLICY_VERSION`. A future
enforcing rule must bump it and give the verdicts it governs their own cache
namespace (as ``verification_cache.BASIS_POLICY_NAMESPACE`` does for the
edition-authority wording), so a verdict cached before the rule cannot replay
around it. Observation mode changes no key, which the tests pin.

Stdlib plus :mod:`source_grounding`, :mod:`native_citations`, and
:mod:`reference_parsing`.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from . import native_citations as _native
from .reference_parsing import designators, editions, section_numbers
from .source_grounding import normalize_url

_log = logging.getLogger(__name__)

#: The semantics of the checks below. Bump when a check's meaning changes.
POLICY_VERSION = "ev1"
MODE_OBSERVE = "observe"

STATUS_CONSISTENT = "consistent"
STATUS_CONCERN = "concern"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUSES = (STATUS_CONSISTENT, STATUS_CONCERN, STATUS_UNKNOWN, STATUS_NOT_APPLICABLE)

CHECK_SOURCE_IDENTITY = "source_identity"
CHECK_EDITION = "edition"
CHECK_AUTHORITY = "authority"
CHECK_QUOTED_SUPPORT = "quoted_support"
CHECK_NUMBERS = "numbers_units"
CHECK_NEGATION = "negation"
CHECK_EXCEPTION = "exception"
CHECKS = (
    CHECK_SOURCE_IDENTITY,
    CHECK_EDITION,
    CHECK_AUTHORITY,
    CHECK_QUOTED_SUPPORT,
    CHECK_NUMBERS,
    CHECK_NEGATION,
    CHECK_EXCEPTION,
)
#: The checks that compare what the evidence *says* with what the verdict
#: asserts. Only these can make an assessment consistent. Source identity and
#: authority say where a passage came from, and quoted support says whether it
#: is on the claim's topic (it carries the claim's section, reference, or
#: quantity) — none of which, alone, says the passage agrees with the verdict:
#: a passage about exactly the claimed clearance can still contradict it.
CONTENT_CHECKS = (
    CHECK_EDITION,
    CHECK_NUMBERS,
    CHECK_NEGATION,
    CHECK_EXCEPTION,
)

#: The overall reading of one assessment.
ASSESSMENT_CONSISTENT = "consistent"
ASSESSMENT_CONCERNS = "concerns"
ASSESSMENT_INSUFFICIENT = "insufficient"
ASSESSMENT_NOT_APPLICABLE = "not_applicable"
#: The validator itself raised; observation never breaks a run.
ASSESSMENT_ERROR = "error"
ASSESSMENTS = (
    ASSESSMENT_CONSISTENT,
    ASSESSMENT_CONCERNS,
    ASSESSMENT_INSUFFICIENT,
    ASSESSMENT_NOT_APPLICABLE,
    ASSESSMENT_ERROR,
)

DIRECTION_SUPPORT = "support"
DIRECTION_CONTRADICT = "contradict"

_SUPPORT_VERDICTS = ("CONFIRMED", "CORRECTED")
_CONCLUSIVE_VERDICTS = ("CONFIRMED", "CORRECTED", "DISPUTED")

#: A quote fragment or cited passage shorter than this is too short to say
#: which passage a quote came from; it is ignored for attribution. A property
#: of matching, not of support.
_MIN_ATTRIBUTION_CHARS = 20
_MAX_DETAIL_CHARS = 300

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\s*\.\.\.\s*\]")
_QUOTE_CHARS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-"})


def _collapse(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return _WS.sub(" ", text.translate(_QUOTE_CHARS)).strip().casefold()


def _bounded(text: str, limit: int = _MAX_DETAIL_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


_STOPWORDS = frozenset(
    """the and for with that this from shall must will should may can not are was
    were been being have has had into onto upon than then when where which while
    such each every other also only per any all its their there these those what
    who whom whose within without between under over above below more less most
    least same both either neither nor but yet section edition standard code
    required requires require provide provided spec specification states stated
    finding claim claims""".split()
)
_WORD = re.compile(r"[a-z][a-z0-9\-]{3,}")


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(_collapse(text)) if w not in _STOPWORDS}


def lexical_overlap(claim: str, quote: str) -> float:
    """Share of the claim's content words the quote also uses (0–1).

    A diagnostic feature only: no status in this module depends on it.
    """
    claim_words = _content_words(claim)
    if not claim_words:
        return 0.0
    return round(len(claim_words & _content_words(quote)) / len(claim_words), 3)


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

_NUMBER = (
    r"(?P<whole>\d+)[\s\-]+(?P<fn>\d+)/(?P<fd>\d+)"  # 1-1/2, 1 1/2
    r"|(?P<n>\d+)/(?P<d>\d+)"  # 1/2
    r"|(?P<dec>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
)

# (dimension, pattern, factor to the dimension's base unit). Order matters:
# longer, compound units first.
_UNITS: tuple[tuple[str, str, float], ...] = (
    ("density", r"gpm\s*/\s*(?:sq\.?\s*ft\.?|ft2|ft²|square\s+f(?:oo|ee)t)", 1.0),
    ("density", r"mm\s*/\s*min", 1.0 / 40.746),
    ("pressure_wg", r"(?:in\.|inch(?:es)?|\")\s*(?:w\.?\s*[gc]\.?|wg\b|wc\b|of\s+water|water\s+(?:gauge|column))", 1.0),
    ("air_flow", r"cfm\b", 1.0),
    ("flow", r"gpm\b|gal(?:lons)?\s*(?:/|per)\s*min(?:ute)?\b", 1.0),
    ("flow", r"l\s*/\s*min\b|lpm\b", 0.264172),
    ("flow", r"l\s*/\s*s\b", 15.8503),
    ("pressure", r"psig?\b", 1.0),
    ("pressure", r"kpa\b", 0.145038),
    ("pressure", r"bar\b", 14.5038),
    ("area", r"sq\.?\s*ft\.?|ft2|ft²|square\s+f(?:ee|oo)t", 1.0),
    ("area", r"m2\b|m²|square\s+met(?:er|re)s?", 10.7639),
    ("length", r"in\.|inch(?:es)?\b|\"", 1.0),
    ("length", r"f(?:ee|oo)t\b|ft\.?(?![a-z])", 12.0),
    ("length", r"mm\b|millimet(?:er|re)s?\b", 1.0 / 25.4),
    ("length", r"cm\b|centimet(?:er|re)s?\b", 1.0 / 2.54),
    ("length", r"m\b(?!\s*/)|met(?:er|re)s?\b", 39.3701),
    ("temperature_f", r"°\s*f\b|deg(?:rees)?\.?\s*f\b|degrees\s+fahrenheit", 1.0),
    ("temperature_c", r"°\s*c\b|deg(?:rees)?\.?\s*c\b|degrees\s+(?:celsius|centigrade)", 1.0),
    ("time", r"hours?\b|hrs?\b", 60.0),
    ("time", r"minutes?\b|mins?\b", 1.0),
    ("time", r"seconds?\b|secs?\b", 1.0 / 60.0),
    ("percent", r"%|percent\b", 1.0),
    ("voltage", r"kv\b", 1000.0),
    ("voltage", r"volts?\b|v\b", 1.0),
    ("current", r"amps?\b|amperes?\b", 1.0),
    ("power", r"kw\b", 1.0),
    ("mass", r"lbs?\.?(?![a-z])|pounds?\b", 1.0),
    ("mass", r"kg\b|kilograms?\b", 2.20462),
)
_UNIT_RE = re.compile(
    r"(?<![\w.])(?:" + _NUMBER + r")\s*-?\s*(?P<unit>"
    + "|".join(f"(?P<u{i}>{pattern})" for i, (_, pattern, _) in enumerate(_UNITS))
    + r")",
    re.IGNORECASE,
)


def _number_value(m: re.Match) -> float | None:
    try:
        if m.group("whole") is not None:
            return int(m.group("whole")) + int(m.group("fn")) / int(m.group("fd"))
        if m.group("n") is not None:
            return int(m.group("n")) / int(m.group("d"))
        return float(m.group("dec").replace(",", ""))
    except (ValueError, ZeroDivisionError):
        return None


def quantities(text: str | None) -> dict[str, list[float]]:
    """``{dimension: [values in the dimension's base unit]}`` found in ``text``.

    Base units: inches, psi, in. w.g., gpm, cfm, gpm/ft², ft², minutes, °F,
    percent, volts, amps, kW, pounds. Celsius is converted to °F.
    """
    out: dict[str, list[float]] = {}
    for m in _UNIT_RE.finditer((text or "").translate(_QUOTE_CHARS)):
        value = _number_value(m)
        if value is None:
            continue
        for i, (dimension, _, factor) in enumerate(_UNITS):
            if m.group(f"u{i}") is not None:
                if dimension == "temperature_c":
                    dimension, converted = "temperature_f", value * 9.0 / 5.0 + 32.0
                elif dimension == "temperature_f":
                    converted = value
                else:
                    converted = value * factor
                out.setdefault(dimension, []).append(converted)
                break
    return out


def _same_value(dimension: str, a: float, b: float) -> bool:
    """Equal within unit-conversion rounding (2%, or 1 °F)."""
    if dimension == "temperature_f":
        return abs(a - b) <= 1.0
    return abs(a - b) <= 0.02 * max(abs(a), abs(b)) + 1e-9


def _format_values(values: Iterable[float]) -> str:
    return ", ".join(f"{v:g}" for v in sorted(set(round(v, 3) for v in values)))


# ---------------------------------------------------------------------------
# Negation and exceptions
# ---------------------------------------------------------------------------

_COMPARATIVE = (
    r"less|more|greater|fewer|lower|higher|smaller|larger|exceed|exceeding|below|"
    r"above|under|over|later|earlier|shorter|longer|wider|narrower|to\s+exceed"
)
_NEGATED = re.compile(
    r"\b(?:shall|must|should|will|may|can|need)\s+not\b(?!\s+(?:be\s+)?(?:" + _COMPARATIVE + r")\b)"
    r"|\bcannot\b(?!\s+(?:be\s+)?(?:" + _COMPARATIVE + r")\b)"
    r"|\bnot\s+(?:be\s+)?(?:permitted|allowed|required|acceptable)\b"
    r"|\bprohibited\b|\bforbidden\b"
    r"|\bno\s+[a-z\-]+(?:\s+[a-z\-]+)?\s+shall\b",
    re.IGNORECASE,
)
_AFFIRMATIVE = re.compile(
    r"\b(?:shall|must)\b|\b(?:is|are)\s+required\b|\brequire[sd]?\b",
    re.IGNORECASE,
)
# Specifications state requirements in the imperative ("Provide ...",
# "Inspect ... quarterly"): a clause that opens with one of these verbs is an
# affirmative requirement, and one that opens "Do not" / "Never" a negated one.
_IMPERATIVE = re.compile(
    r"^\s*(?:provide|install|maintain|locate|space|inspect|test|comply|use|furnish|"
    r"route|protect|support|arrange|connect|label|submit|verify|design|size|mount|"
    r"seal|anchor|brace|enclose|equip|extend|pipe|replace|revise|specify)\b",
    re.IGNORECASE,
)
_NEGATIVE_IMPERATIVE = re.compile(
    r"^\s*(?:do\s+not|never)\b(?!\s+(?:" + _COMPARATIVE + r")\b)",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r";|\.(?=\s)|\n")
# Where a clause's exception begins. The negation check reads a clause only up
# to here: "shall be installed throughout, except that ... shall not be
# required ..." states an affirmative requirement and a negated exception, and
# the exception is the exception check's to judge.
_EXCEPTION_START = re.compile(r",?\s*\b(?:except(?:ion)?s?|unless)\b", re.IGNORECASE)

_EXCEPTION = re.compile(
    r"\bexcept(?:ion|ions|ed)?\b|\bunless\b|\bother\s+than\b|\bshall\s+not\s+apply\b"
    r"|\bnot\s+(?:be\s+)?required\s+(?:where|when|if|in)\b|\bpermitted\s+to\s+be\s+omitted\b"
    r"|\bneed\s+not\b|\bexempt(?:ed|ion|s)?\b",
    re.IGNORECASE,
)


def _clause_polarities(text: str) -> list[tuple[int, set[str]]]:
    """``[(polarity, content words)]`` for each clause stating a requirement.

    ``-1`` negated ("shall not be installed", "not permitted"), ``+1``
    affirmative ("shall", "is required", or an imperative such as "Provide").
    A comparative bound ("shall not be less than", "shall not exceed") is a
    limit, not a negation, and reads as affirmative. A clause is read only up
    to its exception ("except ...", "unless ..."), which the exception check
    judges.
    """
    out: list[tuple[int, set[str]]] = []
    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = _EXCEPTION_START.split(clause, maxsplit=1)[0]
        if _NEGATED.search(clause) or _NEGATIVE_IMPERATIVE.search(clause):
            out.append((-1, _content_words(clause)))
        elif _AFFIRMATIVE.search(clause) or _IMPERATIVE.search(clause):
            out.append((1, _content_words(clause)))
    return out


# ---------------------------------------------------------------------------
# Source authority
# ---------------------------------------------------------------------------

AUTHORITY_PRIMARY = "primary"
AUTHORITY_UNCLASSIFIED = "unclassified"
AUTHORITY_LOW = "low"

# Code publishers, standards developers, listing and approval bodies, and the
# insurer standard owners the modules name. A diagnostic table, not policy:
# the module's own source tiers remain what the verifier is told to prefer.
_PRIMARY_HOSTS = frozenset(
    {
        "nfpa.org", "iccsafe.org", "ashrae.org", "asce.org", "ul.com", "ulse.org",
        "astm.org", "asme.org", "ansi.org", "csagroup.org", "fmglobal.com",
        "fmapprovals.com", "iapmo.org", "smacna.org", "nema.org", "ieee.org",
        "iso.org", "nrc-cnrc.gc.ca", "canlii.org", "ecfr.gov", "osha.gov",
    }
)
_PRIMARY_SUFFIXES = (".gov", ".mil", ".gc.ca", ".gov.bc.ca", ".ontario.ca", ".gouv.qc.ca")
_STATE_US = re.compile(r"\.state\.[a-z]{2}\.us$")
_LOW_HOSTS = frozenset(
    {
        "wikipedia.org", "reddit.com", "quora.com", "eng-tips.com", "medium.com",
        "blogspot.com", "wordpress.com", "facebook.com", "linkedin.com",
        "youtube.com", "stackexchange.com", "answers.com", "tiktok.com", "x.com",
        "twitter.com",
    }
)


def _host(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    host = (urlsplit(normalized).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches_host(host: str, table: frozenset[str]) -> bool:
    return any(host == entry or host.endswith("." + entry) for entry in table)


def authority_class(url: str) -> str:
    """``primary`` / ``low`` / ``unclassified`` for one source URL."""
    host = _host(url)
    if not host:
        return AUTHORITY_UNCLASSIFIED
    if _matches_host(host, _PRIMARY_HOSTS) or host.endswith(_PRIMARY_SUFFIXES) or _STATE_US.search(host):
        return AUTHORITY_PRIMARY
    if _matches_host(host, _LOW_HOSTS):
        return AUTHORITY_LOW
    return AUTHORITY_UNCLASSIFIED


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _result(status: str, detail: str) -> dict:
    return {"status": status, "detail": _bounded(detail)}


def _quote_fragments(quote: str) -> list[str]:
    return [
        fragment
        for fragment in (_collapse(part) for part in _ELLIPSIS.split(quote or ""))
        if len(fragment) >= _MIN_ATTRIBUTION_CHARS
    ]


def _attributions(quote: str, records: list[dict]) -> list[dict]:
    """Native-citation records whose cited text holds (or is held by) the quote."""
    fragments = _quote_fragments(quote)
    whole = _collapse(quote)
    found: list[dict] = []
    for record in records:
        if not record.get("recognized", False):
            continue
        cited = _collapse(record.get("cited_text", ""))
        if len(cited) < _MIN_ATTRIBUTION_CHARS:
            continue
        if any(fragment in cited for fragment in fragments) or cited in whole:
            found.append(record)
    return found


def _check_source_identity(verdict: str, quote: str, accepted: set[str], records: list[dict] | None) -> dict:
    if not accepted:
        return _result(STATUS_CONCERN, "the verdict keeps no accepted source")
    if not quote.strip():
        if verdict in _SUPPORT_VERDICTS:
            return _result(STATUS_CONCERN, "a supporting verdict with no source quote")
        return _result(STATUS_UNKNOWN, "no quote to attribute")
    if records is None:
        return _result(STATUS_UNKNOWN, "native citations were not captured for this result")
    attributed = _attributions(quote, records)
    if not attributed:
        return _result(
            STATUS_UNKNOWN,
            "the quote is not among the passages the API cited; it may come from "
            "elsewhere on a cited page",
        )
    if any(normalize_url(r.get("url", "")) in accepted for r in attributed):
        return _result(STATUS_CONSISTENT, "the quote is a passage the API cited from an accepted source")
    urls = sorted({r.get("url", "") or "(unresolved source)" for r in attributed})
    return _result(
        STATUS_CONCERN,
        "the quoted passage is attributed to a source the verdict does not cite: " + ", ".join(urls),
    )


def _check_edition(direction: str, target: str, evidence: str) -> dict:
    claimed = editions(target, loose=True)
    if not claimed:
        return _result(STATUS_NOT_APPLICABLE, "the claim names no edition")
    stated = editions(evidence)
    shared = [name for name in claimed if name in stated]
    if not shared:
        return _result(STATUS_UNKNOWN, "the evidence names no edition of " + ", ".join(sorted(claimed)))
    differing = [name for name in shared if not (claimed[name] & stated[name])]
    if differing:
        detail = "; ".join(
            f"{name}: claim {_format_values(claimed[name])}, source {_format_values(stated[name])}"
            for name in differing
        )
        if direction == DIRECTION_SUPPORT:
            return _result(STATUS_CONCERN, "the source names a different edition — " + detail)
        return _result(STATUS_CONSISTENT, "the source names a different edition — " + detail)
    if direction == DIRECTION_SUPPORT:
        return _result(STATUS_CONSISTENT, "the source names the edition the claim names")
    return _result(STATUS_UNKNOWN, "the source names the same edition the disputed claim names")


def _check_authority(accepted_urls: list[str], code_claim: bool) -> tuple[dict, list[str]]:
    classes = [authority_class(url) for url in accepted_urls]
    if not classes:
        return _result(STATUS_CONCERN, "no accepted source"), classes
    if AUTHORITY_PRIMARY in classes:
        return _result(STATUS_CONSISTENT, "at least one accepted source is a code, standards, or government publisher"), classes
    if all(c == AUTHORITY_LOW for c in classes):
        return _result(STATUS_CONCERN, "every accepted source is a forum, wiki, social, or blog host"), classes
    if code_claim:
        return _result(STATUS_UNKNOWN, "no accepted source is a recognized publisher of the cited code or standard"), classes
    return _result(STATUS_UNKNOWN, "the accepted sources are not classified"), classes


def _check_quoted_support(target: str, quote: str) -> tuple[dict, list[str]]:
    if not quote.strip():
        return _result(STATUS_UNKNOWN, "no quote"), []
    folded = _collapse(quote)
    anchors: list[str] = []
    for section in section_numbers(target):
        if section in folded:
            anchors.append(f"§{section}")
    for name in designators(target):
        if _collapse(name) in folded:
            anchors.append(name)
    claim_q = quantities(target)
    quote_q = quantities(quote)
    for dimension, values in claim_q.items():
        for v in values:
            if any(_same_value(dimension, v, q) for q in quote_q.get(dimension, [])):
                anchors.append(f"{dimension}={v:g}")
                break
    if anchors:
        return _result(STATUS_CONSISTENT, "the quote carries the claim's " + ", ".join(anchors)), anchors
    # Never a concern: a faithful paraphrase can share none of the claim's
    # anchors, and a missing anchor is not a mismatch.
    return _result(STATUS_UNKNOWN, "the quote carries none of the claim's section numbers, references, or quantities"), anchors


def _check_numbers(direction: str, target: str, quote: str) -> dict:
    claim_q = quantities(target)
    if not claim_q:
        return _result(STATUS_NOT_APPLICABLE, "the claim states no quantity with a unit")
    quote_q = quantities(quote)
    shared = [d for d in claim_q if d in quote_q]
    if not shared:
        return _result(STATUS_UNKNOWN, "the quote states no quantity of the claim's kinds")
    disjoint = [
        d for d in shared
        if not any(_same_value(d, a, b) for a in claim_q[d] for b in quote_q[d])
    ]
    if disjoint:
        detail = "; ".join(
            f"{d}: claim {_format_values(claim_q[d])}, quote {_format_values(quote_q[d])}"
            for d in disjoint
        )
        if direction == DIRECTION_SUPPORT:
            return _result(STATUS_CONCERN, "the quote states a different quantity — " + detail)
        return _result(STATUS_CONSISTENT, "the quote states a different quantity — " + detail)
    if direction == DIRECTION_SUPPORT:
        return _result(STATUS_CONSISTENT, "the quote states the claim's quantity")
    return _result(STATUS_UNKNOWN, "the quote states the same quantity the disputed claim states")


def _check_negation(direction: str, normative: str | list[str], quote: str) -> dict:
    # The first candidate that states a requirement is the claim's normative
    # statement: the correction, else the proposed text, else the issue.
    candidates = [normative] if isinstance(normative, str) else list(normative)
    claim_clauses: list[tuple[int, set[str]]] = []
    for candidate in candidates:
        claim_clauses = _clause_polarities(candidate)
        if claim_clauses:
            break
    quote_clauses = _clause_polarities(quote)
    if not claim_clauses or not quote_clauses:
        return _result(STATUS_NOT_APPLICABLE, "no requirement clause on one side")
    reversed_on: set[str] = set()
    agreed_on: set[str] = set()
    for c_pol, c_words in claim_clauses:
        for q_pol, q_words in quote_clauses:
            # A topic guard, not an acceptance threshold: two clauses are
            # compared only when they share a content word, so a negation
            # about something else in the passage is not read as reversing
            # the claim.
            shared = c_words & q_words
            if not shared:
                continue
            (reversed_on if c_pol != q_pol else agreed_on).update(shared)
    if reversed_on:
        words = ", ".join(sorted(reversed_on)[:6])
        if direction == DIRECTION_SUPPORT:
            return _result(STATUS_CONCERN, f"the quote negates what the claim requires (on: {words})")
        return _result(STATUS_CONSISTENT, f"the quote negates what the disputed claim requires (on: {words})")
    if agreed_on:
        if direction == DIRECTION_SUPPORT:
            return _result(STATUS_CONSISTENT, "the quote and the claim state the requirement the same way")
        return _result(STATUS_UNKNOWN, "the quote states the requirement the way the disputed claim does")
    return _result(STATUS_NOT_APPLICABLE, "the requirement clauses share no subject")


def _check_exception(direction: str, claim_text: str, quote: str) -> dict:
    if not _EXCEPTION.search(quote or ""):
        return _result(STATUS_NOT_APPLICABLE, "the quote states no exception")
    if _EXCEPTION.search(claim_text or ""):
        if direction == DIRECTION_SUPPORT:
            return _result(STATUS_CONSISTENT, "the claim accounts for an exception, as the quote does")
        return _result(STATUS_UNKNOWN, "both the claim and the quote mention an exception")
    if direction == DIRECTION_SUPPORT:
        return _result(STATUS_CONCERN, "the quote states an exception the finding does not mention")
    return _result(STATUS_CONSISTENT, "the quote states an exception the disputed claim does not mention")


# ---------------------------------------------------------------------------
# One assessment
# ---------------------------------------------------------------------------


def _field(obj: Any, name: str) -> str:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else ""


def _header(finding: Any, result: Any, verdict: str) -> dict:
    return {
        "policy_version": POLICY_VERSION,
        "mode": MODE_OBSERVE,
        "finding_id": _field(finding, "finding_id"),
        "file": _field(finding, "fileName"),
        "verdict": verdict,
        "provenance": _native.provenance(result),
    }


def assess_evidence(finding: Any, result: Any) -> dict:
    """The observation-mode assessment of ``result``'s evidence for ``finding``.

    Pure: reads the finding and the result, changes neither, and never
    raises for ordinary input (:func:`annotate_evidence_assessments` catches
    the rest). Returns a JSON-safe dict (see the module docstring).
    """
    verdict = (_field(result, "verdict") or "").strip().upper()
    record = _header(finding, result, verdict)
    local = (
        (_field(result, "cache_status") or "").strip() == "local_skip"
        or (_field(result, "verification_mode") or "").strip() == "local_skip"
    )
    if (
        local
        or bool(getattr(result, "verification_failed", False))
        or verdict not in _CONCLUSIVE_VERDICTS
    ):
        record.update(
            direction="",
            target="",
            assessment=ASSESSMENT_NOT_APPLICABLE,
            agrees_with_verdict=None,
            concerns=[],
            checks={},
            features={},
        )
        return record

    issue = _field(finding, "issue")
    replacement = _field(finding, "replacementText")
    code_reference = _field(finding, "codeReference")
    correction = _field(result, "correction")
    claim_text = "\n".join(t for t in (code_reference, issue, replacement) if t)
    if verdict == "CORRECTED" and correction.strip():
        target_kind, target = "correction", "\n".join(t for t in (code_reference, correction) if t)
        normative = [correction]
    else:
        target_kind, target = "claim", claim_text
        normative = [replacement, issue]
    direction = DIRECTION_SUPPORT if verdict in _SUPPORT_VERDICTS else DIRECTION_CONTRADICT

    quote = _field(result, "source_quote")
    accepted_urls = [
        u for u in (getattr(result, "accepted_sources", None) or getattr(result, "sources", None) or [])
        if isinstance(u, str) and normalize_url(u)
    ]
    accepted = {normalize_url(u) for u in accepted_urls}
    records = getattr(result, "native_citations", None)
    records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else None
    # What a source says about its own edition: the quote, and the passages
    # and titles the API cited from the verdict's accepted sources.
    accepted_records = [
        r for r in (records or []) if normalize_url(r.get("url", "")) in accepted
    ]
    evidence_text = "\n".join(
        [quote]
        + [r.get("cited_text", "") or "" for r in accepted_records]
        + [r.get("title", "") or "" for r in accepted_records]
    )
    code_claim = bool(code_reference.strip()) or bool(designators(target))

    authority, classes = _check_authority(accepted_urls, code_claim)
    quoted, anchors = _check_quoted_support(target, quote)
    checks = {
        CHECK_SOURCE_IDENTITY: _check_source_identity(verdict, quote, accepted, records),
        CHECK_EDITION: _check_edition(direction, target, evidence_text),
        CHECK_AUTHORITY: authority,
        CHECK_QUOTED_SUPPORT: quoted,
        CHECK_NUMBERS: _check_numbers(direction, target, quote),
        CHECK_NEGATION: _check_negation(direction, normative, quote),
        CHECK_EXCEPTION: _check_exception(direction, claim_text + "\n" + correction, quote),
    }
    concerns = [name for name in CHECKS if checks[name]["status"] == STATUS_CONCERN]
    consistent = [name for name in CONTENT_CHECKS if checks[name]["status"] == STATUS_CONSISTENT]
    if concerns:
        assessment, agrees = ASSESSMENT_CONCERNS, False
    elif consistent:
        assessment, agrees = ASSESSMENT_CONSISTENT, True
    else:
        assessment, agrees = ASSESSMENT_INSUFFICIENT, None
    record.update(
        direction=direction,
        target=target_kind,
        assessment=assessment,
        agrees_with_verdict=agrees,
        concerns=concerns,
        checks=checks,
        features={
            "lexical_overlap": lexical_overlap(target, quote),
            "quote_chars": len(quote),
            "anchors": anchors,
            "native_citation_status": _native.capture_status(result),
            "native_citations": len(records or []),
            "attributed": bool(quote.strip() and records and _attributions(quote, records)),
            "authority_classes": classes,
        },
    )
    return record


def annotate_evidence_assessments(findings: Iterable[Any], *, log=None) -> int:
    """Stamp an assessment on every verified finding; return how many.

    Observation mode's one entry point (``pipeline.verify_findings_for_run``
    calls it after each verification round when the switch is on). Each
    result gets its own dict; nothing else on it changes. A validator error on
    one finding is recorded on that finding (``assessment: "error"``) and
    logged, never raised: observation must not be able to break a run.
    """
    findings = list(findings or [])
    count = 0
    for finding in findings:
        result = getattr(finding, "verification", None)
        if result is None:
            continue
        try:
            assessment = assess_evidence(finding, result)
        except Exception as exc:  # noqa: BLE001 — observation never breaks a run
            _log.warning("Evidence validation failed for %s: %s", _field(finding, "finding_id"), exc)
            assessment = {
                **_header(finding, result, (_field(result, "verdict") or "").strip().upper()),
                "assessment": ASSESSMENT_ERROR,
                "agrees_with_verdict": None,
                "concerns": [],
                "checks": {},
                "features": {},
                "error": _bounded(f"{type(exc).__name__}: {exc}"),
            }
        try:
            result.evidence_assessment = assessment
        except Exception:  # noqa: BLE001 — a frozen test double
            continue
        count += 1
    if log is not None and count:
        disagreements = sum(
            1 for f in findings
            if isinstance(getattr(getattr(f, "verification", None), "evidence_assessment", None), dict)
            and f.verification.evidence_assessment.get("agrees_with_verdict") is False
        )
        log(
            f"Evidence validation (observation only): {count} assessed, "
            f"{disagreements} disagree with their verdict.",
            level="info",
        )
    return count


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

#: At most this many disagreeing findings are listed in a run's rollup; the
#: counts above the list are always complete.
MAX_LISTED_DISAGREEMENTS = 50


def compact_assessment(assessment: Any) -> dict | None:
    """The assessment as a diagnostics event carries it (bounded, JSON-safe).

    Every check's status, the detail of each concern (what a person needs to
    judge the disagreement), and the lexical-overlap feature.
    """
    if not isinstance(assessment, dict):
        return None
    checks = assessment.get("checks") or {}
    return {
        "policy_version": assessment.get("policy_version", ""),
        "finding_id": assessment.get("finding_id", ""),
        "file": assessment.get("file", ""),
        "verdict": assessment.get("verdict", ""),
        "direction": assessment.get("direction", ""),
        "assessment": assessment.get("assessment", ""),
        "agrees_with_verdict": assessment.get("agrees_with_verdict"),
        "concerns": list(assessment.get("concerns") or []),
        "statuses": {
            name: (checks.get(name) or {}).get("status", "")
            for name in CHECKS
            if name in checks
        },
        "concern_details": {
            name: (checks.get(name) or {}).get("detail", "")
            for name in assessment.get("concerns") or []
        },
        "lexical_overlap": (assessment.get("features") or {}).get("lexical_overlap"),
        "provenance": assessment.get("provenance", ""),
        **({"error": assessment["error"]} if assessment.get("error") else {}),
    }


def summarize_assessments(records: Iterable[Any]) -> dict | None:
    """A run's rollup of compact assessments, or ``None`` when there are none."""
    records = [r for r in records or [] if isinstance(r, dict)]
    if not records:
        return None
    by_assessment: dict[str, int] = {}
    by_concern: dict[str, int] = {}
    by_provenance: dict[str, int] = {}
    by_verdict: dict[str, dict[str, int]] = {}
    listed: list[dict] = []
    disagreements = 0
    for record in records:
        kind = str(record.get("assessment") or "")
        by_assessment[kind] = by_assessment.get(kind, 0) + 1
        if kind == ASSESSMENT_NOT_APPLICABLE:
            continue
        verdict = str(record.get("verdict") or "")
        bucket = by_verdict.setdefault(verdict, {"assessed": 0, "disagreements": 0})
        bucket["assessed"] += 1
        provenance = str(record.get("provenance") or "")
        by_provenance[provenance] = by_provenance.get(provenance, 0) + 1
        if record.get("agrees_with_verdict") is False:
            disagreements += 1
            bucket["disagreements"] += 1
            for name in record.get("concerns") or []:
                by_concern[name] = by_concern.get(name, 0) + 1
            if len(listed) < MAX_LISTED_DISAGREEMENTS:
                listed.append(
                    {
                        "finding_id": record.get("finding_id", ""),
                        "file": record.get("file", ""),
                        "verdict": verdict,
                        "concerns": list(record.get("concerns") or []),
                        "concern_details": dict(record.get("concern_details") or {}),
                        "provenance": provenance,
                    }
                )
    assessed = sum(v for k, v in by_assessment.items() if k != ASSESSMENT_NOT_APPLICABLE)
    return {
        "policy_version": POLICY_VERSION,
        "mode": MODE_OBSERVE,
        "assessed": assessed,
        "not_applicable": by_assessment.get(ASSESSMENT_NOT_APPLICABLE, 0),
        "by_assessment": by_assessment,
        "disagreements": disagreements,
        "by_concern": by_concern,
        "by_verdict": by_verdict,
        "by_provenance": by_provenance,
        "disagreement_findings": listed,
        "disagreements_not_listed": max(0, disagreements - len(listed)),
    }


def summary_line(rollup: dict | None) -> str:
    """One line for the diagnostics text export ("" when there is no rollup)."""
    if not rollup:
        return ""
    concerns = ", ".join(
        f"{name} {count}" for name, count in sorted((rollup.get("by_concern") or {}).items())
    )
    line = (
        f"Evidence check (observation only, {rollup.get('policy_version', '')}): "
        f"{rollup.get('assessed', 0)} verdict(s) assessed, "
        f"{rollup.get('disagreements', 0)} disagree with the verifier"
    )
    return line + (f" (concerns: {concerns})" if concerns else "")
