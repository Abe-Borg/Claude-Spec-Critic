"""Token analysis (approximate) and exact token count refresh.

Runs the local cl100k_base estimate for every selected file, then kicks
off an Anthropic ``count_tokens`` call for the largest spec to replace
the gauge value with the exact API count when it returns. All UI updates
go through ``_dispatch_if_current``-style callbacks so a stale background
pass cannot overwrite fresher state.

Chunk D2.2: the exact-token-count refresh is debounced via a Tk ``after``
timer so that rapid sequential file changes (multiple drops, a quick
re-browse) collapse into a single outbound API call. The cl100k_base
estimate stays visible during the debounce window; only the final state
pays for an exact ``count_tokens`` call. Stale-result protection inside
``dispatch`` is unchanged.
"""
from __future__ import annotations

import threading

from .code_cycles import DEFAULT_CYCLE
from .extractor import ExtractedSpec, extract_text
from .prompts import get_system_prompt
from .tokenizer import exceeds_per_call_limit


# Chunk D2.2: 300–500 ms recommended by the delta plan. 400 ms balances
# perceived responsiveness against absorbing typical file-toggle bursts;
# the existing project-context typing debounce (``_context_debounce_id``)
# uses 300 ms, and a slightly longer window here covers file-load churn
# (drops + browse) which is naturally slower than keystroke typing.
EXACT_TOKEN_REFRESH_DEBOUNCE_MS = 400


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
            from tiktoken import get_encoding
            enc = get_encoding("cl100k_base")
            sys_tokens = len(enc.encode(get_system_prompt(cycle)))
            ctx_tokens = len(enc.encode(project_context)) if project_context else 0
            extracted_specs: list[ExtractedSpec] = []
            for f in file_paths:
                try:
                    spec = extract_text(f)
                    tokens = len(enc.encode(spec.content))
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
                max_per_file = max(d["tokens"] for d in file_data)
                largest_call = overhead + max_per_file
                per_file_limit_exceeded = exceeds_per_call_limit(max_per_file, overhead)
                _dispatch_if_current(lambda fd=file_data: app.file_list_panel.load_files(fd))
                _dispatch_if_current(lambda lc=largest_call, fc=len(file_data): app.token_gauge.update_gauge(lc, fc))
                _dispatch_if_current(lambda lc=largest_call: app.log.log_success(f"Token analysis complete: largest spec call ~{lc:,} tokens"))
                if per_file_limit_exceeded:
                    over_files = [d["filename"] for d in file_data if exceeds_per_call_limit(d["tokens"], overhead)]
                    _dispatch_if_current(lambda of=over_files: app.log.log_warning(
                        f"File too large for single API call: {', '.join(of)}"
                    ))
                _dispatch_if_current(lambda b=per_file_limit_exceeded: app.run_button.configure(
                    state="disabled" if b else "normal"
                ))
                _dispatch_if_current(lambda b=per_file_limit_exceeded: app.file_list_panel.set_over_limit(b))
                # After the cl100k_base estimate, kick off an exact
                # Anthropic count_tokens call for the largest spec and
                # re-render the gauge with the exact value. The local
                # estimate stays visible while the API call is in flight.
                refresh_exact_token_count(
                    app, file_data, extracted_specs, project_context, cycle,
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

    Chunk D2.2: the actual API-call thread launch is debounced through
    ``app.after``. Each invocation cancels any pending timer and
    reschedules ``EXACT_TOKEN_REFRESH_DEBOUNCE_MS`` later, so a burst of
    rapid file changes produces at most one outbound API call after the
    burst settles. Already-running blocking HTTP requests are not
    cancelled — the debounce only prevents unnecessary calls from
    starting. Stale-result protection inside ``dispatch`` (the
    ``_analysis_epoch`` guard) is unchanged.

    Chunk E directive 2: ``count_tokens`` is called with the same model the
    review will run against. The GUI exposes the selected model via
    ``app._get_selected_model`` when available; otherwise we fall back to
    ``REVIEW_MODEL_DEFAULT`` so headless and partially-initialized callers
    still get a sensible count.
    """
    from .api_config import REVIEW_MODEL_DEFAULT
    from .tokenizer import count_tokens_via_api
    from .prompts import get_single_spec_user_message, get_system_prompt
    from .review_modes import DEFAULT_REVIEW_MODE

    selected_model = REVIEW_MODEL_DEFAULT
    model_getter = getattr(app, "_get_selected_model", None)
    if callable(model_getter):
        try:
            override = model_getter()
        except Exception:
            override = None
        if isinstance(override, str) and override:
            selected_model = override

    biggest = max(file_data, key=lambda d: d["tokens"])
    biggest_spec = next((s for s in extracted_specs if s.filename == biggest["filename"]), None)
    if biggest_spec is None:
        return

    def _exact():
        try:
            system_prompt = get_system_prompt(cycle, mode=DEFAULT_REVIEW_MODE)
            user_message = get_single_spec_user_message(
                biggest_spec.content,
                biggest_spec.filename,
                project_context=project_context,
                cycle=cycle,
                mode=DEFAULT_REVIEW_MODE,
                # Chunk K2: the GUI token gauge must measure the real
                # request, so id-tagged element overhead is reflected.
                paragraph_map=biggest_spec.paragraph_map,
            )
            exact = count_tokens_via_api(
                model=selected_model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
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
    fc = len(selected_data)
    if fc > 0:
        max_per_file = max(d["tokens"] for d in selected_data)
        largest_call = overhead + max_per_file
        per_file_exceeded = exceeds_per_call_limit(max_per_file, overhead)
    else:
        largest_call = 0
        per_file_exceeded = False
    app.token_gauge.update_gauge(largest_call, fc)
    app.run_button.configure(state="normal" if (fc > 0 and not per_file_exceeded) else "disabled")
    app.file_list_panel.set_over_limit(per_file_exceeded)
