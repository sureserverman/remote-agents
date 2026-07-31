"""Dedicated-socket startup readiness outcomes using harmless fake agents."""

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


@pytest.mark.parametrize(
    ("mode", "expected_live"),
    (("ready", True), ("delayed", True), ("immediate_exit", False), ("invalid_intent", False)),
)
async def test_terminal_launch_reports_real_readiness(
    tmp_path: Path, mode: str, expected_live: bool
) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=0.3, mode=mode)
    session_id = SessionId.new()
    try:
        if mode == "invalid_intent":
            terminal.invalidate_next_intent = True

        observation = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

        assert observation.live is expected_live
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_terminal_launch_times_out_without_claiming_readiness(tmp_path: Path) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=0.01, mode="delayed")
    session_id = SessionId.new()
    try:
        observation = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

        assert observation.live is False
        assert observation.detail == "startup_timeout"
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_terminal_rechecks_a_timed_out_launch_before_recovering_it(tmp_path: Path) -> None:
    terminal, gateway = make_terminal(tmp_path, timeout=0.01, mode="delayed")
    session_id = SessionId.new()
    try:
        launched = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))
        assert not launched.live
        assert not (await terminal.confirm_ready(session_id, ProfileId("fake"))).live

        await asyncio.sleep(0.06)

        assert (await terminal.confirm_ready(session_id, ProfileId("fake"))).live
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_terminal_builds_a_profile_for_the_actual_generated_session(tmp_path: Path) -> None:
    agent = tmp_path / "fake_agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(1)\n", encoding="utf-8")
    session_id = SessionId.new()
    created_for: list[SessionId] = []

    def profile_factory(received_session_id: SessionId) -> LaunchProfile:
        created_for.append(received_session_id)
        return LaunchProfile(
            sys.executable,
            (sys.executable, str(agent), f"ra-{received_session_id}"),
            {"PATH": os.environ["PATH"]},
            "READY",
        )

    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): tmp_path},
        {},
        startup_timeout=0.3,
        profile_factories={ProfileId("fake"): profile_factory},
    )
    try:
        launched = await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))

        assert launched.live
        assert created_for == [session_id]
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


def make_terminal(
    tmp_path: Path, *, timeout: float, mode: str = "ready"
) -> tuple[TmuxTerminal, TmuxGateway]:
    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        "import sys, time\n"
        "if sys.argv[1] == 'delayed': time.sleep(0.05)\n"
        "if sys.argv[1] != 'immediate_exit': print('READY', flush=True); time.sleep(1)\n",
        encoding="utf-8",
    )
    socket = f"remote-agents-test-{uuid4().hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    profile = LaunchProfile(
        sys.executable,
        (sys.executable, str(agent), mode),
        {"PATH": os.environ["PATH"]},
        "READY",
    )
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): tmp_path},
        {ProfileId("fake"): profile},
        startup_timeout=timeout,
    )
    return terminal, gateway
