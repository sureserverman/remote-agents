"""Opt-in acceptance: the whole managed journey driven from the local terminal alone.

This is the parity claim exercised against real tmux on the owner's host, above the
hand-written doubles every other tier uses. It launches through a composition standing in
for the service, then does everything else through the terminal's own composition on its own
connection — list, detail, copy attach, inspect, graceful stop, cleanup — and force-stops a
second session it started itself.

Two safety rules this file follows, because it runs against the owner's real database and
real tmux server:

- It only ever acts on sessions **it launched in this run**, identified by session id. It
  never lists-and-stops, and it never touches a session that was already running.
- Every session it starts is cleaned up in a `finally`, including on assertion failure.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required. Without it every test here skips, because
running it unattended would create and destroy real agent panes.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    LaunchCommand,
)
from remote_agents.application.services import SessionService
from remote_agents.application.session_actions import available_actions, explain_state
from remote_agents.bootstrap import ProjectCatalogueProvider, _local_runtime, local_context
from remote_agents.config import load_config
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.ports.terminal_text import sanitize_terminal_text
from remote_agents.production import ProductionPaths


def _enabled() -> tuple[Path, ProductionPaths]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    paths = ProductionPaths.for_home(Path.home())
    if not paths.config_path.is_file():
        pytest.skip("BLOCKED: production config is unavailable")
    return paths.config_path, paths


def _key(prefix: str) -> str:
    """A fresh idempotency key per run; a date-based one refuses a same-day re-run."""
    return f"{prefix}-{date.today()}-{uuid4()}"


def _this_project(config):
    return next(
        (
            item
            for item in ProjectCatalogueProvider(config.registry_path, config.dev_root)
            .refresh()
            .catalogue
            if item.name == "remote-agents"
        ),
        None,
    )


def _service(config, paths, connection) -> SessionService:
    """A composition standing in for the running service, on its own connection.

    The provider is refreshed before its `paths` are read: the routing table is empty until
    then, and a terminal built on an empty one cannot resolve any project's directory, so
    every launch through it fails immediately.
    """
    provider = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    provider.refresh()
    runtime = _local_runtime(config, paths, provider.paths)
    return SessionService(SQLiteSessionStore(connection), runtime.terminal)


@pytest.mark.live_acceptance
async def test_the_terminal_manages_a_session_the_service_started(tmp_path: Path) -> None:
    """List, detail, copy attach, inspect, graceful stop and clean up — terminal-side only."""
    config_path, paths = _enabled()
    config = load_config(config_path)
    project = _this_project(config)
    if project is None:
        pytest.skip("BLOCKED: this project is not in the catalogue")
    profile = ProfileId(os.environ.get("REMOTE_AGENTS_ACCEPTANCE_PROFILE", "claude"))

    terminal_connection = open_database(paths.database_path, migrations=MIGRATIONS)
    service_connection = open_database(paths.database_path, migrations=MIGRATIONS)
    record = None
    service = None
    try:
        context = local_context(config, terminal_connection, paths)
        if str(profile) not in {c.profile_id for c in context.profiles if c.available}:
            pytest.skip(f"BLOCKED: {profile} is not available on this host")

        service = _service(config, paths, service_connection)
        record = await service.launch(
            LaunchCommand(
                ProjectId(project.opaque_id), profile, _key("parity"), "tui-parity"
            )
        )
        assert record.state is SessionState.RUNNING

        # 1. The terminal sees a session it did not start.
        listed = await context.launcher.list_sessions()
        mine = next(item for item in listed if item.session_id == record.session_id)

        # 2. Detail: state and an explanation for it.
        assert explain_state(mine.state)

        # 3. Copy attach, byte for byte what the owner would paste.
        command = await context.launcher.copy_attach(record.session_id)
        assert command == " ".join(attach_argv(record.session_id))

        # 4. Inspect, through the shared sanitizer.
        assert context.capture is not None, "the terminal composition wired no capture"
        captured = await context.capture(record.session_id)
        text = sanitize_terminal_text(captured.encode(), max_lines=2000, max_bytes=512 * 1024)
        assert isinstance(text, str)

        # 5. The policy allows a graceful stop from RUNNING, and the terminal issues one.
        assert "graceful" in available_actions(mine.state)
        stopped = await context.launcher.graceful_stop(
            GracefulStopCommand(record.session_id, profile)
        )
        assert stopped.preserved, "the terminal could not gracefully stop a service launch"

        # 6. Cleanup is what PRESERVED offers, and it retires the session.
        preserved = next(
            item.state
            for item in await context.launcher.list_sessions()
            if item.session_id == record.session_id
        )
        assert "cleanup" in available_actions(preserved)
        await context.launcher.cleanup(CleanupCommand(record.session_id))

        final = next(
            item.state
            for item in await service.list_sessions()
            if item.session_id == record.session_id
        )
        assert final is SessionState.ENDED
        record = None
    finally:
        await _retire(service, record)
        terminal_connection.close()
        service_connection.close()


@pytest.mark.live_acceptance
async def test_the_terminal_force_stops_a_second_session_it_started() -> None:
    """Force is destructive, so it is proved on a session this test started, and only that one."""
    config_path, paths = _enabled()
    config = load_config(config_path)
    project = _this_project(config)
    if project is None:
        pytest.skip("BLOCKED: this project is not in the catalogue")
    profile = ProfileId(os.environ.get("REMOTE_AGENTS_ACCEPTANCE_PROFILE", "claude"))

    terminal_connection = open_database(paths.database_path, migrations=MIGRATIONS)
    service_connection = open_database(paths.database_path, migrations=MIGRATIONS)
    record = None
    service = None
    try:
        context = local_context(config, terminal_connection, paths)
        if str(profile) not in {c.profile_id for c in context.profiles if c.available}:
            pytest.skip(f"BLOCKED: {profile} is not available on this host")

        service = _service(config, paths, service_connection)
        record = await service.launch(
            LaunchCommand(
                ProjectId(project.opaque_id), profile, _key("force"), "tui-force"
            )
        )
        assert record.state is SessionState.RUNNING
        assert "force" in available_actions(record.state)

        await context.launcher.force_stop(ForceStopCommand(record.session_id))

        final = next(
            item.state
            for item in await service.list_sessions()
            if item.session_id == record.session_id
        )
        assert final is SessionState.ENDED, "force stop did not retire the session"
        record = None
    finally:
        await _retire(service, record)
        terminal_connection.close()
        service_connection.close()


@pytest.mark.live_acceptance
async def test_the_terminal_lists_resume_capable_agents_without_resuming_anything() -> None:
    """Resume's read half is safe to exercise; starting a real resumed agent is not.

    Resuming would create a second live pane against a real conversation. The catalogue and
    capability reads are what this can prove unattended; the resume itself stays an owner
    step, recorded in the acceptance document.
    """
    config_path, paths = _enabled()
    config = load_config(config_path)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    try:
        context = local_context(config, connection, paths)
        assert context.conversations is not None, "the terminal composition wired no conversations"
        capabilities = await context.conversations.capabilities()
        assert capabilities, "no profile reported a resume capability on this host"
        # Every capability is truthful about itself rather than assumed available.
        for capability in capabilities:
            if not capability.catalogue_available:
                assert capability.reason, f"{capability.profile_id} is unavailable with no reason"
    finally:
        connection.close()


async def _retire(service: SessionService | None, record) -> None:
    """Leave nothing running, whatever failed above."""
    if service is None or record is None:
        return
    try:
        await service.force_stop(ForceStopCommand(record.session_id))
    except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
        pass
