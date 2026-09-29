"""The run's Anthropic API credential, held in memory only (plan WP-13).

A key typed into the desktop app used to be copied into
``os.environ["ANTHROPIC_API_KEY"]`` so the shared client factory could read
it. That made the key process-global: every child process the app starts
inherited it, and two runs with different keys could swap accounts under
each other. It is now an :class:`ApiCredential` the GUI captures when a run
starts and binds to the run's worker threads; nothing writes it to the
environment.

How a request finds its key (``reviewer._get_client`` and
:func:`resolve_api_key`):

1. the credential bound to the current thread's context by
   :func:`use_credential` — the GUI binds the run's credential around each of
   its worker threads, and each credential builds and keeps its own client,
   so a run keeps the client it started with;
2. otherwise ``ANTHROPIC_API_KEY`` from the environment — the command-line
   path (``scripts/recover_batch.py``, headless callers, tests), unchanged.

A worker thread does not inherit its submitter's context, so every thread
pool that can reach the API starts its tasks through :func:`bind_credential`
(which captures the submitter's credential when the task is submitted), and
a GUI thread is started through :func:`run_with_credential`.
``tests/test_credentials.py`` fails when an executor submission under
``src/`` does neither.

The credential is never serialized: it refuses ``pickle``, its ``repr`` hides
the key, and ``json.dumps`` rejects it, so it cannot reach saved state, a
report, or a trace by accident. Stdlib-only.
"""
from __future__ import annotations

import contextvars
import functools
import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, TypeVar

ENV_API_KEY = "ANTHROPIC_API_KEY"

# Credentials the Anthropic SDK reads from the environment. A child process
# the app starts has no use for either, so :func:`child_process_env` drops
# them.
SENSITIVE_ENV_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

T = TypeVar("T")


class ApiCredential:
    """One Anthropic API key and the client built for it, never serialized.

    ``source`` says where the key came from (``"gui"``, ``"key_file"``, …)
    for log lines; the key itself is only returned by :meth:`reveal`.
    """

    __slots__ = ("_key", "_source", "_client", "_lock")

    def __init__(self, key: str, *, source: str = "gui") -> None:
        cleaned = (key or "").strip()
        if not cleaned:
            raise ValueError("An API credential needs a non-empty key.")
        object.__setattr__(self, "_key", cleaned)
        object.__setattr__(self, "_source", str(source or ""))
        object.__setattr__(self, "_client", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ApiCredential is immutable")

    @property
    def source(self) -> str:
        return self._source

    def reveal(self) -> str:
        """The key itself, for the one place that hands it to the SDK."""
        return self._key

    def client(self, factory: Callable[[str], T]) -> T:
        """This credential's client, built by ``factory(key)`` on first use.

        One client per credential, kept for the credential's life, so every
        request of a run goes through the client the run started with.
        """
        with self._lock:
            if self._client is None:
                object.__setattr__(self, "_client", factory(self._key))
            return self._client

    def __repr__(self) -> str:
        return f"ApiCredential(source={self._source!r}, key=<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("An API credential is never serialized.")

    def __getstate__(self) -> Any:
        raise TypeError("An API credential is never serialized.")

    def __copy__(self) -> "ApiCredential":
        return self

    def __deepcopy__(self, memo: dict) -> "ApiCredential":
        return self


_ACTIVE: contextvars.ContextVar[ApiCredential | None] = contextvars.ContextVar(
    "spec_critic_api_credential", default=None
)


def credential_from_text(key: str | None, *, source: str = "gui") -> ApiCredential | None:
    """An :class:`ApiCredential` for ``key``, or ``None`` when it is blank."""
    cleaned = (key or "").strip()
    return ApiCredential(cleaned, source=source) if cleaned else None


def active_credential() -> ApiCredential | None:
    """The credential bound to the current context, if any."""
    return _ACTIVE.get()


@contextmanager
def use_credential(credential: ApiCredential | None) -> Iterator[ApiCredential | None]:
    """Bind ``credential`` for the duration of the block.

    ``None`` binds nothing: an outer binding (or the environment) stays in
    force. Leaving the block restores the prior binding even on error.
    """
    if credential is None:
        yield None
        return
    if not isinstance(credential, ApiCredential):
        raise TypeError("use_credential takes an ApiCredential or None")
    token = _ACTIVE.set(credential)
    try:
        yield credential
    finally:
        _ACTIVE.reset(token)


def run_with_credential(credential: ApiCredential | None, fn: Callable[..., T]) -> Callable[..., T]:
    """``fn`` wrapped to run under ``credential`` (unchanged when ``None``)."""
    if credential is None:
        return fn

    @functools.wraps(fn)
    def bound(*args: Any, **kwargs: Any) -> T:
        with use_credential(credential):
            return fn(*args, **kwargs)

    return bound


def bind_credential(fn: Callable[..., T]) -> Callable[..., T]:
    """``fn`` wrapped to run under the credential bound *now*.

    Call it where a task is submitted to a thread pool: the pool's worker
    thread does not see the submitter's context, so without it the task
    would fall back to the environment's key — a different account, or
    none.
    """
    return run_with_credential(_ACTIVE.get(), fn)


def resolve_api_key() -> str:
    """The key a request made now would use: the bound credential's, else
    ``ANTHROPIC_API_KEY`` (unmodified), else ``""``."""
    credential = _ACTIVE.get()
    if credential is not None:
        return credential.reveal()
    return os.environ.get(ENV_API_KEY) or ""


def has_api_key() -> bool:
    """Whether a request made now would have a key."""
    return bool(resolve_api_key())


def child_process_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without the Anthropic credentials.

    For the child processes the app starts itself (``subprocess``); a child
    never needs the key. ``os.startfile`` and ``webbrowser`` take no
    environment and inherit the process's own, which the app no longer adds
    a key to.
    """
    env = dict(os.environ if base is None else base)
    for name in SENSITIVE_ENV_VARS:
        env.pop(name, None)
    return env
