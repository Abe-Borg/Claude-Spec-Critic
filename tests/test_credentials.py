"""The run's API credential stays in memory (plan WP-13, chunk S16).

A key typed into the desktop app used to be copied into
``os.environ["ANTHROPIC_API_KEY"]`` so the shared client factory could read
it; every child process then inherited it, and two runs with different keys
could swap accounts. It is now an :class:`ApiCredential` bound to the run's
threads. These tests pin:

* the credential object: never serialized, never shown;
* resolution: a bound credential wins, ``ANTHROPIC_API_KEY`` is the
  command-line path, and nothing is set and restored in the environment;
* the client factory: one client per credential, the env-keyed cache
  unchanged;
* propagation: every thread pool under ``src/`` that can reach the API
  submits through ``bind_credential`` (an AST tripwire), and a bound pool
  worker really sees the submitter's credential;
* the key checks in verification and triage honor a bound credential;
* child processes the app starts get an environment without the
  credentials, and no code under ``src/`` or ``scripts/`` writes one to the
  environment;
* the recovery CLI keeps a key-file key out of the environment.

Fake keys only; the GUI flows are in ``test_gui_run_credentials.py``.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import credentials as C
from src.core.credentials import (
    ApiCredential,
    active_credential,
    bind_credential,
    child_process_env,
    credential_from_text,
    has_api_key,
    resolve_api_key,
    run_with_credential,
    use_credential,
)
from src.review import reviewer

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "sk-ant-api03-FAKE-credential-test-key-0000"
OTHER_KEY = "sk-ant-api03-FAKE-other-account-key-1111"


# ---------------------------------------------------------------------------
# The credential object
# ---------------------------------------------------------------------------


class TestTheCredentialIsNeverSerialized:
    def test_repr_and_str_hide_the_key(self):
        credential = ApiCredential(FAKE_KEY, source="gui")
        assert FAKE_KEY not in repr(credential)
        assert FAKE_KEY not in str(credential)
        assert "redacted" in repr(credential)
        assert credential.source == "gui"

    def test_reveal_returns_the_stripped_key(self):
        assert ApiCredential(f"  {FAKE_KEY}\n").reveal() == FAKE_KEY

    def test_pickle_refuses(self):
        with pytest.raises(TypeError):
            pickle.dumps(ApiCredential(FAKE_KEY))

    def test_json_refuses_and_a_repr_fallback_is_redacted(self):
        credential = ApiCredential(FAKE_KEY)
        with pytest.raises(TypeError):
            json.dumps({"key": credential})
        assert FAKE_KEY not in json.dumps({"key": credential}, default=repr)
        assert FAKE_KEY not in json.dumps({"key": credential}, default=str)

    def test_copies_are_the_same_object(self):
        credential = ApiCredential(FAKE_KEY)
        assert copy.copy(credential) is credential
        assert copy.deepcopy({"c": credential})["c"] is credential

    def test_it_is_immutable(self):
        credential = ApiCredential(FAKE_KEY)
        with pytest.raises(AttributeError):
            credential._key = OTHER_KEY  # type: ignore[misc]

    @pytest.mark.parametrize("blank", ["", "   ", "\n", None])
    def test_a_blank_key_is_no_credential(self, blank):
        assert credential_from_text(blank) is None
        with pytest.raises(ValueError):
            ApiCredential(blank)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_the_environment_is_the_command_line_path(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER_KEY)
        assert active_credential() is None
        assert resolve_api_key() == OTHER_KEY
        assert has_api_key() is True

    def test_a_bound_credential_wins_and_is_released(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER_KEY)
        credential = ApiCredential(FAKE_KEY)
        with use_credential(credential):
            assert active_credential() is credential
            assert resolve_api_key() == FAKE_KEY
        assert active_credential() is None
        assert resolve_api_key() == OTHER_KEY

    def test_binding_never_touches_the_environment(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        before = dict(os.environ)
        with use_credential(ApiCredential(FAKE_KEY)):
            assert has_api_key() is True
            assert dict(os.environ) == before
            assert all(FAKE_KEY not in v for v in os.environ.values())
        assert dict(os.environ) == before
        assert has_api_key() is False

    def test_none_binds_nothing_and_nesting_restores(self):
        outer, inner = ApiCredential(FAKE_KEY), ApiCredential(OTHER_KEY)
        with use_credential(outer):
            with use_credential(None):
                assert active_credential() is outer
            with use_credential(inner):
                assert active_credential() is inner
            assert active_credential() is outer

    def test_a_binding_is_released_on_error(self):
        with pytest.raises(RuntimeError):
            with use_credential(ApiCredential(FAKE_KEY)):
                raise RuntimeError("boom")
        assert active_credential() is None

    def test_only_a_credential_can_be_bound(self):
        with pytest.raises(TypeError):
            with use_credential(FAKE_KEY):  # type: ignore[arg-type]
                pass

    def test_no_key_anywhere_is_an_error_naming_both_inputs(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="enter one in the app"):
            reviewer._get_api_key()


# ---------------------------------------------------------------------------
# The client factory
# ---------------------------------------------------------------------------


class _FakeAnthropic:
    constructed: list[str] = []

    def __init__(self, *, api_key: str):
        self.api_key = api_key
        self.max_retries = 2
        _FakeAnthropic.constructed.append(api_key)

    def with_options(self, **kwargs):
        view = _FakeAnthropic.__new__(_FakeAnthropic)
        view.api_key = self.api_key
        view.max_retries = kwargs.get("max_retries", self.max_retries)
        view.base = self
        return view


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setattr(reviewer, "Anthropic", _FakeAnthropic)
    monkeypatch.setattr(reviewer, "_cached_client", None)
    monkeypatch.setattr(reviewer, "_cached_key", None)
    _FakeAnthropic.constructed = []
    return _FakeAnthropic


class TestClientFactory:
    def test_a_bound_credential_builds_its_own_client_once(self, fake_sdk, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER_KEY)
        credential = ApiCredential(FAKE_KEY)
        with use_credential(credential):
            first = reviewer._get_client()
            second = reviewer._get_client()
        assert first is second
        assert first.api_key == FAKE_KEY
        assert fake_sdk.constructed == [FAKE_KEY]
        # The env-keyed module cache was never touched.
        assert reviewer._cached_client is None

    def test_the_no_retry_view_wraps_the_credentials_client(self, fake_sdk):
        credential = ApiCredential(FAKE_KEY)
        with use_credential(credential):
            base = reviewer._get_client()
            view = reviewer._get_client(sdk_retries=False)
        assert view.max_retries == 0
        assert view.base is base
        assert base.max_retries == 2

    def test_two_runs_keep_two_clients(self, fake_sdk):
        run_a, run_b = ApiCredential(FAKE_KEY), ApiCredential(OTHER_KEY)
        with use_credential(run_a):
            client_a = reviewer._get_client()
        with use_credential(run_b):
            client_b = reviewer._get_client()
        with use_credential(run_a):
            assert reviewer._get_client() is client_a
        assert client_a.api_key == FAKE_KEY and client_b.api_key == OTHER_KEY

    def test_concurrent_first_calls_build_one_client(self, fake_sdk):
        credential = ApiCredential(FAKE_KEY)
        barrier = threading.Barrier(8)
        seen: list = []

        def worker():
            barrier.wait(timeout=5)
            seen.append(reviewer._get_client())

        threads = [
            threading.Thread(target=run_with_credential(credential, worker))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert len(seen) == 8 and all(client is seen[0] for client in seen)
        assert fake_sdk.constructed == [FAKE_KEY]

    def test_two_concurrent_runs_never_mix_accounts(self, fake_sdk, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-FAKE-env-key-5555")
        runs = {"a": ApiCredential(FAKE_KEY), "b": ApiCredential(OTHER_KEY)}
        barrier = threading.Barrier(2)
        seen: dict[str, list[str]] = {"a": [], "b": []}

        def run(name):
            for _ in range(50):
                barrier.wait(timeout=5)
                seen[name].append(reviewer._get_client(sdk_retries=False).api_key)

        threads = [
            threading.Thread(target=run_with_credential(runs[name], run), args=(name,))
            for name in runs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert seen["a"] == [FAKE_KEY] * 50
        assert seen["b"] == [OTHER_KEY] * 50
        assert sorted(fake_sdk.constructed) == sorted([FAKE_KEY, OTHER_KEY])

    def test_without_a_binding_the_environment_cache_is_used(self, fake_sdk, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER_KEY)
        client = reviewer._get_client()
        assert client.api_key == OTHER_KEY
        assert reviewer._cached_client is client
        assert reviewer._cached_key == OTHER_KEY


# ---------------------------------------------------------------------------
# Propagation into thread pools
# ---------------------------------------------------------------------------


class TestPoolPropagation:
    def test_a_pool_worker_does_not_inherit_the_binding(self):
        credential = ApiCredential(FAKE_KEY)
        with use_credential(credential), ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(active_credential).result() is None

    def test_bind_credential_carries_the_submitters_credential(self):
        credential = ApiCredential(FAKE_KEY)
        with use_credential(credential), ThreadPoolExecutor(max_workers=2) as pool:
            seen = [pool.submit(bind_credential(active_credential)).result() for _ in range(4)]
        assert all(item is credential for item in seen)

    def test_the_worker_thread_is_left_clean(self):
        credential = ApiCredential(FAKE_KEY)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with use_credential(credential):
                assert pool.submit(bind_credential(active_credential)).result() is credential
            # The same worker thread, reused for a task submitted unbound.
            assert pool.submit(active_credential).result() is None

    def test_bind_credential_is_the_identity_without_a_binding(self):
        def fn():
            return 1

        assert bind_credential(fn) is fn
        assert run_with_credential(None, fn) is fn


# Pools whose tasks never reach the API: document extraction only. Anything
# added here must not import the client factory (checked below).
_NO_API_POOL_MODULES = {
    "src/input/extractor.py",
    "src/input/extraction_cache.py",
}


def _executor_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"submit", "map"}
            and node.args
        ):
            yield node


def _is_bound(arg: ast.AST) -> bool:
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Name)
        and arg.func.id == "bind_credential"
    )


class TestEveryApiPoolBindsTheCredential:
    """A pool task that skipped ``bind_credential`` would silently run on
    the environment's key — another account, or none."""

    def _python_sources(self):
        for path in sorted((ROOT / "src").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("src/tracing/"):
                continue  # the recorder's docstring example is not code
            yield rel, path
        yield "scripts/recover_batch.py", ROOT / "scripts" / "recover_batch.py"

    def test_every_submission_is_bound(self):
        unbound: list[str] = []
        found = 0
        for rel, path in self._python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for call in _executor_calls(tree):
                found += 1
                if rel in _NO_API_POOL_MODULES:
                    continue
                if not _is_bound(call.args[0]):
                    unbound.append(f"{rel}:{call.lineno}")
        assert found >= 9, "the tripwire no longer sees the app's pools"
        assert unbound == [], (
            "thread-pool submissions that can reach the API without the run's "
            f"credential: {unbound}"
        )

    @pytest.mark.parametrize("rel", sorted(_NO_API_POOL_MODULES))
    def test_the_exempt_pools_cannot_reach_the_api(self, rel):
        """An exempt module imports nothing that reaches the API: only the
        stdlib, python-docx / pypdf / lxml, and its own ``src.input``
        neighbours."""
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Name) and node.id == "_get_client":
                pytest.fail(f"{rel} names the client factory")
        reaching = {
            name for name in imported
            if name.startswith("anthropic")
            or (name.startswith("..") and not name.startswith("..input"))
            or name.startswith("src.") and not name.startswith("src.input")
        }
        assert reaching == set(), f"{rel} imports {sorted(reaching)}"


_SENSITIVE = set(C.SENSITIVE_ENV_VARS)


def _environment_writes(tree: ast.AST) -> list[int]:
    """Lines that write an Anthropic credential into the environment."""
    lines: list[int] = []

    def is_os_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and is_os_environ(target.value)
                and not (
                    isinstance(target.slice, ast.Constant)
                    and target.slice.value not in _SENSITIVE
                )
            ):
                lines.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "putenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
                lines.append(node.lineno)
            if func.attr in {"setdefault", "update"} and is_os_environ(func.value):
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Constant) and first.value not in _SENSITIVE):
                    lines.append(node.lineno)
    return lines


class TestNothingWritesTheKeyToTheEnvironment:
    def test_no_source_writes_a_credential_to_os_environ(self):
        offenders: list[str] = []
        paths = list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
        paths.append(ROOT / "main.py")
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders += [
                f"{path.relative_to(ROOT).as_posix()}:{line}"
                for line in _environment_writes(tree)
            ]
        assert offenders == []

    def test_the_tripwire_sees_a_write(self):
        tree = ast.parse(
            'import os\nos.environ["ANTHROPIC_API_KEY"] = key\n'
            "os.environ[name] = key\nos.putenv('X', 'y')\n"
            'os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", t)\n'
            'os.environ["SPEC_CRITIC_TRACE"] = "1"\n'
        )
        assert _environment_writes(tree) == [2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Verification and triage honor a bound credential
# ---------------------------------------------------------------------------


class TestKeyChecksHonorTheBinding:
    def test_verification_runs_with_only_a_bound_credential(self, monkeypatch):
        from src.verification.verifier import OUTCOME_NO_API_KEY
        from tests.fixtures.verification_drivers import message, run_realtime, search_blocks

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with use_credential(ApiCredential(FAKE_KEY)):
            result, client = run_realtime(monkeypatch, message(search_blocks()))
        assert client.calls, "the verifier refused a run whose key was bound, not in env"
        assert result.outcome != OUTCOME_NO_API_KEY

    def test_control_verification_without_any_key_still_fails(self, monkeypatch):
        from src.verification.verifier import OUTCOME_NO_API_KEY
        from tests.fixtures.verification_drivers import message, run_realtime, search_blocks

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result, client = run_realtime(monkeypatch, message(search_blocks()))
        assert client.calls == []
        assert result.outcome == OUTCOME_NO_API_KEY

    def test_triage_runs_with_only_a_bound_credential(self, monkeypatch):
        from src.verification import triage

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        reached: list = []

        def client_factory(**_kwargs):
            reached.append(active_credential())
            raise RuntimeError("stop here: the key check was passed")

        monkeypatch.setattr(triage, "_get_client", client_factory)
        with use_credential(ApiCredential(FAKE_KEY)):
            with pytest.raises(RuntimeError, match="key check was passed"):
                triage._classify_batch([SimpleNamespace()], model="claude-haiku-4-5")
        assert reached and reached[0].reveal() == FAKE_KEY
        assert triage._classify_batch([SimpleNamespace()], model="claude-haiku-4-5") == {}


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


class TestChildProcesses:
    def test_child_process_env_drops_the_credentials(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", OTHER_KEY)
        monkeypatch.setenv("SPEC_CRITIC_TRACE", "0")
        env = child_process_env()
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
        assert env["SPEC_CRITIC_TRACE"] == "0"
        # The process's own environment is untouched.
        assert os.environ["ANTHROPIC_API_KEY"] == FAKE_KEY

    def test_child_process_env_takes_a_base(self):
        env = child_process_env({"ANTHROPIC_API_KEY": FAKE_KEY, "PATH": "/bin"})
        assert env == {"PATH": "/bin"}

    def test_the_installer_is_started_without_the_credentials(self, monkeypatch, tmp_path):
        from src.core import updates

        monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
        monkeypatch.setattr(updates.sys, "platform", "linux")
        launched: list[dict] = []
        monkeypatch.setattr(
            updates.subprocess, "Popen", lambda args, **kw: launched.append(kw)
        )
        updates.spawn_installer(tmp_path / "setup.exe")
        assert len(launched) == 1
        assert "ANTHROPIC_API_KEY" not in launched[0]["env"]

    def test_every_subprocess_call_in_src_passes_a_sanitized_env(self):
        """``subprocess`` launches the app controls carry
        ``env=child_process_env()``. ``os.startfile`` and ``webbrowser``
        take no environment and are out of reach by design."""
        offenders: list[str] = []
        found = 0
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
                ):
                    continue
                found += 1
                env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
                sanitized = (
                    isinstance(env, ast.Call)
                    and isinstance(env.func, ast.Name)
                    and env.func.id == "child_process_env"
                )
                if not sanitized:
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        assert found >= 3
        assert offenders == []


# ---------------------------------------------------------------------------
# The recovery CLI
# ---------------------------------------------------------------------------


class TestRecoveryCliKey:
    def _load(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "recover_batch_for_credentials_test", ROOT / "scripts" / "recover_batch.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_key_file_key_is_bound_not_exported(self, monkeypatch):
        module = self._load()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(module, "load_api_key_from_file", lambda: f" {FAKE_KEY} ")
        seen: dict = {}

        def fake_recover(parser, ns):
            seen["credential"] = active_credential()
            seen["env"] = os.environ.get("ANTHROPIC_API_KEY")
            return 0

        monkeypatch.setattr(module, "_recover", fake_recover)
        assert module.main(["--batch-id", "msgbatch_x", "--module", "california_k12_mep"]) == 0
        assert seen["credential"].reveal() == FAKE_KEY
        assert seen["credential"].source == "key_file"
        assert seen["env"] is None
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_an_environment_key_is_used_as_is(self, monkeypatch):
        module = self._load()
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER_KEY)
        monkeypatch.setattr(
            module, "load_api_key_from_file", lambda: pytest.fail("the key file was read")
        )
        seen: dict = {}

        def fake_recover(parser, ns):
            seen["credential"] = active_credential()
            seen["key"] = resolve_api_key()
            return 0

        monkeypatch.setattr(module, "_recover", fake_recover)
        assert module.main(["--batch-id", "msgbatch_x", "--module", "california_k12_mep"]) == 0
        assert seen["credential"] is None
        assert seen["key"] == OTHER_KEY

    def test_no_key_at_all_is_a_usage_error(self, monkeypatch):
        module = self._load()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(module, "load_api_key_from_file", lambda: "")
        with pytest.raises(SystemExit) as excinfo:
            module.main(["--batch-id", "msgbatch_x", "--module", "california_k12_mep"])
        assert excinfo.value.code == 2
