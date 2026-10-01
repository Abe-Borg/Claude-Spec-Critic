"""EX-03: model selection, effort, and confidence — arms, isolation, runner, scorer.

The decision record is ``plans/experiments/EX-03-model-effort-confidence.md``;
the dataset is :mod:`evals.model_effort_dataset`.

Three experiments, each **one change** against the same baseline (the app's
defaults with every experimental switch off):

* ``escalation_model`` — the verification escalation tier on Opus 4.8
  instead of Opus 5.5 (``SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL``). Scored
  on the verification cases.
* ``review_effort`` — the per-spec review at effort ``xhigh`` instead of its
  default (``medium`` on the Opus 5.5 review model; ``high`` on Opus 5 when
  this experiment was written) (``SPEC_CRITIC_REVIEW_EFFORT``). Scored on the
  review cases.
* ``review_scope_wording`` — ``<review_scope>``'s certainty gate replaced by a
  grounding rule that agrees with the confidence rubric
  (``SPEC_CRITIC_REVIEW_SCOPE_WORDING=coverage_first``). Scored on the review
  cases.

**Isolation.** Each arm runs in a process of its own, whose environment
:func:`arm_environment` builds from scratch: every ``SPEC_CRITIC_*`` variable
the operator's shell carries is dropped (so an unrelated experiment or model
override cannot ride along), the arm's one setting is added, and every path
the app writes — the verification cache, the pending-batch record, traces,
the log, the updater's state — points into the arm's own state directory,
which must start empty. A separate process matters for more than tidiness:
the model defaults are read when ``src.core.api_config`` is imported, so an
arm cannot be switched inside a running process, and the verification cache
is keyed without the model (``verification_cache.make_cache_key``), so one
cache shared by two arms would replay the baseline's verdicts in the
candidate. Inside an arm, :func:`run_arm` gives each verification view a
fresh in-memory cache that never loads from disk, and records its hit count,
which must be zero (every case has its own cache key; the dataset validates
that).

**This module sends nothing unless asked to.** ``describe``, ``probe``,
``validate``, and ``score`` are offline. ``run`` and ``run-experiment`` spend
money: they require ``--live``, a spending cap, and an API key that is not the
test sentinel, and the decision record says a live run has not been
authorized.

Offline use::

    python -m evals.model_effort [describe]
    python -m evals.model_effort validate
    python -m evals.model_effort probe          # the requests this process would send
    python -m evals.model_effort score --out DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import model_effort_dataset as ds

# --------------------------------------------------------------------------
# Arms and experiments
# --------------------------------------------------------------------------

EXPERIMENT_ESCALATION = "escalation_model"
EXPERIMENT_REVIEW_EFFORT = "review_effort"
EXPERIMENT_REVIEW_SCOPE = "review_scope_wording"

ENV_ESCALATION_MODEL = "SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL"
ENV_REVIEW_EFFORT = "SPEC_CRITIC_REVIEW_EFFORT"
ENV_REVIEW_SCOPE_WORDING = "SPEC_CRITIC_REVIEW_SCOPE_WORDING"

BASELINE_ARM = "baseline"


@dataclass(frozen=True)
class Arm:
    arm_id: str
    #: The one environment setting this arm adds, or ``None`` for the baseline.
    setting: tuple[str, str] | None
    rationale: str
    #: The probe fields (see :func:`request_probe`) this arm must change, and
    #: no others. ``tests/test_model_effort_experiment.py`` builds each arm's
    #: requests in its own process and checks the difference is exactly this.
    expected_request_changes: tuple[str, ...] = ()


ARMS: dict[str, Arm] = {
    BASELINE_ARM: Arm(
        arm_id=BASELINE_ARM,
        setting=None,
        rationale="The app's defaults, every experimental switch off.",
    ),
    "escalation_opus_4_8": Arm(
        arm_id="escalation_opus_4_8",
        setting=(ENV_ESCALATION_MODEL, "claude-opus-4-8"),
        rationale=(
            "The one whitelisted alternative that changes only the escalation model's "
            "request: same effort (both are Opus, so both are held to the medium Opus "
            "ceiling), and escalation still fires because it differs from the Sonnet 5.5 "
            "initial verifier. It differs from Opus 5.5 in one capability the deepest "
            "pass could use: web fetch, which is gated off on Opus 5.5 (as on Opus 5). It "
            "costs 25% more per token ($5/$25 against $4/$20), which the cost rule "
            "weighs. A Sonnet escalation is not one change today (see the decision "
            "record)."
        ),
        expected_request_changes=(
            "escalation.model",
            "escalation.tools",
            "deep_initial.model",
            "deep_initial.tools",
        ),
    ),
    "review_effort_xhigh": Arm(
        arm_id="review_effort_xhigh",
        setting=(ENV_REVIEW_EFFORT, "xhigh"),
        rationale=(
            "The review ran at xhigh until it was lowered to high as a token-spend "
            "measure, then to medium with the move to Opus 5.5 (its API default; "
            "Anthropic reports Opus 5.5 at medium above Opus 5 at high), each time "
            "with no recall measurement on this workload. The question is whether "
            "xhigh's extra spend buys severe defects back."
        ),
        expected_request_changes=("review.effort",),
    ),
    "review_scope_coverage_first": Arm(
        arm_id="review_scope_coverage_first",
        setting=(ENV_REVIEW_SCOPE_WORDING, "coverage_first"),
        rationale=(
            "<review_scope> still says 'Only report a finding if you have concrete "
            "evidence from the spec text that a genuine problem exists', a certainty "
            "bar the coverage-first rubric says not to apply. The arm replaces that "
            "one sentence with a grounding rule."
        ),
        expected_request_changes=("review.system_prompt_sha256",),
    ),
}


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    stage: str
    candidate: str
    question: str
    baseline: str = BASELINE_ARM


EXPERIMENTS: dict[str, Experiment] = {
    EXPERIMENT_ESCALATION: Experiment(
        experiment_id=EXPERIMENT_ESCALATION,
        stage=ds.STAGE_VERIFICATION,
        candidate="escalation_opus_4_8",
        question=(
            "Does an escalation tier that can read full pages (Opus 4.8, web fetch) "
            "reach better-grounded verdicts on hard findings than Opus 5.5, at a cost "
            "the owner accepts?"
        ),
    ),
    EXPERIMENT_REVIEW_EFFORT: Experiment(
        experiment_id=EXPERIMENT_REVIEW_EFFORT,
        stage=ds.STAGE_REVIEW,
        candidate="review_effort_xhigh",
        question=(
            "Does review effort xhigh find severe defects that the default (medium) "
            "misses, and at what cost?"
        ),
    ),
    EXPERIMENT_REVIEW_SCOPE: Experiment(
        experiment_id=EXPERIMENT_REVIEW_SCOPE,
        stage=ds.STAGE_REVIEW,
        candidate="review_scope_coverage_first",
        question=(
            "Does replacing <review_scope>'s certainty gate with a grounding rule raise "
            "recall without adding unsupported findings?"
        ),
    ),
}


def one_change_problems(
    arms: Mapping[str, Arm] = ARMS, experiments: Mapping[str, Experiment] = EXPERIMENTS
) -> list[str]:
    """Problems with the arm and experiment registries; empty means valid."""
    problems: list[str] = []
    baseline = arms.get(BASELINE_ARM)
    if baseline is None or baseline.setting is not None:
        problems.append("the baseline arm must exist and set nothing")
    for arm in arms.values():
        if arm.arm_id == BASELINE_ARM:
            continue
        if arm.setting is None:
            problems.append(f"{arm.arm_id}: a candidate arm must set exactly one variable")
            continue
        name, value = arm.setting
        if not name.startswith("SPEC_CRITIC_") or not value:
            problems.append(f"{arm.arm_id}: its setting must be one SPEC_CRITIC_ variable with a value")
        if name in ARM_STATE_ENV or name in FIXED_ENV:
            problems.append(f"{arm.arm_id}: {name} is a state path, not an experimental setting")
        if not arm.expected_request_changes:
            problems.append(f"{arm.arm_id}: names no request field it changes")
    used = [e.candidate for e in experiments.values()]
    for exp in experiments.values():
        if exp.candidate not in arms or exp.baseline not in arms:
            problems.append(f"{exp.experiment_id}: unknown arm")
        if exp.baseline != BASELINE_ARM:
            problems.append(f"{exp.experiment_id}: every experiment compares against the one baseline")
    for arm_id in arms:
        if arm_id != BASELINE_ARM and used.count(arm_id) != 1:
            problems.append(f"{arm_id}: must be the candidate of exactly one experiment")
    settings = [a.setting[0] for a in arms.values() if a.setting]
    if len(settings) != len(set(settings)):
        problems.append("two arms set the same variable")
    return problems


# --------------------------------------------------------------------------
# Environment and state isolation
# --------------------------------------------------------------------------

#: Every path the app writes, pointed into the arm's state directory.
ARM_STATE_ENV: dict[str, str] = {
    "SPEC_CRITIC_CACHE_PATH": "verification_cache.json",
    "SPEC_CRITIC_PENDING_BATCH_PATH": "pending_batch.json",
    "SPEC_CRITIC_TRACE_DIR": "traces",
    "SPEC_CRITIC_LOG_PATH": "logs/spec_critic.log",
    "SPEC_CRITIC_UPDATE_STATE_PATH": "update_check.json",
    # Plan EX-05's research cache. Its switch is dropped with every other
    # inherited setting, so an arm never reads it; the path is pointed here
    # anyway so no arm can touch the operator's file.
    "SPEC_CRITIC_RESEARCH_CACHE_PATH": "research_cache.json",
}

#: Settings every arm shares that are not part of any experiment.
FIXED_ENV: dict[str, str] = {
    "SPEC_CRITIC_DISABLE_UPDATE_CHECK": "1",
    # The arm's cache lives in the arm's state directory; the runner does not
    # read it (each view gets a fresh in-memory cache) and does not save it.
    "SPEC_CRITIC_VERIFICATION_CACHE_PERSIST": "0",
}


class ArmStateError(RuntimeError):
    """The arm's state directory is not fresh, or points at the operator's own state."""


def arm_environment(arm: Arm, *, state_dir: Path, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The complete environment for ``arm``'s process (see the module docstring)."""
    base = dict(os.environ if base_env is None else base_env)
    env = {k: v for k, v in base.items() if not k.startswith("SPEC_CRITIC_")}
    env.update(FIXED_ENV)
    for name, rel in ARM_STATE_ENV.items():
        env[name] = str(Path(state_dir) / rel)
    if arm.setting is not None:
        env[arm.setting[0]] = arm.setting[1]
    return env


def arm_settings(env: Mapping[str, str]) -> dict[str, str]:
    """The experimental settings in ``env``: its ``SPEC_CRITIC_*`` variables
    minus the state paths and the fixed settings every arm shares."""
    return {
        k: v
        for k, v in env.items()
        if k.startswith("SPEC_CRITIC_") and k not in ARM_STATE_ENV and k not in FIXED_ENV
    }


def environment_problems(arm: Arm, env: Mapping[str, str] | None = None) -> list[str]:
    """Why ``env`` (default: this process) is not ``arm``'s environment."""
    env = dict(os.environ if env is None else env)
    problems = []
    expected = dict([arm.setting]) if arm.setting else {}
    actual = arm_settings(env)
    if actual != expected:
        problems.append(
            f"the process carries experimental settings {sorted(actual.items())}, "
            f"but arm {arm.arm_id} is {sorted(expected.items())}"
        )
    for name in ARM_STATE_ENV:
        if not env.get(name):
            problems.append(f"{name} is not set; every state path must point into the arm's directory")
    for name, value in FIXED_ENV.items():
        if env.get(name) != value:
            problems.append(f"{name} must be {value!r}")
    return problems


def _operator_state_dirs() -> list[Path]:
    return [Path.home() / ".spec_critic"]


def prepare_state_dir(state_root: Path, arm: Arm) -> Path:
    """Create ``<state_root>/<arm_id>``, refusing one that already holds state."""
    root = Path(state_root).resolve()
    for guarded in _operator_state_dirs():
        guarded = guarded.resolve()
        if root == guarded or guarded in root.parents:
            raise ArmStateError(f"{root} is inside the app's own state directory {guarded}")
    state_dir = root / arm.arm_id
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ArmStateError(
            f"{state_dir} already holds state; a warm cache or a saved batch would leak "
            "into the arm. Use a fresh state root."
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


# --------------------------------------------------------------------------
# Request probe: what this process would send
# --------------------------------------------------------------------------

_PROBE_SPEC = "PART 1 GENERAL\n1.01 SUMMARY\nA. Provide the specified piping system."


def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tool_names(params: Mapping[str, Any]) -> list[str]:
    return sorted(str(t.get("name") or t.get("type")) for t in params.get("tools") or [])


def _verification_shape(finding: Any, *, cycle: Any, escalated: bool) -> dict[str, Any]:
    from src.verification.verification_routing import build_verification_request, select_routing
    from src.verification.verifier import _build_verification_prompt, _get_verification_system_prompt

    decision = select_routing(finding, escalated=escalated, local_skip=False, cycle=cycle)
    prompt = _build_verification_prompt(finding, cycle=cycle, include_verdict_tool=decision.include_verdict_tool)
    system_prompt = _get_verification_system_prompt(cycle, include_verdict_tool=decision.include_verdict_tool)
    params = build_verification_request(decision, prompt=prompt, system_prompt=system_prompt).params
    return {
        "mode": str(getattr(decision.mode, "value", decision.mode)),
        "model": params.get("model"),
        "effort": (params.get("output_config") or {}).get("effort"),
        "thinking": params.get("thinking"),
        "max_tokens": params.get("max_tokens"),
        "tools": _tool_names(params),
        "system_prompt_sha256": _digest(system_prompt),
    }


def request_probe() -> dict[str, Any]:
    """The request fields each experiment could move, as this process builds them.

    Builds real requests with the production builders (no network): the
    per-spec review, an initial verification (a HIGH finding with a code
    reference), its escalation, a CRITICAL jurisdictional finding's initial
    pass (routed to the escalation tier), and the effort of the phases the
    review switch must not touch. Flattened to ``"<request>.<field>"`` keys.
    """
    from src.core import api_config
    from src.core.code_cycles import DEFAULT_CYCLE
    from src.review.review_request_builder import ReviewRequestSpec, build_review_request
    from src.review.reviewer import Finding
    from src.verification.verification_prescreen import should_escalate_verification

    built = build_review_request(
        ReviewRequestSpec(
            spec_content=_PROBE_SPEC,
            filename="230500.docx",
            model=api_config.REVIEW_MODEL_DEFAULT,
            cycle=DEFAULT_CYCLE,
            include_service_tier=False,
        )
    )
    params = built.params
    shapes: dict[str, dict[str, Any]] = {
        "review": {
            "model": params.get("model"),
            "effort": (params.get("output_config") or {}).get("effort"),
            "thinking": params.get("thinking"),
            "max_tokens": params.get("max_tokens"),
            "tools": _tool_names(params),
            "tool_choice": params.get("tool_choice"),
            "system_prompt_sha256": _digest(built.system_prompt),
            "user_message_sha256": _digest(built.user_message),
        }
    }
    high = Finding(
        severity="HIGH",
        fileName="21 13 13.docx",
        section="3.01",
        issue="Maximum protection area per sprinkler exceeds the ordinary hazard limit.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13",
    )
    critical = Finding(
        severity="CRITICAL",
        fileName="21 13 13.docx",
        section="3.01",
        issue="The DSA-approved drawings require seismic bracing the section omits.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13; CBC Chapter 16",
    )
    shapes["verification_initial"] = _verification_shape(high, cycle=DEFAULT_CYCLE, escalated=False)
    shapes["escalation"] = _verification_shape(high, cycle=DEFAULT_CYCLE, escalated=True)
    shapes["deep_initial"] = _verification_shape(critical, cycle=DEFAULT_CYCLE, escalated=False)
    shapes["other_phases"] = {
        phase: (api_config.effort_config_for(model=model, phase=phase) or {}).get("effort")
        for phase, model in (
            (api_config.PHASE_CROSS_CHECK, api_config.CROSS_CHECK_MODEL_DEFAULT),
            (api_config.PHASE_COMPLIANCE, api_config.COMPLIANCE_MODEL_DEFAULT),
            (api_config.PHASE_RESEARCH, api_config.RESEARCH_MODEL_DEFAULT),
        )
    }
    shapes["escalation_gate"] = {
        "fires_on_unresolved_high": should_escalate_verification(
            high,
            verdict="UNVERIFIED",
            grounded=False,
            successful_source_count=0,
            search_error_count=0,
        )
    }
    flat: dict[str, Any] = {}
    for request, fields in shapes.items():
        for key, value in fields.items():
            flat[f"{request}.{key}"] = value
    return flat


def probe_differences(baseline: Mapping[str, Any], other: Mapping[str, Any]) -> list[str]:
    """Probe keys whose values differ, sorted."""
    return sorted(k for k in set(baseline) | set(other) if baseline.get(k) != other.get(k))


def probe_arm_subprocess(
    arm: Arm,
    *,
    state_root: Path,
    python: str = sys.executable,
    base_env: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run :func:`request_probe` in a fresh process with ``arm``'s environment."""
    state_dir = prepare_state_dir(state_root, arm)
    env = arm_environment(arm, state_dir=state_dir, base_env=base_env)
    env.setdefault("PYTHONPATH", str(ds._REPO_ROOT))
    completed = subprocess.run(
        [python, "-m", "evals.model_effort", "probe"],
        cwd=str(ds._REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"probe for {arm.arm_id} failed: {completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------
# Cost and latency
# --------------------------------------------------------------------------


def price_attempts(attempts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Estimated USD of known attempts, and the count of unknown or unpriced ones.

    Every attempt is priced on its own model at its own transport's rate
    (``core.pricing``), as the diagnostics cost summary does.
    """
    from src.core.api_config import cache_pricing_kwargs
    from src.core.attempt_usage import AttemptUsage
    from src.core.pricing import estimate_cost_breakdown

    total = 0.0
    unknown = 0
    unpriced = 0
    count = 0
    for raw in attempts:
        count += 1
        attempt = AttemptUsage.from_dict(raw) if not isinstance(raw, AttemptUsage) else raw
        if not attempt.usage_known:
            unknown += 1
            continue
        cost = estimate_cost_breakdown(
            attempt.input_tokens,
            attempt.output_tokens,
            model=attempt.model,
            batch=attempt.transport == "batch",
            web_search_requests=attempt.web_search_requests,
            **cache_pricing_kwargs(attempt),
        )
        if cost is None:
            unpriced += 1
            continue
        total += cost.total
    return {"usd": round(total, 6), "attempts": count, "unknown_usage": unknown, "unpriced": unpriced}


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100); ``None`` for no values."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    rank = max(1, math.ceil(q / 100.0 * len(vals)))
    return vals[min(rank, len(vals)) - 1]


# --------------------------------------------------------------------------
# Runner (the arm's own process)
# --------------------------------------------------------------------------

VIEW_PATH = "path"
VIEW_TIER = "tier"
VIEWS = (VIEW_PATH, VIEW_TIER)

_SENTINEL_KEY = "test-key-not-real-do-not-use"


class RunRefused(RuntimeError):
    """A run's preconditions do not hold; nothing was sent."""


def _record_path(out_dir: Path, arm_id: str, stage: str, repetition: int) -> Path:
    return Path(out_dir) / f"{arm_id}.{stage}.r{repetition}.jsonl"


def _summary_path(out_dir: Path, arm_id: str, stage: str, repetition: int) -> Path:
    return Path(out_dir) / f"{arm_id}.{stage}.r{repetition}.summary.json"


def _profile_inputs(project: Mapping[str, str]) -> tuple[dict | None, str | None, str | None]:
    """``(user_location, jurisdiction_fingerprint, country)`` for a case's project."""
    if not project:
        return None, None, None
    from src.core.project_profile import ProjectProfile

    profile = ProjectProfile(
        city=project.get("city", ""),
        state_or_province=project.get("state_or_province", ""),
        country=project.get("country", ""),
        client_name=project.get("client_name", ""),
    )
    return profile.web_search_user_location(), profile.jurisdiction_fingerprint(), profile.country


def _verification_record(case: ds.VerificationCase, result: Any, *, view: str, latency: float) -> dict[str, Any]:
    from src.output.report_status import classify_status
    from src.review.reviewer import Finding

    finding = Finding(**dict(case.finding))
    finding.verification = result
    attempts = list(getattr(result, "call_usage", None) or [])
    return {
        "view": view,
        "verdict": str(getattr(result, "verdict", "") or ""),
        # ``status`` is the run's record status ("ok" / "error" / "not_run").
        "report_status": str(getattr(classify_status(finding), "value", classify_status(finding))),
        "grounded": bool(getattr(result, "grounded", False)),
        "verification_failed": bool(getattr(result, "verification_failed", False)),
        "outcome": str(getattr(result, "outcome", "") or ""),
        "cache_status": str(getattr(result, "cache_status", "") or ""),
        "verification_mode": str(getattr(result, "verification_mode", "") or ""),
        "model_used": str(getattr(result, "model_used", "") or ""),
        "escalation_attempted": bool(getattr(result, "escalation_attempted", False)),
        "escalated": bool(getattr(result, "escalated", False)),
        "models_disagreed": bool(getattr(result, "models_disagreed", False)),
        "budget_exhausted": bool(getattr(result, "budget_exhausted", False)),
        "accepted_sources": len(getattr(result, "sources", None) or []),
        "has_source_quote": bool((getattr(result, "source_quote", "") or "").strip()),
        "web_search_requests": int(getattr(result, "web_search_requests", 0) or 0),
        "web_fetch_requests": int(getattr(result, "web_fetch_requests", 0) or 0),
        "native_citations": len(getattr(result, "native_citations", None) or []),
        "attempts": attempts,
        "cost": price_attempts(attempts),
        "latency_seconds": round(latency, 3),
    }


def _materialize_spec(case: ds.ReviewCase, directory: Path) -> Path:
    """Write the case's text as a .docx (one paragraph per line)."""
    from docx import Document

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / case.filename
    doc = Document()
    for line in case.spec_text.split("\n"):
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


def _prepared_review(case: ds.ReviewCase, *, spec_dir: Path) -> tuple[Any, list[dict]]:
    """Extract and pre-screen the case exactly as ``pipeline._prepare_specs`` does."""
    from src.input.extractor import extract_text_from_docx
    from src.input.preprocessor import preprocess_spec
    from src.modules.registry import get_module

    module = get_module(case.module_id)
    spec = extract_text_from_docx(_materialize_spec(case, spec_dir))
    _loc, _fp, country = _profile_inputs(case.project)
    pre = preprocess_spec(
        spec.content,
        spec.filename,
        cycle=module.cycle,
        profile_country=country,
        label_spans=getattr(spec, "label_spans", ()),
    )
    alerts = [
        *pre.leed_alerts,
        *pre.placeholder_alerts,
        *pre.code_cycle_alerts,
        *pre.structural_alerts,
        *pre.template_marker_alerts,
        *pre.invalid_code_cycle_alerts,
        *pre.duplicate_paragraph_alerts,
        *pre.polity_alerts,
    ]
    return spec, alerts


def _finding_dict(finding: Any) -> dict[str, Any]:
    keys = ("severity", "section", "issue", "actionType", "existingText", "replacementText",
            "codeReference", "confidence", "anchorText", "insertPosition", "evidenceElementId")
    return {k: getattr(finding, k, None) for k in keys}


def _review_record(result: Any, *, latency: float) -> dict[str, Any]:
    attempts = list(getattr(result, "call_usage", None) or [])
    return {
        "parse_status": str(getattr(result, "parse_status", "") or ""),
        "parse_source": str(getattr(result, "parse_source", "") or ""),
        "error": getattr(result, "error", None),
        "findings": [_finding_dict(f) for f in getattr(result, "findings", None) or []],
        "repair_attempts": sum(1 for a in attempts if a.get("role") == "repair"),
        "attempts": attempts,
        "cost": price_attempts(attempts),
        "latency_seconds": round(latency, 3),
    }


def check_run_preconditions(arm: Arm, *, out_dir: Path, stage: str, repetition: int, live: bool,
                            max_spend_usd: float | None, env: Mapping[str, str] | None = None) -> None:
    """Raise :class:`RunRefused` unless a paid run of ``arm`` may start here."""
    env = dict(os.environ if env is None else env)
    problems: list[str] = []
    if not live:
        problems.append("a run sends paid requests; pass --live to confirm")
    if max_spend_usd is None or not (max_spend_usd > 0):
        problems.append("a positive spending cap (--max-spend-usd) is required")
    key = env.get("ANTHROPIC_API_KEY", "")
    if not key or key == _SENTINEL_KEY:
        problems.append("no real ANTHROPIC_API_KEY in the environment")
    problems.extend(environment_problems(arm, env))
    if _record_path(out_dir, arm.arm_id, stage, repetition).exists():
        problems.append("this arm, stage, and repetition already has records; use a new output directory")
    # The escalation model is read when api_config is imported: check what this
    # process actually resolved, not only what the environment says.
    from src.core import api_config

    wanted = env.get(ENV_ESCALATION_MODEL) or api_config.MODEL_OPUS_55
    if api_config.VERIFICATION_ESCALATION_MODEL != wanted:
        problems.append(
            f"this process resolved the escalation model {api_config.VERIFICATION_ESCALATION_MODEL!r}, "
            f"not {wanted!r}; start the arm in a fresh process"
        )
    if problems:
        raise RunRefused("; ".join(problems))


def run_arm(
    arm_id: str,
    *,
    stage: str,
    out_dir: Path,
    max_spend_usd: float,
    split: str = ds.SPLIT_HELD_OUT,
    repetition: int = 1,
    views: Sequence[str] = VIEWS,
    live: bool = False,
    cases: Sequence[ds.EvalCase] | None = None,
    log: Callable[[str], None] = lambda _m: None,
    _skip_preconditions: bool = False,
) -> dict[str, Any]:
    """Run one arm's cases of one stage in this process, recording every outcome.

    Writes ``<arm>.<stage>.r<n>.jsonl`` (one record per case and view) and a
    summary beside it. The spending cap is checked before each case: the run
    stops once the estimated spend reaches it, and the remaining cases are
    recorded as not run. One case can overshoot the cap by its own cost (a
    review with its repair, or a verification with its escalation).

    Verification cases run once per view: ``path`` is production's
    ``verify_finding`` (the initial pass, then the escalation tier when the gate
    fires); ``tier`` forces the escalation tier (``escalated=True``) so every
    case measures the escalation model, not just the ones the gate sends there.
    Each view has its own fresh cache: the key does not include the model or
    the escalation flag, so one cache would replay the path view's verdict in
    the tier view.
    """
    from src.modules.registry import get_module
    from src.verification.verification_cache import VerificationCache

    arm = ARMS[arm_id]
    out_dir = Path(out_dir)
    if not _skip_preconditions:
        check_run_preconditions(arm, out_dir=out_dir, stage=stage, repetition=repetition,
                                live=live, max_spend_usd=max_spend_usd)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_cases = list(ds.load_dataset() if cases is None else cases)
    if stage == ds.STAGE_VERIFICATION:
        selected: list[ds.EvalCase] = ds.verification_cases(all_cases, split=split)
    elif stage == ds.STAGE_REVIEW:
        selected = ds.review_cases(all_cases, split=split)
    else:
        raise ValueError(f"unknown stage {stage!r}")

    record_path = _record_path(out_dir, arm_id, stage, repetition)
    started = datetime.now(timezone.utc).isoformat()
    spent = 0.0
    unknown_usage = 0
    stopped_reason = ""
    caches = {view: VerificationCache() for view in views}
    spec_dir = out_dir / "specs" / arm_id / f"r{repetition}"

    with record_path.open("x", encoding="utf-8") as fh:
        for case in selected:
            base = {"arm_id": arm_id, "case_id": case.case_id, "split": case.split,
                    "stage": stage, "repetition": repetition,
                    "case_sha256": ds.case_digest(case)}
            if stopped_reason:
                fh.write(json.dumps({**base, "status": "not_run", "reason": stopped_reason}) + "\n")
                continue
            if spent >= max_spend_usd:
                stopped_reason = f"spending cap reached (${spent:.2f} of ${max_spend_usd:.2f})"
                fh.write(json.dumps({**base, "status": "not_run", "reason": stopped_reason}) + "\n")
                continue
            module = get_module(case.module_id)
            if isinstance(case, ds.VerificationCase):
                from src.review.reviewer import Finding
                from src.verification.verifier import verify_finding

                user_location, fingerprint, _country = _profile_inputs(case.project)
                for view in views:
                    started_at = time.monotonic()
                    try:
                        result = verify_finding(
                            Finding(**dict(case.finding)),
                            cycle=module.cycle,
                            cache=caches[view],
                            escalated=view == VIEW_TIER,
                            user_location=user_location,
                            jurisdiction_fingerprint=fingerprint,
                        )
                    except Exception as exc:  # recorded, never raised: the run continues
                        fh.write(json.dumps({**base, "view": view, "status": "error",
                                             "error": f"{type(exc).__name__}: {exc}"}) + "\n")
                        continue
                    record = _verification_record(case, result, view=view,
                                                  latency=time.monotonic() - started_at)
                    spent += record["cost"]["usd"]
                    unknown_usage += record["cost"]["unknown_usage"]
                    fh.write(json.dumps({**base, "status": "ok", **record}, default=str) + "\n")
            else:
                from src.core.api_config import REVIEW_MODEL_DEFAULT
                from src.review.realtime_review import build_realtime_review_jobs, run_realtime_review_jobs

                started_at = time.monotonic()
                try:
                    spec, alerts = _prepared_review(case, spec_dir=spec_dir)
                    jobs, _request_map = build_realtime_review_jobs(
                        [spec],
                        project_context=case.project_context,
                        model=REVIEW_MODEL_DEFAULT,
                        cycle=module.cycle,
                        pre_detected_alerts={spec.filename: alerts},
                    )
                    results = run_realtime_review_jobs(jobs, max_workers=1)
                    result = results[jobs[0].job_key]
                except Exception as exc:
                    fh.write(json.dumps({**base, "status": "error",
                                         "error": f"{type(exc).__name__}: {exc}"}) + "\n")
                    continue
                record = _review_record(result, latency=time.monotonic() - started_at)
                spent += record["cost"]["usd"]
                unknown_usage += record["cost"]["unknown_usage"]
                fh.write(json.dumps({**base, "status": "ok", **record}, default=str) + "\n")
            log(f"{arm_id} {case.case_id}: spent ${spent:.4f}")

    cache_stats = {view: cache.stats() for view, cache in caches.items()}
    summary = {
        "arm_id": arm_id,
        "setting": list(arm.setting) if arm.setting else None,
        "stage": stage,
        "split": split,
        "repetition": repetition,
        "views": list(views) if stage == ds.STAGE_VERIFICATION else [],
        "dataset_sha256": ds.dataset_digest(all_cases),
        "split_sha256": ds.dataset_digest(all_cases, split=split),
        "cases": len(selected),
        "estimated_spend_usd": round(spent, 6),
        "unknown_usage_attempts": unknown_usage,
        "max_spend_usd": max_spend_usd,
        "stopped_reason": stopped_reason,
        "cache_stats": cache_stats if stage == ds.STAGE_VERIFICATION else {},
        # A hit means a case replayed another's verdict inside the arm; the
        # dataset's unique cache keys make this impossible, so a nonzero count
        # says the isolation broke and the arm's results are not usable.
        "cache_contaminated": any(int(s.get("hits", 0)) for s in cache_stats.values())
        if stage == ds.STAGE_VERIFICATION else False,
        "started_at": started,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "request_probe_sha256": _digest(request_probe()),
    }
    _summary_path(out_dir, arm_id, stage, repetition).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_experiment(
    experiment_id: str,
    *,
    state_root: Path,
    out_dir: Path,
    max_spend_usd: float,
    split: str = ds.SPLIT_HELD_OUT,
    repetitions: int = 2,
    python: str = sys.executable,
    live: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict[str, Any]]:
    """Run an experiment's baseline and candidate, each in its own process.

    The cap is split evenly across every (arm, repetition) run. Repetitions
    alternate the order (baseline first, then candidate first) so a drift in
    the service over the run does not favor one arm.
    """
    if not live:
        raise RunRefused("a run sends paid requests; pass --live to confirm")
    exp = EXPERIMENTS[experiment_id]
    per_run = max_spend_usd / (2 * repetitions)
    outcomes = []
    for rep in range(1, repetitions + 1):
        order = (exp.baseline, exp.candidate) if rep % 2 else (exp.candidate, exp.baseline)
        for arm_id in order:
            arm = ARMS[arm_id]
            state_dir = prepare_state_dir(Path(state_root) / f"r{rep}", arm)
            env = arm_environment(arm, state_dir=state_dir)
            env.setdefault("PYTHONPATH", str(ds._REPO_ROOT))
            cmd = [python, "-m", "evals.model_effort", "run", "--arm", arm_id, "--stage", exp.stage,
                   "--out", str(out_dir), "--split", split, "--repetition", str(rep),
                   "--max-spend-usd", f"{per_run:.4f}", "--live"]
            completed = runner(cmd, cwd=str(ds._REPO_ROOT), env=env, check=False)
            outcomes.append({"arm_id": arm_id, "repetition": rep, "returncode": completed.returncode})
    return outcomes


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """The 95% Wilson score interval for ``successes / n`` (``None`` when n is 0)."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _rate(successes: int, n: int) -> dict[str, Any]:
    return {"count": successes, "n": n, "rate": round(successes / n, 4) if n else None,
            "wilson95": wilson_interval(successes, n)}


def exact_mcnemar_p(b: int, c: int) -> float | None:
    """Two-sided exact binomial p for ``b`` vs ``c`` discordant pairs."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 4)


def load_records(out_dir: Path, arm_id: str, stage: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(Path(out_dir).glob(f"{arm_id}.{stage}.r*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


_CONCLUSIVE = ("CONFIRMED", "CORRECTED", "DISPUTED")


def classify_verification_outcome(case: ds.VerificationCase, record: Mapping[str, Any]) -> dict[str, bool]:
    """What one verification record means against its case (see the decision record)."""
    verdict = str(record.get("verdict") or "")
    failed = bool(record.get("verification_failed")) or record.get("status") != "ok"
    local = str(record.get("cache_status") or "") == "local_skip"
    accepted = case.accepted
    correct = not failed and not local and verdict == case.expected_verdict
    return {
        "failed": failed,
        "local": local,
        "correct": correct,
        "acceptable": not failed and not local and verdict in accepted,
        "false_confirmed": not failed and verdict == "CONFIRMED" and "CONFIRMED" not in accepted,
        "false_disputed": not failed and verdict == "DISPUTED" and "DISPUTED" not in accepted,
        "false_corrected": not failed and verdict == "CORRECTED" and "CORRECTED" not in accepted,
        "unwarranted_uncertainty": (not failed and not local and verdict == "UNVERIFIED"
                                    and "UNVERIFIED" not in accepted),
        "kept_uncertainty": not failed and verdict == "UNVERIFIED" and case.expected_verdict == "UNVERIFIED",
        # A severe true finding the verifier told a reviewer to throw away.
        "discarded_severe_true": (not failed and case.severe and verdict == "DISPUTED"
                                  and case.expected_verdict in ("CONFIRMED", "CORRECTED")),
    }


def score_verification(records: Iterable[Mapping[str, Any]], cases: Iterable[ds.EvalCase], *,
                       view: str, split: str = ds.SPLIT_HELD_OUT) -> dict[str, Any]:
    by_id = {c.case_id: c for c in ds.verification_cases(cases, split=split)}
    rows = [r for r in records if r.get("case_id") in by_id and r.get("view", view) == view
            and r.get("status") != "not_run"]
    not_run = sum(1 for r in records if r.get("case_id") in by_id and r.get("status") == "not_run")
    outcomes = [(by_id[r["case_id"]], r, classify_verification_outcome(by_id[r["case_id"]], r)) for r in rows]
    n = len(outcomes)
    scored = [o for o in outcomes if not o[2]["local"]]
    n_scored = len(scored)
    not_confirmed_truth = [o for o in scored if "CONFIRMED" not in o[0].accepted]
    not_disputed_truth = [o for o in scored if "DISPUTED" not in o[0].accepted]
    expected_unverified = [o for o in scored if o[0].expected_verdict == "UNVERIFIED"]
    expected_conclusive = [o for o in scored if o[0].expected_verdict in _CONCLUSIVE]
    severe_true = [o for o in scored if o[0].severe and o[0].expected_verdict in ("CONFIRMED", "CORRECTED")]
    severe = [o for o in scored if o[0].severe]
    conclusive = [o for o in scored if str(o[1].get("verdict")) in _CONCLUSIVE and not o[2]["failed"]]
    latencies = [float(r.get("latency_seconds") or 0.0) for _c, r, _o in outcomes]
    cost = sum(float((r.get("cost") or {}).get("usd") or 0.0) for _c, r, _o in outcomes)
    return {
        "view": view,
        "split": split,
        "records": n,
        "not_run": not_run,
        "classified_locally": n - n_scored,
        "accuracy": _rate(sum(o[2]["correct"] for o in scored), n_scored),
        "acceptable": _rate(sum(o[2]["acceptable"] for o in scored), n_scored),
        "false_confirmed": _rate(sum(o[2]["false_confirmed"] for o in not_confirmed_truth), len(not_confirmed_truth)),
        "false_disputed": _rate(sum(o[2]["false_disputed"] for o in not_disputed_truth), len(not_disputed_truth)),
        "false_confirmed_severe": sum(o[2]["false_confirmed"] for o in severe),
        "discarded_severe_true": _rate(sum(o[2]["discarded_severe_true"] for o in severe_true), len(severe_true)),
        "kept_legitimate_uncertainty": _rate(sum(o[2]["kept_uncertainty"] for o in expected_unverified),
                                             len(expected_unverified)),
        "unwarranted_uncertainty": _rate(sum(o[2]["unwarranted_uncertainty"] for o in expected_conclusive),
                                         len(expected_conclusive)),
        "operational_failures": _rate(sum(o[2]["failed"] for o in scored), n_scored),
        "evidence": {
            "conclusive_verdicts": len(conclusive),
            "grounded": sum(1 for _c, r, _o in conclusive if r.get("grounded")),
            "with_source_quote": sum(1 for _c, r, _o in conclusive if r.get("has_source_quote")),
            "mean_accepted_sources": round(sum(int(r.get("accepted_sources") or 0) for _c, r, _o in conclusive)
                                           / len(conclusive), 3) if conclusive else None,
            "used_web_fetch": sum(1 for _c, r, _o in conclusive if int(r.get("web_fetch_requests") or 0) > 0),
        },
        "escalation": {
            "escalation_attempted": sum(1 for _c, r, _o in outcomes if r.get("escalation_attempted")),
            "deep_reasoning": sum(1 for _c, r, _o in outcomes if r.get("verification_mode") == "deep_reasoning"),
            "contested": sum(1 for _c, r, _o in outcomes if r.get("models_disagreed")),
        },
        "cost_usd": {"total": round(cost, 6), "per_record": round(cost / n, 6) if n else None,
                     "unknown_usage_attempts": sum(int((r.get("cost") or {}).get("unknown_usage") or 0)
                                                   for _c, r, _o in outcomes)},
        "latency_seconds": {"p50": percentile(latencies, 50), "p90": percentile(latencies, 90),
                            "max": max(latencies) if latencies else None},
    }


def _haystack(finding: Mapping[str, Any]) -> str:
    return " ".join(str(finding.get(k) or "") for k in ("issue", "existingText", "section", "codeReference")).lower()


def _matches(match_any: Sequence[Sequence[str]], finding: Mapping[str, Any]) -> bool:
    hay = _haystack(finding)
    return any(group and all(token.lower() in hay for token in group) for group in match_any)


def match_review(case: ds.ReviewCase, findings: Sequence[Mapping[str, Any]],
                 adjudication: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Which finding each defect matched, which findings hit traps, and which are extra.

    ``adjudication`` (optional, per case) overrides the substring matcher:
    ``{"matches": {defect label: finding index or null}, "unsupported": [finding index, ...]}``.
    """
    adjudication = adjudication or {}
    overrides = adjudication.get("matches") or {}
    used: set[int] = set()
    matched: dict[str, int | None] = {}
    for defect in case.defects:
        if defect.label in overrides:
            idx = overrides[defect.label]
            matched[defect.label] = idx if idx is None else int(idx)
        else:
            idx = next((i for i, f in enumerate(findings) if i not in used and _matches(defect.match_any, f)), None)
            matched[defect.label] = idx
        if matched[defect.label] is not None:
            used.add(int(matched[defect.label]))
    trap_hits: list[tuple[str, int]] = []
    for trap in case.traps:
        for i, f in enumerate(findings):
            if i in used:
                continue
            action = str(f.get("actionType") or "").upper()
            if action in trap.actions and _matches(trap.match_any, f):
                trap_hits.append((trap.label, i))
    unsupported = {i for _label, i in trap_hits} | {int(i) for i in adjudication.get("unsupported") or []}
    extras = [i for i in range(len(findings)) if i not in used and i not in unsupported]
    return {"matched": matched, "trap_hits": trap_hits, "unsupported": sorted(unsupported), "unclassified_extras": extras}


def _confidence_band(value: Any) -> str:
    from src.review.structured_schemas import CONFIDENCE_HIGH_MIN, CONFIDENCE_MODERATE_MIN

    try:
        v = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if v >= CONFIDENCE_HIGH_MIN:
        return "high"
    if v >= CONFIDENCE_MODERATE_MIN:
        return "moderate"
    return "low"


def score_review(records: Iterable[Mapping[str, Any]], cases: Iterable[ds.EvalCase], *,
                 split: str = ds.SPLIT_HELD_OUT,
                 adjudication: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Review metrics over one arm's records (every repetition pooled)."""
    by_id = {c.case_id: c for c in ds.review_cases(cases, split=split)}
    rows = [r for r in records if r.get("case_id") in by_id and r.get("status") != "not_run"]
    not_run = sum(1 for r in records if r.get("case_id") in by_id and r.get("status") == "not_run")
    defects = severe = found = severe_found = severity_match = 0
    trap_hits = unsupported = extras = findings_total = failed = repaired = 0
    bands: dict[str, dict[str, int]] = {}
    brier: list[float] = []
    latencies: list[float] = []
    cost = 0.0
    for r in rows:
        case = by_id[r["case_id"]]
        ok = r.get("status") == "ok" and r.get("parse_status") == "ok"
        findings = list(r.get("findings") or []) if ok else []
        failed += 0 if ok else 1
        repaired += 1 if int(r.get("repair_attempts") or 0) > 0 else 0
        latencies.append(float(r.get("latency_seconds") or 0.0))
        cost += float((r.get("cost") or {}).get("usd") or 0.0)
        findings_total += len(findings)
        m = match_review(case, findings, (adjudication or {}).get(case.case_id))
        for d in case.defects:
            defects += 1
            severe += d.severe
            idx = m["matched"][d.label]
            if idx is not None:
                found += 1
                severe_found += d.severe
                if str(findings[idx].get("severity") or "").upper() == d.severity.upper():
                    severity_match += 1
        trap_hits += len(m["trap_hits"])
        unsupported += len(m["unsupported"])
        extras += len(m["unclassified_extras"])
        labels = {idx: 1.0 for idx in m["matched"].values() if idx is not None}
        labels.update({idx: 0.0 for idx in m["unsupported"]})
        for idx, label in labels.items():
            band = _confidence_band(findings[idx].get("confidence"))
            slot = bands.setdefault(band, {"findings": 0, "supported": 0})
            slot["findings"] += 1
            slot["supported"] += int(label)
            try:
                brier.append((float(findings[idx].get("confidence")) - label) ** 2)
            except (TypeError, ValueError):
                pass
    return {
        "split": split,
        "records": len(rows),
        "not_run": not_run,
        "recall": _rate(found, defects),
        "severe_recall": _rate(severe_found, severe),
        "severity_match": _rate(severity_match, found),
        "findings": findings_total,
        "trap_hits": trap_hits,
        "unsupported_findings": _rate(unsupported, findings_total),
        "unclassified_extra_findings": extras,
        "failed_reviews": _rate(failed, len(rows)),
        "repaired_reviews": _rate(repaired, len(rows)),
        "confidence_calibration": {
            "by_band": bands,
            "brier": round(sum(brier) / len(brier), 4) if brier else None,
            "labelled_findings": len(brier),
            "note": "Only matched (1) and unsupported (0) findings are labelled; extras are not scored.",
        },
        "cost_usd": {"total": round(cost, 6), "per_record": round(cost / len(rows), 6) if rows else None},
        "latency_seconds": {"p50": percentile(latencies, 50), "p90": percentile(latencies, 90),
                            "max": max(latencies) if latencies else None},
    }


def paired_verification(base: Iterable[Mapping[str, Any]], cand: Iterable[Mapping[str, Any]],
                        cases: Iterable[ds.EvalCase], *, view: str,
                        split: str = ds.SPLIT_HELD_OUT) -> dict[str, Any]:
    """Discordant pairs by (case, repetition): correct in one arm and not the other."""
    by_id = {c.case_id: c for c in ds.verification_cases(cases, split=split)}

    def index(records):
        out = {}
        for r in records:
            if r.get("case_id") in by_id and r.get("view", view) == view and r.get("status") != "not_run":
                out[(r["case_id"], r.get("repetition", 1))] = classify_verification_outcome(by_id[r["case_id"]], r)
        return out

    a, b = index(base), index(cand)
    keys = sorted(set(a) & set(b))
    lost = sum(1 for k in keys if a[k]["correct"] and not b[k]["correct"])
    gained = sum(1 for k in keys if b[k]["correct"] and not a[k]["correct"])
    severe_discarded_new = sum(1 for k in keys if b[k]["discarded_severe_true"] and not a[k]["discarded_severe_true"])
    return {"pairs": len(keys), "baseline_only_correct": lost, "candidate_only_correct": gained,
            "exact_p": exact_mcnemar_p(lost, gained), "new_severe_discards": severe_discarded_new}


def paired_review(base: Iterable[Mapping[str, Any]], cand: Iterable[Mapping[str, Any]],
                  cases: Iterable[ds.EvalCase], *, split: str = ds.SPLIT_HELD_OUT,
                  adjudication: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Discordant defects by (case, repetition, defect): found in one arm only."""
    by_id = {c.case_id: c for c in ds.review_cases(cases, split=split)}

    def index(records):
        out = {}
        for r in records:
            if r.get("case_id") not in by_id or r.get("status") == "not_run":
                continue
            case = by_id[r["case_id"]]
            ok = r.get("status") == "ok" and r.get("parse_status") == "ok"
            findings = list(r.get("findings") or []) if ok else []
            m = match_review(case, findings, (adjudication or {}).get(case.case_id))
            for d in case.defects:
                out[(case.case_id, r.get("repetition", 1), d.label)] = (m["matched"][d.label] is not None, d.severe)
        return out

    a, b = index(base), index(cand)
    keys = sorted(set(a) & set(b))
    lost = [k for k in keys if a[k][0] and not b[k][0]]
    gained = [k for k in keys if b[k][0] and not a[k][0]]
    return {
        "pairs": len(keys),
        "baseline_only_found": len(lost),
        "candidate_only_found": len(gained),
        "severe_baseline_only": sum(1 for k in lost if a[k][1]),
        "severe_candidate_only": sum(1 for k in gained if a[k][1]),
        "exact_p": exact_mcnemar_p(len(lost), len(gained)),
    }


# --------------------------------------------------------------------------
# Pre-registered decision rules (set 2026-09-29, before any run)
# --------------------------------------------------------------------------

DECISION_PROMOTE = "promote"
DECISION_RETAIN = "retain"
DECISION_REJECT = "reject"
DECISION_DEFER = "defer"

DECISION_RULES: dict[str, dict[str, Any]] = {
    EXPERIMENT_ESCALATION: {
        "scored_on": "held-out split; quality from the tier view (every case runs the escalation "
                     "tier), cost and latency from the path view (production's effect)",
        "min_scored_pairs": 30,
        "reject_if": [
            "the candidate returns more false DISPUTED verdicts than the baseline (tier view)",
            "the candidate discards a severe true finding the baseline kept (tier or path view)",
            "the candidate returns more than one more false CONFIRMED than the baseline (tier view)",
            "the candidate has more than one more operational failure than the baseline (either view)",
        ],
        "promote_if": [
            "not rejected",
            "the candidate is correct on at least 3 more (case, repetition) pairs than it loses (tier view)",
            "path-view cost per record is at most 1.25x the baseline's",
            "path-view p90 latency is at most 1.5x the baseline's",
        ],
        "otherwise": "retain Opus 5.5",
    },
    EXPERIMENT_REVIEW_EFFORT: {
        "scored_on": "held-out review cases, every repetition pooled",
        "min_severe_defect_pairs": 16,
        "reject_if": [
            "the candidate misses more severe defects than it newly finds (paired)",
            "the candidate hits more than one more trap than the baseline",
            "the candidate has more failed reviews than the baseline",
        ],
        "promote_if": [
            "not rejected",
            "the candidate newly finds at least 2 severe defects and misses none the baseline found",
            "trap hits are not higher",
            "cost per review is at most 1.5x the baseline's",
        ],
        "otherwise": "retain the default (medium)",
    },
    EXPERIMENT_REVIEW_SCOPE: {
        "scored_on": "held-out review cases, every repetition pooled",
        "min_defect_pairs": 20,
        "reject_if": [
            "the candidate misses more severe defects than it newly finds (paired)",
            "the candidate hits more than one more trap than the baseline",
            "the candidate has more failed reviews than the baseline",
        ],
        "promote_if": [
            "not rejected",
            "the candidate newly finds at least 2 more defects than it misses, and misses at most 1",
            "trap hits are not higher",
            "cost per review is at most 1.25x the baseline's",
        ],
        "otherwise": "retain the current wording",
    },
}


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def decide(experiment_id: str, *, baseline: Mapping[str, Any], candidate: Mapping[str, Any],
           paired: Mapping[str, Any], baseline_path: Mapping[str, Any] | None = None,
           candidate_path: Mapping[str, Any] | None = None,
           paired_path: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply :data:`DECISION_RULES` to scored arms; returns the decision and why."""
    rules = DECISION_RULES[experiment_id]
    reasons: list[str] = []
    if experiment_id == EXPERIMENT_ESCALATION:
        if paired["pairs"] < rules["min_scored_pairs"]:
            return {"decision": DECISION_DEFER,
                    "reasons": [f"only {paired['pairs']} scored pairs (minimum {rules['min_scored_pairs']})"]}
        fd_b, fd_c = baseline["false_disputed"]["count"], candidate["false_disputed"]["count"]
        fc_b, fc_c = baseline["false_confirmed"]["count"], candidate["false_confirmed"]["count"]
        of_b, of_c = baseline["operational_failures"]["count"], candidate["operational_failures"]["count"]
        new_discards = paired.get("new_severe_discards", 0) + (paired_path or {}).get("new_severe_discards", 0)
        if fd_c > fd_b:
            reasons.append(f"false DISPUTED rose from {fd_b} to {fd_c}")
        if new_discards:
            reasons.append(f"{new_discards} severe true finding(s) discarded that the baseline kept")
        if fc_c > fc_b + 1:
            reasons.append(f"false CONFIRMED rose from {fc_b} to {fc_c}")
        if baseline_path is not None and candidate_path is not None:
            of_b += baseline_path["operational_failures"]["count"]
            of_c += candidate_path["operational_failures"]["count"]
        if of_c > of_b + 1:
            reasons.append(f"operational failures rose from {of_b} to {of_c}")
        if reasons:
            return {"decision": DECISION_REJECT, "reasons": reasons}
        net = paired["candidate_only_correct"] - paired["baseline_only_correct"]
        checks = []
        if net < 3:
            checks.append(f"net gain of {net} correct pairs is below 3")
        cost_ratio = _ratio((candidate_path or {}).get("cost_usd", {}).get("per_record"),
                            (baseline_path or {}).get("cost_usd", {}).get("per_record"))
        if cost_ratio is None or cost_ratio > 1.25:
            checks.append(f"path-view cost ratio {cost_ratio} is unknown or above 1.25")
        lat_ratio = _ratio((candidate_path or {}).get("latency_seconds", {}).get("p90"),
                           (baseline_path or {}).get("latency_seconds", {}).get("p90"))
        if lat_ratio is None or lat_ratio > 1.5:
            checks.append(f"path-view p90 latency ratio {lat_ratio} is unknown or above 1.5")
        if checks:
            return {"decision": DECISION_RETAIN, "reasons": checks}
        return {"decision": DECISION_PROMOTE, "reasons": [f"net gain {net}, cost ratio {cost_ratio:.2f}"]}

    # Review experiments.
    minimum_key = "min_severe_defect_pairs" if experiment_id == EXPERIMENT_REVIEW_EFFORT else "min_defect_pairs"
    measured = paired["pairs"] if minimum_key == "min_defect_pairs" else candidate["severe_recall"]["n"]
    if measured < rules[minimum_key]:
        return {"decision": DECISION_DEFER, "reasons": [f"only {measured} scored (minimum {rules[minimum_key]})"]}
    if paired["severe_baseline_only"] > paired["severe_candidate_only"]:
        reasons.append(
            f"severe defects: {paired['severe_baseline_only']} lost, {paired['severe_candidate_only']} gained"
        )
    if candidate["trap_hits"] > baseline["trap_hits"] + 1:
        reasons.append(f"trap hits rose from {baseline['trap_hits']} to {candidate['trap_hits']}")
    if candidate["failed_reviews"]["count"] > baseline["failed_reviews"]["count"]:
        reasons.append(
            f"failed reviews rose from {baseline['failed_reviews']['count']} to {candidate['failed_reviews']['count']}"
        )
    if reasons:
        return {"decision": DECISION_REJECT, "reasons": reasons}
    checks = []
    cost_ratio = _ratio(candidate["cost_usd"]["per_record"], baseline["cost_usd"]["per_record"])
    if experiment_id == EXPERIMENT_REVIEW_EFFORT:
        if paired["severe_candidate_only"] < 2 or paired["severe_baseline_only"] > 0:
            checks.append(
                f"severe defects gained {paired['severe_candidate_only']}, lost {paired['severe_baseline_only']}"
            )
        limit = 1.5
    else:
        net = paired["candidate_only_found"] - paired["baseline_only_found"]
        if net < 2 or paired["baseline_only_found"] > 1:
            checks.append(f"defects gained {paired['candidate_only_found']}, lost {paired['baseline_only_found']}")
        limit = 1.25
    if candidate["trap_hits"] > baseline["trap_hits"]:
        checks.append("trap hits are higher")
    if cost_ratio is None or cost_ratio > limit:
        checks.append(f"cost ratio {cost_ratio} is unknown or above {limit}")
    if checks:
        return {"decision": DECISION_RETAIN, "reasons": checks}
    return {"decision": DECISION_PROMOTE, "reasons": [f"cost ratio {cost_ratio:.2f}"]}


def score_experiment(experiment_id: str, out_dir: Path, *, split: str = ds.SPLIT_HELD_OUT,
                     cases: Sequence[ds.EvalCase] | None = None,
                     adjudication: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Score both arms of an experiment from their records and apply its rules."""
    exp = EXPERIMENTS[experiment_id]
    all_cases = list(ds.load_dataset() if cases is None else cases)
    base = load_records(out_dir, exp.baseline, exp.stage)
    cand = load_records(out_dir, exp.candidate, exp.stage)
    if exp.stage == ds.STAGE_VERIFICATION:
        scores = {
            view: {
                "baseline": score_verification(base, all_cases, view=view, split=split),
                "candidate": score_verification(cand, all_cases, view=view, split=split),
                "paired": paired_verification(base, cand, all_cases, view=view, split=split),
            }
            for view in VIEWS
        }
        decision = decide(
            experiment_id,
            baseline=scores[VIEW_TIER]["baseline"], candidate=scores[VIEW_TIER]["candidate"],
            paired=scores[VIEW_TIER]["paired"],
            baseline_path=scores[VIEW_PATH]["baseline"], candidate_path=scores[VIEW_PATH]["candidate"],
            paired_path=scores[VIEW_PATH]["paired"],
        )
    else:
        scores = {
            "baseline": score_review(base, all_cases, split=split, adjudication=adjudication),
            "candidate": score_review(cand, all_cases, split=split, adjudication=adjudication),
            "paired": paired_review(base, cand, all_cases, split=split, adjudication=adjudication),
        }
        decision = decide(experiment_id, baseline=scores["baseline"], candidate=scores["candidate"],
                          paired=scores["paired"])
    return {"experiment": experiment_id, "split": split, "scores": scores, "decision": decision,
            "dataset_sha256": ds.dataset_digest(all_cases)}


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------

EVALUATION_PROTOCOL: dict[str, str] = {
    "status": (
        "NOT RUN. No live request has been made for this experiment. The owner chose an "
        "offline-only session for S22 (2026-09-29), and no API key was in the environment. "
        "Nothing in this module or its decision record is a measured quality or cost result."
    ),
    "authorization": (
        "A live run needs its own authorization: a maximum spend, an API key, the dataset "
        "digest it will run against, and this stopping rule agreed in advance. The presence "
        "of a key is not authorization to spend."
    ),
    "step_0_recheck": (
        "Recheck model capabilities and list prices for Opus 5.5, Opus 4.8, and Sonnet 5.5, "
        "that Opus 4.8 still accepts web_fetch_20260209, and whether Opus 5.5 now does. "
        "Record them in the decision record."
    ),
    "step_1_harness_check": (
        "Run each arm once on the tuning split with a small cap. Confirm every record is "
        "'ok', no cache hit is recorded, and the matchers read the findings sensibly; fix "
        "matchers or adjudication on the tuning split only."
    ),
    "step_2_runs": (
        "run-experiment on the held-out split with --repetitions 2 (order alternates). One "
        "experiment at a time; nothing else changes between arms. Record the Python and SDK "
        "versions and the start and end times."
    ),
    "step_3_adjudication": (
        "Before scoring the review experiments, a person reads every unclassified extra "
        "finding and records unsupported ones in an adjudication file, blind to the arm."
    ),
    "step_4_score": (
        "score each experiment. Report every metric with its sample size and Wilson "
        "interval, the paired discordance and exact p, and the decision the pre-registered "
        "rules give. Report false CONFIRMED and false DISPUTED separately: a change must not "
        "trade one for the other."
    ),
    "stopping_rule": (
        "Each arm process stops when its share of the cap is spent (checked between cases). "
        "Stop the experiment if any arm records a cache hit, if a probe shows more than one "
        "change, or if the tuning check fails."
    ),
    "hermetic_limit": (
        "The offline tests prove that each arm changes one request field, that caches and "
        "state stay per arm, and that the scorer and rules compute what they say. They do "
        "not measure model quality and must not be reported as if they did."
    ),
}


def describe(*, include_probe: bool = True) -> dict[str, Any]:
    """Everything the offline CLI prints."""
    cases = ds.load_dataset()
    out: dict[str, Any] = {
        "dataset": ds.summarize(cases),
        "dataset_problems": ds.validate_dataset(cases),
        "arms": {a.arm_id: {"setting": a.setting, "rationale": a.rationale,
                            "expected_request_changes": list(a.expected_request_changes)}
                 for a in ARMS.values()},
        "experiments": {e.experiment_id: {"stage": e.stage, "baseline": e.baseline,
                                          "candidate": e.candidate, "question": e.question}
                        for e in EXPERIMENTS.values()},
        "one_change_problems": one_change_problems(),
        "decision_rules": DECISION_RULES,
        "evaluation_protocol": EVALUATION_PROTOCOL,
    }
    if include_probe:
        probe = request_probe()
        out["this_process_probe_sha256"] = _digest(probe)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("describe", help="dataset, arms, rules, and protocol (offline)")
    sub.add_parser("validate", help="exit 1 when the dataset or the arms are invalid (offline)")
    sub.add_parser("probe", help="the request fields this process would send (offline)")
    p_run = sub.add_parser("run", help="run one arm in this process (paid)")
    p_run.add_argument("--arm", required=True, choices=sorted(ARMS))
    p_run.add_argument("--stage", required=True, choices=[ds.STAGE_VERIFICATION, ds.STAGE_REVIEW])
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--split", default=ds.SPLIT_HELD_OUT, choices=list(ds.SPLITS))
    p_run.add_argument("--repetition", type=int, default=1)
    p_run.add_argument("--max-spend-usd", type=float, default=None)
    p_run.add_argument("--live", action="store_true")
    p_exp = sub.add_parser("run-experiment", help="run an experiment's arms in fresh processes (paid)")
    p_exp.add_argument("--experiment", required=True, choices=sorted(EXPERIMENTS))
    p_exp.add_argument("--state-root", required=True)
    p_exp.add_argument("--out", required=True)
    p_exp.add_argument("--split", default=ds.SPLIT_HELD_OUT, choices=list(ds.SPLITS))
    p_exp.add_argument("--repetitions", type=int, default=2)
    p_exp.add_argument("--max-spend-usd", type=float, required=True)
    p_exp.add_argument("--live", action="store_true")
    p_score = sub.add_parser("score", help="score an experiment's records (offline)")
    p_score.add_argument("--experiment", required=True, choices=sorted(EXPERIMENTS))
    p_score.add_argument("--out", required=True)
    p_score.add_argument("--split", default=ds.SPLIT_HELD_OUT, choices=list(ds.SPLITS))
    p_score.add_argument("--adjudication", default=None)
    ns = parser.parse_args(argv)

    def emit(value: Any) -> None:
        json.dump(value, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")

    if ns.command in (None, "describe"):
        emit(describe())
        return 0
    if ns.command == "validate":
        problems = ds.validate_dataset() + one_change_problems()
        emit({"problems": problems})
        return 1 if problems else 0
    if ns.command == "probe":
        emit(request_probe())
        return 0
    if ns.command == "run":
        try:
            emit(run_arm(ns.arm, stage=ns.stage, out_dir=Path(ns.out), split=ns.split,
                         repetition=ns.repetition, max_spend_usd=ns.max_spend_usd, live=ns.live,
                         log=lambda m: print(m, file=sys.stderr)))
        except RunRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    if ns.command == "run-experiment":
        try:
            emit(run_experiment(ns.experiment, state_root=Path(ns.state_root), out_dir=Path(ns.out),
                                max_spend_usd=ns.max_spend_usd, split=ns.split,
                                repetitions=ns.repetitions, live=ns.live))
        except (RunRefused, ArmStateError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    if ns.command == "score":
        adjudication = None
        if ns.adjudication:
            adjudication = json.loads(Path(ns.adjudication).read_text(encoding="utf-8"))
        emit(score_experiment(ns.experiment, Path(ns.out), split=ns.split, adjudication=adjudication))
        return 0
    parser.error(f"unknown command {ns.command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
