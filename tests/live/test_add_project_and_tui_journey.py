"""Opt-in acceptance for project creation and the local terminal on this real host.

The host-local half runs unattended: it creates a scratch project through the real command,
proves a running catalogue picks it up, launches a real managed session into it, attaches by
the same argument vector the terminal execs, gracefully stops it from a second connection
standing in for the service, and removes everything it made. The Telegram half of the journey
still needs the owner, and is audited from the durable lifecycle trace rather than driven.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.application.commands import CleanupCommand, GracefulStopCommand, LaunchCommand
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import ProjectCatalogueProvider, _local_runtime, local_context
from remote_agents.config import load_config
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.production import ProductionPaths

_SCRATCH = "ra-acceptance-probe"


def _enabled() -> tuple[Path, ProductionPaths]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    paths = ProductionPaths.for_home(Path.home())
    if not paths.config_path.is_file():
        pytest.skip("BLOCKED: production config is unavailable")
    return paths.config_path, paths


@pytest.mark.live_acceptance
def test_a_created_project_reaches_a_running_catalogue_and_is_then_removed() -> None:
    """Create through the real service against the real registry, then undo it exactly."""
    config_path, _ = _enabled()
    config = load_config(config_path)
    area = next(
        item for item in FilesystemProjectWorkspace(config.dev_root).areas() if item == "infra"
    )
    registry_before = config.registry_path.read_bytes()
    provider = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    before = provider.refresh().catalogue
    assert _SCRATCH not in {project.name for project in before}

    creator = ProjectCreationService(
        FilesystemProjectWorkspace(config.dev_root),
        RegistryProjectRecorder(config.registry_path, config.dev_root, today=date.today),
    )
    created = creator.create(CreateProjectCommand(area, _SCRATCH))
    try:
        after = provider.refresh().catalogue
        entry = next(project for project in after if project.name == _SCRATCH)
        assert entry.group == "Registered"
        assert provider.paths[ProjectId(entry.opaque_id)] == created.path
        assert provider.snapshot.registry_error is None
    finally:
        _remove_scratch_entry(config.registry_path, created.path)
        created.path.rmdir()

    assert config.registry_path.read_bytes() == registry_before
    assert _SCRATCH not in {project.name for project in provider.refresh().catalogue}


@pytest.mark.live_acceptance
async def test_a_terminal_launch_attaches_and_stops_from_a_second_connection() -> None:
    """Drive the terminal's own composition against real tmux, then stop it as the bot would."""
    config_path, paths = _enabled()
    config = load_config(config_path)
    project = next(
        (
            item
            for item in ProjectCatalogueProvider(config.registry_path, config.dev_root)
            .refresh()
            .catalogue
            if item.name == "remote-agents"
        ),
        None,
    )
    if project is None:
        pytest.skip("BLOCKED: this project is not in the catalogue")
    profile = ProfileId(os.environ.get("REMOTE_AGENTS_ACCEPTANCE_PROFILE", "claude"))

    terminal_connection = open_database(paths.database_path, migrations=MIGRATIONS)
    service_connection = open_database(paths.database_path, migrations=MIGRATIONS)
    record = None
    try:
        context = local_context(config, terminal_connection, paths)
        available = {choice.profile_id for choice in context.profiles if choice.available}
        if str(profile) not in available:
            pytest.skip(f"BLOCKED: {profile} is not available on this host")

        record = await context.launcher.launch(
            LaunchCommand(
                ProjectId(project.opaque_id),
                profile,
                f"acceptance-{date.today()}-{uuid4()}",
                "acceptance",
            )
        )
        assert record.state is SessionState.RUNNING

        assert context.attach_argv(str(record.session_id)) == attach_argv(record.session_id)

        service_runtime = _local_runtime(
            config, paths, ProjectCatalogueProvider(config.registry_path, config.dev_root).paths
        )
        service = SessionService(SQLiteSessionStore(service_connection), service_runtime.terminal)
        assert record.session_id in {item.session_id for item in await service.list_sessions()}

        stopped = await service.graceful_stop(GracefulStopCommand(record.session_id, profile))
        assert stopped.preserved, "the service could not gracefully stop a terminal launch"
        await service.cleanup(CleanupCommand(record.session_id))

        final = await context.launcher.list_sessions()
        ended = next(item.state for item in final if item.session_id == record.session_id)
        assert ended is SessionState.ENDED
    finally:
        terminal_connection.close()
        service_connection.close()


def _remove_scratch_entry(registry_path: Path, project_path: Path) -> None:
    """Delete exactly the five appended lines for the scratch project and nothing else."""
    lines = registry_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(
        index for index, line in enumerate(lines) if line.strip() == f"- path: {project_path}"
    )
    assert lines[start + 1].strip() == f"name: {_SCRATCH}"
    registry_path.write_text("".join(lines[:start] + lines[start + 5 :]), encoding="utf-8")
