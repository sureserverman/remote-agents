"""Approved fake-Telegram journey over real SQLite and an isolated tmux server."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.projects.registry import RegisteredProject
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import build_catalogue
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionState


async def test_integrated_fake_journeys_use_real_sqlite_and_isolated_tmux(tmp_path: Path) -> None:
    project_path = tmp_path / "dev" / "opaque-editor"
    project_path.mkdir(parents=True)
    catalogue = build_catalogue(
        (RegisteredProject(project_path, "opaque-editor", "writing"),),
        (),
    )
    project = catalogue[0]
    terminal, gateway = _terminal(tmp_path, ProjectId(project.opaque_id))
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")), terminal
    )

    callbacks = CallbackStateStore()

    try:
        record = await service.launch(
            LaunchCommand(ProjectId(project.opaque_id), ProfileId("claude"), "launch-path")
        )
        assert inspect_capture(await _capture(gateway, record.session_id)).text.startswith("READY")

        stop = StopController(callbacks)
        token = stop.offer(record.session_id, record.profile_id, record.state, "graceful", 7, 11, 2)
        assert token is not None
        request = stop.claim(token, 7, 11, 2)
        assert request is not None
        assert await stop.execute(request, service, record)
        preserved = (await service.list_sessions())[0]
        assert preserved.state is SessionState.PRESERVED
        assert inspect_capture(await _capture(gateway, record.session_id)).text

        cleanup = StopController(callbacks)
        token = cleanup.offer(
            record.session_id, record.profile_id, preserved.state, "cleanup", 7, 11, 3
        )
        assert token is not None
        request = cleanup.claim(token, 7, 11, 3)
        assert request is not None
        assert await cleanup.execute(request, service, preserved)

        command = await service.launch(
            LaunchCommand(ProjectId(project.opaque_id), ProfileId("claude"), "force-path")
        )
        force = StopController(callbacks)
        token = force.offer(
            command.session_id, command.profile_id, command.state, "force", 7, 11, 4
        )
        assert token is not None and force.confirm_force(token, 7, 11, 4)
        request = force.claim(token, 7, 11, 4)
        assert request is not None
        assert await force.execute(request, service, command)
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.mutate("kill-session", f"ra-{record.session_id}")
            except RuntimeError:
                pass


async def _capture(gateway: TmuxGateway, session_id) -> bytes:
    return (await gateway.capture(session_id)).encode()


def _terminal(tmp_path: Path, project_id: ProjectId) -> tuple[TmuxTerminal, TmuxGateway]:
    agent = tmp_path / "fake_agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(10)\n", encoding="utf-8")
    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    profile = LaunchProfile(
        sys.executable, (sys.executable, str(agent)), {"PATH": os.environ["PATH"]}, "READY"
    )
    return TmuxTerminal(
        gateway,
        {project_id: tmp_path / "dev" / "opaque-editor"},
        {ProfileId("claude"): profile},
        startup_timeout=1,
    ), gateway
