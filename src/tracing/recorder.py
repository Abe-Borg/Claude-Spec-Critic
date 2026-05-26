"""TraceRecorder — the main orchestrator.

Owns the on-disk trace directory and a background writer thread. Public
methods (``open_span``, ``close_span``, ``add_event``, ``prompt_ref``,
``record_finding_snapshot``) enqueue work for the writer; the writer drains
the queue, serializes to JSONL, and ``fsync``s on ``stop()``.

Threading model:
    - The recorder's public methods are safe to call from any thread.
    - The writer thread is the only thread that touches file handles.
    - An ``open_spans`` dict tracks active spans; a lock guards it because
      ``open_span`` / ``close_span`` are called concurrently from worker
      threads (batch verification uses a ThreadPoolExecutor).
    - ``contextvars.ContextVar`` carries the active SpanHandle so worker
      tasks submitted via ``concurrent.futures`` inherit the parent's
      span without explicit plumbing.

Defensive behavior:
    - ``stop()`` is idempotent; called twice is a no-op.
    - The writer thread wraps its main loop in try/except — on an
      unexpected error it logs, sets a sentinel flag, and exits. Subsequent
      ``add_event`` calls are still enqueued but the queue drains nowhere
      (capture_hooks layer above swallows exceptions, so no caller breaks).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import LEVEL_DEEP, LEVEL_DEFAULT
from .redaction import scrub_data
from .spans import (
    AgentSpan,
    STATUS_ERROR,
    STATUS_OK,
    SpanHandle,
    make_event,
    make_span,
)


_log = logging.getLogger(__name__)


# Active span tracked through two complementary mechanisms:
#   1. ContextVar — set by the ``recorder.span()`` context manager. Works
#      across asyncio boundaries and propagates via ``copy_context().run``.
#   2. Thread-local stack — pushed/popped by ``open_span`` / ``close_span``
#      so capture-hook callers (which return a SpanHandle rather than
#      using a context manager) still establish a parent chain. The hooks
#      then look up ``current_span()`` and use whatever is most recent.
#
# Caveat: ``concurrent.futures.ThreadPoolExecutor.submit`` does NOT
# automatically propagate the calling thread's contextvars OR thread-local
# state to the worker. Code that submits to a thread pool and wants the
# worker to see the parent span must either pass ``parent=`` explicitly,
# or wrap the submitted callable with :func:`bind_to_current_context`
# (which snapshots the current context and runs the function inside it).
_CURRENT_SPAN: contextvars.ContextVar[SpanHandle | None] = contextvars.ContextVar(
    "spec_critic_current_span", default=None
)

_THREAD_SPAN_STACK = threading.local()


def _stack() -> list[SpanHandle]:
    stack = getattr(_THREAD_SPAN_STACK, "spans", None)
    if stack is None:
        stack = []
        _THREAD_SPAN_STACK.spans = stack
    return stack


def current_span() -> SpanHandle | None:
    """Return the active SpanHandle for this task/thread.

    ContextVar wins if set (typically by ``recorder.span()`` context
    manager). Falls back to the thread-local push/pop stack maintained by
    ``open_span`` / ``close_span`` so capture-hook callers also get a
    parent chain without needing the contextmanager.
    """
    ctx_value = _CURRENT_SPAN.get()
    if ctx_value is not None:
        return ctx_value
    stack = _stack()
    return stack[-1] if stack else None


def bind_to_current_context(fn):
    """Wrap a function so it runs in a snapshot of the current context.

    Useful for ``ThreadPoolExecutor.submit(bind_to_current_context(fn), ...)``
    so the worker thread sees the same SpanHandle as the submitter — without
    this, ``current_span()`` returns ``None`` inside the worker.
    """
    ctx = contextvars.copy_context()

    def wrapper(*args, **kwargs):
        return ctx.run(fn, *args, **kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "wrapped")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


# ---- Writer-thread sentinel --------------------------------------------
# Unique object signaling shutdown. Anything else in the queue is a
# (filename, dict) tuple destined for a JSONL line.
_SHUTDOWN_SENTINEL = object()


# Files the writer manages. Indexed by short name for clarity in
# enqueued tuples — no string typos at call sites that go through helper
# methods.
FILE_SPANS = "spans.jsonl"
FILE_EVENTS = "events.jsonl"
FILE_PROMPTS = "prompts.jsonl"
FILE_FINDINGS = "findings.jsonl"
FILE_RUN_META = "run.json"


# Queue-overflow warning threshold. Per the plan we don't drop events;
# this just surfaces a one-time warning if a pathological run accumulates
# more pending writes than the writer can drain.
_QUEUE_WARN_THRESHOLD = 100_000


class TraceRecorder:
    """One trace per ``run_id``. Multiple instantiations against the same
    ``trace_dir`` append (used by batch mode after an app restart)."""

    def __init__(
        self,
        *,
        run_id: str,
        trace_dir: Path,
        capture_level: str,
        spec_critic_version: str = "",
    ) -> None:
        self._run_id = run_id
        self._trace_dir = Path(trace_dir)
        self._capture_level = capture_level if capture_level in (LEVEL_DEFAULT, LEVEL_DEEP) else LEVEL_DEFAULT
        self._spec_critic_version = spec_critic_version

        self._queue: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._writer_alive = threading.Event()
        self._stopped = threading.Event()

        self._open_spans: dict[str, AgentSpan] = {}
        self._open_spans_lock = threading.Lock()

        # Prompt-hash dedup table. Default-level only — deep mode inlines
        # full prompts on the span.
        self._prompt_seen: set[str] = set()
        self._prompt_seen_lock = threading.Lock()

        self._queue_warned = False
        # run.json metadata held in memory so stop() can update ``ended_at``
        # without re-reading the file.
        self._run_meta: dict[str, Any] = {}

    # ---- properties ----------------------------------------------------
    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def trace_dir(self) -> Path:
        return self._trace_dir

    @property
    def capture_level(self) -> str:
        return self._capture_level

    @property
    def is_deep(self) -> bool:
        return self._capture_level == LEVEL_DEEP

    # ---- lifecycle -----------------------------------------------------
    def start(
        self,
        *,
        mode: str = "",
        model: str = "",
        cycle_label: str = "",
        files_reviewed: list[str] | None = None,
    ) -> None:
        """Spin up the writer thread and write the initial run.json.

        Safe to call multiple times against the same trace dir — the
        second call appends to the existing JSONL files and rewrites
        run.json with an updated ``resumed_at`` timestamp. Batch mode
        relies on this behavior to continue an interrupted run.
        """
        self._trace_dir.mkdir(parents=True, exist_ok=True)
        # Snapshot meta. If run.json already exists, preserve started_at
        # and append a resume timestamp so the viewer can show "started
        # X, resumed Y" for batch runs that survived an app restart.
        existing = self._read_existing_run_meta()
        now = time.time()
        if existing:
            self._run_meta = existing
            resumes = list(self._run_meta.get("resumed_at") or [])
            resumes.append(now)
            self._run_meta["resumed_at"] = resumes
            # capture_level may have changed between sessions; keep the
            # latest value so the viewer knows what's in the JSONL.
            self._run_meta["capture_level"] = self._capture_level
        else:
            self._run_meta = {
                "run_id": self._run_id,
                "started_at": now,
                "ended_at": None,
                "mode": mode,
                "model": model,
                "cycle_label": cycle_label,
                "files_reviewed": list(files_reviewed or []),
                "capture_level": self._capture_level,
                "spec_critic_version": self._spec_critic_version,
                "resumed_at": [],
            }
        self._write_run_meta_sync()

        if self._writer_thread is None or not self._writer_thread.is_alive():
            self._writer_alive.set()
            self._stopped.clear()
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name=f"spec-critic-trace-writer-{self._run_id}",
                daemon=True,
            )
            self._writer_thread.start()

    def stop(self, *, flush_timeout: float = 5.0) -> None:
        """Drain the writer queue and close files.

        Idempotent. Updates run.json with ``ended_at``. Logs (but does not
        raise) if the writer thread fails to drain within ``flush_timeout``.

        ``ended_at`` marks the end of the *automated pipeline* (review →
        verification → cross-check → finalize), which is where the GUI tears
        the recorder down. Report export and edit application run afterward on
        the UI thread — behind a file-save dialog and user-driven edit
        selection — so they are intentionally NOT inside the trace window;
        otherwise ``ended_at`` would absorb open-ended user think-time. A
        ``run.json`` whose ``ended_at`` predates the on-disk report's mtime is
        therefore expected, not a truncated trace.
        """
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._run_meta["ended_at"] = time.time()
        try:
            self._write_run_meta_sync()
        except Exception as exc:
            _log.warning("Failed to update run.json on stop: %s", exc)

        self._queue.put(_SHUTDOWN_SENTINEL)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=flush_timeout)
            if self._writer_thread.is_alive():
                _log.warning(
                    "Trace writer thread did not drain within %.1fs; %d items remain",
                    flush_timeout,
                    self._queue.qsize(),
                )
        self._writer_alive.clear()

    # ---- public capture surface ----------------------------------------
    def open_span(
        self,
        kind: str,
        name: str,
        *,
        parent: SpanHandle | None = None,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SpanHandle:
        if parent is None:
            parent = current_span()
        span = make_span(
            kind=kind,
            name=name,
            run_id=self._run_id,
            parent_span_id=parent.span_id if parent else None,
            inputs=inputs,
            metadata=metadata,
        )
        with self._open_spans_lock:
            self._open_spans[span.span_id] = span
        handle = SpanHandle(
            span_id=span.span_id,
            kind=span.kind,
            started_at=span.started_at,
            parent_span_id=span.parent_span_id,
        )
        # Push onto the thread-local stack so subsequent open_span calls
        # without an explicit parent see this span as the parent. Strict
        # LIFO discipline is the norm but the close handler is lenient if
        # spans are closed out of order.
        _stack().append(handle)
        return handle

    def close_span(
        self,
        handle: SpanHandle,
        *,
        outputs: dict[str, Any] | None = None,
        status: str = STATUS_OK,
        error: str | None = None,
    ) -> None:
        with self._open_spans_lock:
            span = self._open_spans.pop(handle.span_id, None)
        if span is None:
            # Span closed twice or unknown handle — log and skip rather
            # than raising so a botched capture site can't crash the
            # pipeline.
            _log.debug("close_span called on unknown span_id=%s", handle.span_id)
            return
        span.ended_at = time.time()
        span.status = status
        span.error = error
        if outputs:
            # Late-bound outputs merge with anything the caller stamped
            # via add_event(..., type='note', ...) shenanigans — explicit
            # outputs win.
            span.outputs.update(outputs)
        # Lenient stack pop: prefer LIFO, but tolerate out-of-order closes
        # so a span that crossed function boundaries (batch mode) doesn't
        # corrupt the stack when its peer eventually closes.
        stack = _stack()
        for i in range(len(stack) - 1, -1, -1):
            if stack[i].span_id == handle.span_id:
                stack.pop(i)
                break
        self._enqueue(FILE_SPANS, scrub_data(span.to_jsonl_dict()))

    def add_event(self, handle: SpanHandle | None, type: str, **fields: Any) -> None:
        """Append one event to events.jsonl.

        ``handle`` may be ``None`` for orphan events that don't belong to
        a specific span — they ride out tagged with the run_id only.
        """
        span_id = handle.span_id if handle is not None else self._run_id
        event = make_event(span_id=span_id, type=type, fields=fields)
        self._enqueue(FILE_EVENTS, scrub_data(event))

    def prompt_ref(self, kind: str, text: str) -> dict[str, Any]:
        """Return either a content-hash reference or the inline text.

        Default mode writes ``text`` to prompts.jsonl (deduped) and
        returns ``{"ref": hash, "kind": kind}``. Deep mode skips the
        sidecar and returns ``{"inline": text}`` so the span is
        self-contained for replay.
        """
        if self._capture_level == LEVEL_DEEP:
            return {"inline": text}
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]
        with self._prompt_seen_lock:
            already = digest in self._prompt_seen
            if not already:
                self._prompt_seen.add(digest)
        if not already:
            self._enqueue(
                FILE_PROMPTS,
                {"hash": digest, "kind": kind, "text": text},
            )
        return {"ref": digest, "kind": kind}

    def record_finding_snapshot(self, finding: Any) -> None:
        """Serialize one Finding to findings.jsonl at its terminal state.

        Uses ``dataclasses.asdict`` recursively to capture nested fields
        (verification, locator_evidence, etc.). Non-dataclass fields fall
        back to ``repr`` so the line stays JSON-serializable even when a
        field's type doesn't have a clean JSON form.
        """
        try:
            payload = self._finding_to_dict(finding)
        except Exception as exc:
            _log.debug("Failed to serialize finding snapshot: %s", exc)
            payload = {"finding_id": getattr(finding, "finding_id", None), "error": str(exc)}
        self._enqueue(FILE_FINDINGS, scrub_data(payload))

    @contextmanager
    def span(
        self,
        kind: str,
        name: str,
        *,
        parent: SpanHandle | None = None,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[SpanHandle]:
        """Context manager that auto-closes the span and threads ContextVar."""
        handle = self.open_span(kind, name, parent=parent, inputs=inputs, metadata=metadata)
        token = _CURRENT_SPAN.set(handle)
        try:
            yield handle
        except Exception as exc:
            self.close_span(handle, status=STATUS_ERROR, error=str(exc))
            _CURRENT_SPAN.reset(token)
            raise
        else:
            self.close_span(handle, status=STATUS_OK)
            _CURRENT_SPAN.reset(token)

    # ---- internals -----------------------------------------------------
    def _enqueue(self, filename: str, payload: dict[str, Any]) -> None:
        if self._stopped.is_set():
            return
        self._queue.put((filename, payload))
        if not self._queue_warned and self._queue.qsize() > _QUEUE_WARN_THRESHOLD:
            self._queue_warned = True
            _log.warning(
                "Trace queue has %d pending writes — writer thread may be falling behind",
                self._queue.qsize(),
            )

    def _writer_loop(self) -> None:
        """Drain the queue, one JSONL line per item, until sentinel."""
        try:
            with self._open_writers() as writers:
                while True:
                    item = self._queue.get()
                    if item is _SHUTDOWN_SENTINEL:
                        # Drain anything queued before the sentinel.
                        # _open_writers context manager will fsync on exit.
                        break
                    filename, payload = item
                    writer = writers.get(filename)
                    if writer is None:
                        _log.debug("Unknown trace file %s; dropping line", filename)
                        continue
                    try:
                        line = json.dumps(payload, ensure_ascii=False, default=_json_default)
                        writer.write(line)
                        writer.write("\n")
                    except Exception as exc:
                        _log.warning("Failed to write trace line to %s: %s", filename, exc)
        except Exception as exc:
            _log.error("Trace writer thread crashed: %s", exc, exc_info=True)
        finally:
            self._writer_alive.clear()

    @contextmanager
    def _open_writers(self) -> Iterator[dict[str, Any]]:
        # Open in append mode so a second start() against the same dir
        # continues an existing trace rather than truncating.
        handles: dict[str, Any] = {}
        try:
            handles[FILE_SPANS] = (self._trace_dir / FILE_SPANS).open("a", encoding="utf-8")
            handles[FILE_EVENTS] = (self._trace_dir / FILE_EVENTS).open("a", encoding="utf-8")
            handles[FILE_FINDINGS] = (self._trace_dir / FILE_FINDINGS).open("a", encoding="utf-8")
            if self._capture_level == LEVEL_DEFAULT:
                handles[FILE_PROMPTS] = (self._trace_dir / FILE_PROMPTS).open("a", encoding="utf-8")
            yield handles
        finally:
            for fh in handles.values():
                try:
                    fh.flush()
                    import os
                    os.fsync(fh.fileno())
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass

    def _read_existing_run_meta(self) -> dict[str, Any] | None:
        path = self._trace_dir / FILE_RUN_META
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.debug("Could not parse existing run.json (%s); starting fresh", exc)
            return None

    def _write_run_meta_sync(self) -> None:
        """run.json is small and read on every resume, so write it
        synchronously rather than via the writer queue."""
        self._trace_dir.mkdir(parents=True, exist_ok=True)
        path = self._trace_dir / FILE_RUN_META
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(scrub_data(self._run_meta), indent=2), encoding="utf-8")
        tmp.replace(path)

    def _finding_to_dict(self, finding: Any) -> dict[str, Any]:
        """Best-effort dataclass-to-dict conversion."""
        if is_dataclass(finding):
            return asdict(finding)
        # Fallback: pick known attribute names. Keeps the snapshot
        # useful even when called on a duck-typed shim in tests.
        fields = (
            "finding_id",
            "severity",
            "section",
            "issue",
            "codeReference",
            "actionType",
            "existingText",
            "replacementText",
            "evidenceElementId",
            "suppression_reason",
            "demotion_reason",
        )
        out: dict[str, Any] = {}
        for key in fields:
            if hasattr(finding, key):
                out[key] = getattr(finding, key)
        verification = getattr(finding, "verification", None)
        if verification is not None:
            if is_dataclass(verification):
                out["verification"] = asdict(verification)
            else:
                out["verification"] = repr(verification)
        return out


def _json_default(obj: Any) -> Any:
    """Fallback serializer for ``json.dumps``.

    Dataclasses become dicts; sets become sorted lists; pathlib paths and
    everything else fall back to ``repr`` so a line never fails to
    serialize for one weird field.
    """
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


# ---- module-level recorder singleton -----------------------------------
_RECORDER: TraceRecorder | None = None
_RECORDER_LOCK = threading.Lock()


def get_recorder() -> TraceRecorder | None:
    return _RECORDER


def set_recorder(recorder: TraceRecorder | None) -> None:
    """Install (or clear) the global recorder.

    Called once at run start by the pipeline entry; safe to call again on
    batch resume with a fresh TraceRecorder pointing at the same trace
    directory.
    """
    global _RECORDER
    with _RECORDER_LOCK:
        _RECORDER = recorder
