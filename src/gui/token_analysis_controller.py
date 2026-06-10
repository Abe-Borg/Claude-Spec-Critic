"""Token analysis (approximate) and exact token count refresh.

Runs the local cl100k_base estimate for every selected file, then kicks
off an Anthropic ``count_tokens`` call for the largest spec to replace
the gauge value with the exact API count when it returns. All UI updates
go through ``_dispatch_if_current``-style callbacks so a stale background
pass cannot overwrite fresher state.

The exact-token-count refresh is debounced via a Tk ``after``
timer so that rapid sequential file changes (multiple drops, a quick
re-browse) collapse into a single outbound API call. The cl100k_base
estimate stays visible during the debounce window; only the final state
pays for an exact ``count_tokens`` call. Stale-result protection inside
``dispatch`` is unchanged.
"""
from __future__ import annotations

import threading
from typing import NamedTuple

from ..core.code_cycles import DEFAULT_CYCLE
from ..input.extractor import ExtractedSpec, extract_text
from ..review.prompts import get_system_prompt
from ..core.tokenizer import count_tokens, exceeds_per_call_limit


# 300–500 ms recommended by the delta plan. 400 ms balances
# perceived responsiveness against absorbing typical file-toggle bursts;
# the existing project-context typing debounce (``_context_debounce_id``)
# uses 300 ms, and a slightly longer window here covers file-load churn
# (drops + browse) which is naturally slower than keystroke typing.
EXACT_TOKEN_REFRESH_DEBOUNCE_MS = 400


def resolve_initial_selection(paths, prior_selection):
    """Decide each file's initial checkbox state for a (re)load of the panel.

    A path present in ``prior_selection`` keeps its prior checked state, so a
    user's deselection survives an *accumulation* reload (loading another
    folder re-analyzes the full merged list and rebuilds every row). A
    genuinely-new path — one not in ``prior_selection`` — defaults to selected
    (``True``). On the first load ``prior_selection`` is empty, so every file
    defaults selected, matching the original behavior.

    Returns a ``{path: bool}`` map covering exactly ``paths``.
    """
    prior = prior_selection or {}
    return {p: prior.get(p, True) for p in paths}


class CallMetrics(NamedTuple):
    """Gauge / run-button / over-limit inputs derived from the selected files."""
    largest_call: int
    file_count: int
    per_file_limit_exceeded: bool
    over_files: list  # filenames whose own per-call size exceeds the limit


def compute_call_metrics(selected_data, overhead) -> CallMetrics:
    """Derive the gauge / run-button / over-limit metrics from the *selected*
    (checked) files only.

    Shared by ``analyze_tokens`` (right after a panel reload) and
    ``on_file_selection_change`` (on a checkbox toggle) so the two never
    drift: an unchecked file — including a deselection preserved across an
    accumulation reload — must not count toward the largest single-call
    estimate, the per-call-limit warning, or the Review-button gate. An empty
    selection yields zeroed metrics, so Review disables and nothing warns.

    ``selected_data`` items are the loaded file dicts (each with ``tokens`` /
    ``filename``); ``overhead`` is the system-prompt + project-context token
    cost shared by every per-spec call.
    """
    fc = len(selected_data)
    if fc == 0:
        return CallMetrics(0, 0, False, [])
    max_per_file = max(d["tokens"] for d in selected_data)
    largest_call = overhead + max_per_file
    exceeded = exceeds_per_call_limit(max_per_file, overhead)
    over_files = (
        [d["filename"] for d in selected_data if exceeds_per_call_limit(d["tokens"], overhead)]
        if exceeded
        else []
    )
    return CallMetrics(largest_call, fc, exceeded, over_files)


def select_biggest_spec(file_data, extracted_specs):
    """Return the ExtractedSpec for the largest-by-tokens entry in file_data.

    Matches on the unique ``source_path`` rather than the basename:
    accumulated folders can hold two files with the same basename (a
    CSI-numbered spec reused across projects), so a filename match could
    resolve to the wrong — possibly unchecked — duplicate and refresh the
    gauge with the exact count of a file that won't be reviewed, re-introducing
    the misleading post-reload gauge behavior. Falls back to a basename match
    only when no source-path match exists (e.g. a spec built without a
    ``source_path``). Returns None for empty input or no match.
    """
    if not file_data:
        return None
    biggest = max(file_data, key=lambda d: d["tokens"])
    biggest_path = str(biggest.get("path", ""))
    spec = next(
        (s for s in extracted_specs
         if getattr(s, "source_path", "") and s.source_path == biggest_path),
        None,
    )
    if spec is None:
        spec = next(
            (s for s in extracted_specs if s.filename == biggest.get("filename")),
            None,
        )
    return spec


def analyze_tokens(app, file_paths) -> None:
    if not file_paths:
        app.log.log_warning("No supported files found")
        app.token_gauge.reset()
        app.file_list_panel.reset()
        return
    app.log.log_step(f"Analyzing {len(file_paths)} files...")

    # Capture every UI-thread value the worker needs *before* spawning
    # the thread; reading Tkinter state from a background thread is not
    # safe.
    project_context = app._get_project_context()
    cycle = DEFAULT_CYCLE

    # Snapshot the current checkbox state (on the UI thread, before the worker
    # clears + rebuilds the panel) so an accumulation reload doesn't silently
    # re-select files the user had unchecked. Genuinely-new files still default
    # to selected. See resolve_initial_selection.
    prior_selection = app.file_list_panel.selection_state_by_path()

    # Stale-result guard: bump and capture the analysis epoch. A newer
    # analysis bumps the epoch; older threads see their captured value
    # differs from ``app._analysis_epoch`` and silently drop their
    # results.
    app._analysis_epoch += 1
    captured_epoch = app._analysis_epoch

    def _is_current() -> bool:
        return app._analysis_epoch == captured_epoch

    def _dispatch_if_current(fn):
        app.after(0, lambda: fn() if _is_current() else None)

    def analyze():
        try:
            _dispatch_if_current(lambda: app._clear_file_state())
            file_data = []
            processed_names: list[str] = []
            sys_tokens = count_tokens(get_system_prompt(cycle))
            ctx_tokens = count_tokens(project_context) if project_context else 0
            extracted_specs: list[ExtractedSpec] = []
            for f in file_paths:
                try:
                    spec = extract_text(f)
                    tokens = count_tokens(spec.content)
                    file_data.append({"path": f, "filename": spec.filename, "tokens": tokens, "content": spec.content})
                    processed_names.append(f.name)
                    extracted_specs.append(spec)
                except Exception as e:
                    _dispatch_if_current(lambda err=str(e), n=f.name: app.log.log_warning(f"Could not read {n}: {err}"))
            if processed_names:
                _dispatch_if_current(lambda names=processed_names: app.log.log_file_batch(names))
            if file_data:
                _dispatch_if_current(lambda fd=file_data, es=extracted_specs, st=sys_tokens, ct=ctx_tokens:
                    app._set_file_data(fd, es, st, ct))
                overhead = sys_tokens + ctx_tokens
                initial_selection = resolve_initial_selection(
                    [d["path"] for d in file_data], prior_selection
                )
                _dispatch_if_current(lambda fd=file_data, sel=initial_selection:
                    app.file_list_panel.load_files(fd, selection=sel))
                # Gauge / run-button / over-limit reflect only the *checked*
                # files, so a preserved-unchecked oversized file doesn't keep
                # Review disabled (or keep warning) until the user toggles a
                # box. Same computation as on_file_selection_change.
                selected_data = [d for d in file_data if initial_selection.get(d["path"], True)]
                metrics = compute_call_metrics(selected_data, overhead)
                _dispatch_if_current(lambda m=metrics: app.token_gauge.update_gauge(m.largest_call, m.file_count))
                if metrics.file_count > 0:
                    _dispatch_if_current(lambda lc=metrics.largest_call: app.log.log_success(
                        f"Token analysis complete: largest spec call ~{lc:,} tokens"))
                if metrics.over_files:
                    _dispatch_if_current(lambda of=metrics.over_files: app.log.log_warning(
                        f"File too large for single API call: {', '.join(of)}"
                    ))
                _dispatch_if_current(lambda m=metrics: app.run_button.configure(
                    state="normal" if (m.file_count > 0 and not m.per_file_limit_exceeded) else "disabled"
                ))
                _dispatch_if_current(lambda m=metrics: app.file_list_panel.set_over_limit(m.per_file_limit_exceeded))
                # After the cl100k_base estimate, kick off an exact Anthropic
                # count_tokens call for the largest *selected* spec and
                # re-render the gauge with the exact value. The local estimate
                # stays visible while the API call is in flight.
                if metrics.file_count > 0:
                    refresh_exact_token_count(
                        app, selected_data, extracted_specs, project_context, cycle,
                        sys_tokens, ctx_tokens, _dispatch_if_current,
                    )
        except Exception as e:
            _dispatch_if_current(lambda err=e: app.log.log_error(f"Analysis failed: {err}"))

    threading.Thread(target=analyze, daemon=True).start()


def refresh_exact_token_count(app, file_data, extracted_specs, project_context, cycle, sys_tokens, ctx_tokens, dispatch) -> None:
    """Run Anthropic count_tokens for the largest spec and update the gauge.

    Runs in its own background thread so the cl100k_base estimate stays
    on screen while we wait. Falls back silently to the local estimate
    when the API call fails or returns None.

    The actual API-call thread launch is debounced through
    ``app.after``. Each invocation cancels any pending timer and
    reschedules ``EXACT_TOKEN_REFRESH_DEBOUNCE_MS`` later, so a burst of
    rapid file changes produces at most one outbound API call after the
    burst settles. Already-running blocking HTTP requests are not
    cancelled — the debounce only prevents unnecessary calls from
    starting. Stale-result protection inside ``dispatch`` (the
    ``_analysis_epoch`` guard) is unchanged.

    ``count_tokens`` is called with the same model the
    review will run against. The GUI exposes the selected model via
    ``app._get_selected_model`` when available; otherwise we fall back to
    ``REVIEW_MODEL_DEFAULT`` so headless and partially-initialized callers
    still get a sensible count.
    """
    from ..core.api_config import REVIEW_MODEL_DEFAULT
    from ..core.tokenizer import count_tokens_via_api
    from ..review.prompts import get_single_spec_user_message, get_system_prompt

    selected_model = REVIEW_MODEL_DEFAULT
    model_getter = getattr(app, "_get_selected_model", None)
    if callable(model_getter):
        try:
            override = model_getter()
        except Exception:
            override = None
        if isinstance(override, str) and override:
            selected_model = override

    biggest_spec = select_biggest_spec(file_data, extracted_specs)
    if biggest_spec is None:
        return

    def _exact():
        try:
            system_prompt = get_system_prompt(cycle)
            user_message = get_single_spec_user_message(
                biggest_spec.content,
                biggest_spec.filename,
                project_context=project_context,
                cycle=cycle,
                # The GUI token gauge must measure the real
                # request, so id-tagged element overhead is reflected.
                paragraph_map=biggest_spec.paragraph_map,
            )
            from ..review.structured_schemas import review_findings_tool
            exact = count_tokens_via_api(
                model=selected_model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[review_findings_tool(model=selected_model)],
            )
            if exact is None:
                return
            fc = len(file_data)
            dispatch(lambda lc=int(exact), n=fc: app.token_gauge.update_gauge(lc, n, is_exact=True))
            dispatch(lambda lc=int(exact): app.log.log(
                f"Exact token count (API): {lc:,} tokens for largest spec",
                level="muted",
            ))
        except Exception:
            # Silent fallback — the cl100k_base estimate is already on screen.
            return

    def _launch_thread():
        # Clear the timer id before launching so the next refresh call
        # doesn't try to cancel an already-fired timer.
        app._exact_token_refresh_timer_id = None
        threading.Thread(target=_exact, daemon=True).start()

    # Cancel any pending debounce timer and reschedule. Each rapid
    # invocation slides the deadline forward — only the final state
    # ever launches the thread.
    prev_timer_id = getattr(app, "_exact_token_refresh_timer_id", None)
    if prev_timer_id is not None:
        try:
            app.after_cancel(prev_timer_id)
        except Exception:
            # Already fired or invalid id; safe to ignore — we always
            # overwrite ``_exact_token_refresh_timer_id`` immediately
            # below.
            pass
    app._exact_token_refresh_timer_id = app.after(
        EXACT_TOKEN_REFRESH_DEBOUNCE_MS, _launch_thread,
    )


def on_file_selection_change(app) -> None:
    if not app._loaded_file_data:
        return
    sel = set(app.file_list_panel.get_selected_files())
    selected_data = [d for d in app._loaded_file_data if d["path"] in sel]
    overhead = (
        getattr(app, "_system_prompt_tokens", 0)
        + getattr(app, "_project_context_tokens", 0)
    )
    metrics = compute_call_metrics(selected_data, overhead)
    app.token_gauge.update_gauge(metrics.largest_call, metrics.file_count)
    app.run_button.configure(
        state="normal" if (metrics.file_count > 0 and not metrics.per_file_limit_exceeded) else "disabled"
    )
    app.file_list_panel.set_over_limit(metrics.per_file_limit_exceeded)
    if metrics.file_count > 0 and getattr(app, "_extracted_specs", None):
        refresh_exact_token_count(
            app, selected_data, app._extracted_specs,
            app._get_project_context(), DEFAULT_CYCLE,
            getattr(app, "_system_prompt_tokens", 0),
            getattr(app, "_project_context_tokens", 0),
            lambda fn: app.after(0, fn),
        )
