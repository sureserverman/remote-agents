"""The finish-time socket cleanup kills servers only from the process that saw the whole run.

Under pytest-xdist every worker runs `pytest_sessionfinish` too. A worker's own `before` set is
taken when *it* starts, so every server another worker created since then looks like its own --
and a worker that finishes first would kill servers a sibling is still using. The controller
starts before every worker and finishes after all of them, so its `before` is the true one;
only it, or a plain non-xdist run, tears servers down.
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
    controller = SimpleNamespace(_remote_agents_sockets_before=set())
    calls = _finish(controller, monkeypatch, tmp_path)
    assert calls == [["tmux", "-L", "remote-agents-test-live", "kill-server"]]
