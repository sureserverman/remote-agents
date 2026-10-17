"""The finish-time socket cleanup kills servers only from the process that saw the whole run.

Under pytest-xdist every worker runs `pytest_sessionfinish` too. A worker's own `before` set is
taken when *it* starts, so every server another worker created since then looks like its own --
and a worker that finishes first would kill servers a sibling is still using. The controller
starts before every worker and finishes after all of them, so its `before` is the true one;
only it, or a plain non-xdist run, tears servers down.

**Nor from a pytest a test runs in a child (BL-100).** A child is not a worker, so the worker
rule did not stop it, and its `before` is taken mid-run just as a worker's is: its finish killed
every server a sibling worker had started while it ran. The session that owns the sockets marks
itself at its start and exports `_SOCKET_OWNER_VARIABLE`; a session that inherits the variable
owns nothing. `tests/contract/test_nested_pytest_spares_sibling_servers.py` drives the real case.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _conftest():
    spec = importlib.util.spec_from_file_location("_root_conftest", _CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _finish(config, monkeypatch, tmp_path) -> list[list[str]]:
    module = _conftest()
    socket = tmp_path / "remote-agents-test-live"
    monkeypatch.setattr(module, "_test_sockets", lambda: {socket})
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **_: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    module.pytest_sessionfinish(SimpleNamespace(config=config), 0)
    return calls


def test_an_xdist_worker_kills_no_server_at_its_finish(monkeypatch, tmp_path) -> None:
    worker = SimpleNamespace(workerinput={"workerid": "gw0"}, _remote_agents_sockets_before=set())
    assert _finish(worker, monkeypatch, tmp_path) == []


def test_the_controller_or_a_plain_run_kills_this_runs_servers(monkeypatch, tmp_path) -> None:
    controller = SimpleNamespace(
        _remote_agents_sockets_before=set(), _remote_agents_owns_sockets=True
    )
    calls = _finish(controller, monkeypatch, tmp_path)
    assert calls == [["tmux", "-L", "remote-agents-test-live", "kill-server"]]


def test_a_child_pytest_kills_no_server_at_its_finish(monkeypatch, tmp_path) -> None:
    """Its start found the owner's variable and claimed nothing, so its finish has nothing."""
    child = SimpleNamespace(_remote_agents_sockets_before=set())
    assert _finish(child, monkeypatch, tmp_path) == []


def _start(monkeypatch, *, inherited: str | None):
    module = _conftest()
    if inherited is None:
        monkeypatch.delenv(module._SOCKET_OWNER_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(module._SOCKET_OWNER_VARIABLE, inherited)
    monkeypatch.setattr(module, "_test_sockets", lambda: set())
    config = SimpleNamespace()
    module.pytest_sessionstart(SimpleNamespace(config=config))
    return module, config


def test_the_first_session_claims_the_sockets_and_exports_the_claim(monkeypatch) -> None:
    module, config = _start(monkeypatch, inherited=None)
    assert config._remote_agents_owns_sockets is True
    assert module.os.environ[module._SOCKET_OWNER_VARIABLE]


def test_a_session_that_inherits_the_claim_neither_reaps_nor_records(monkeypatch) -> None:
    _module, config = _start(monkeypatch, inherited="4242")
    assert not hasattr(config, "_remote_agents_owns_sockets")
    assert not hasattr(config, "_remote_agents_sockets_before")
