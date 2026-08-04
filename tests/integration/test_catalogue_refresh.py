"""Integration tests for runtime catalogue refresh after a project is created."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.application.commands import InspectQuery, LaunchCommand
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import ProjectCatalogueProvider
from remote_agents.domain.models import ProfileId, ProjectId


class _StubRunner:
    """Terminal gateway runner that is never reached by these path-resolution tests."""

    async def run(self, *argv: str) -> str:
        raise AssertionError("no tmux command should run in a catalogue refresh test")


_REGISTRY = """version: 1
projects:
  - path: {existing}
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


@pytest.fixture
def dev_root(tmp_path: Path) -> Path:
    root = tmp_path / "dev"
    (root / "infra" / "existing").mkdir(parents=True)
    (root / "dev-area").mkdir(parents=True)
    return root


@pytest.fixture
def registry_path(tmp_path: Path, dev_root: Path) -> Path:
    path = tmp_path / "projects-registry.yaml"
    path.write_text(
        _REGISTRY.format(existing=dev_root / "infra" / "existing"),
        encoding="utf-8",
    )
    return path


def _service(dev_root: Path, registry_path: Path) -> ProjectCreationService:
    return ProjectCreationService(
        FilesystemProjectWorkspace(dev_root),
        RegistryProjectRecorder(registry_path, dev_root, today=lambda: date(2026, 8, 4)),
    )


def _opaque_id(path: Path) -> str:
    return sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:24]


def test_refresh_exposes_a_created_project_without_a_restart(
    dev_root: Path, registry_path: Path
) -> None:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    before = provider.refresh()
    assert "new-project" not in {project.name for project in before.catalogue}

    created = _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    after = provider.refresh()

    entry = next(project for project in after.catalogue if project.name == "new-project")
    assert entry.group == "Registered"
    assert entry.opaque_id == _opaque_id(created.path)


def test_a_terminal_built_before_creation_resolves_the_new_project(
    dev_root: Path, registry_path: Path, tmp_path: Path
) -> None:
    """The terminal keeps the mapping it was constructed with, so it must be mutated live."""
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    provider.refresh()
    gateway = TmuxGateway(
        "remote-agents-test-catalogue", _StubRunner(), intent_directory=tmp_path / "intents"
    )
    terminal = TmuxTerminal(gateway, provider.paths, {}, startup_timeout=1)

    created = _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    provider.refresh()

    project_id = ProjectId(_opaque_id(created.path))
    assert terminal._project_paths[project_id] == created.path


def test_the_shared_path_view_is_read_only_to_its_consumers(
    dev_root: Path, registry_path: Path
) -> None:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    provider.refresh()

    with pytest.raises(TypeError):
        provider.paths[ProjectId("intruder")] = Path("/tmp")  # type: ignore[index]


def test_refresh_keeps_the_opaque_id_of_an_unchanged_project_stable(
    dev_root: Path, registry_path: Path
) -> None:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    first = {project.name: project.opaque_id for project in provider.refresh().catalogue}

    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    second = {project.name: project.opaque_id for project in provider.refresh().catalogue}

    assert first["existing"] == second["existing"]


def test_refresh_skips_a_catalogued_directory_that_no_longer_exists(
    dev_root: Path, registry_path: Path
) -> None:
    """A stale registry entry must degrade one project, never the whole refresh."""
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    provider.refresh()
    (dev_root / "infra" / "existing").rmdir()

    snapshot = provider.refresh()

    assert snapshot.registry_error is None
    assert ProjectId(_opaque_id(dev_root / "infra" / "existing")) not in provider.paths


def test_refresh_reports_a_degraded_registry_without_raising(
    dev_root: Path, registry_path: Path
) -> None:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    registry_path.write_text("version: 2\nprojects: []\n", encoding="utf-8")

    snapshot = provider.refresh()

    assert snapshot.registry_error == "registry_invalid"


async def test_a_session_launched_before_a_refresh_survives_it_intact(
    dev_root: Path, registry_path: Path, tmp_path: Path
) -> None:
    """Adding a project must not disturb a session already running against another one."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        provider = ProjectCatalogueProvider(registry_path, dev_root)
        provider.refresh()
        store = SQLiteSessionStore(connection)
        service = SessionService(store, FakeTerminal())
        existing_id = ProjectId(_opaque_id(dev_root / "infra" / "existing"))
        launched = await service.launch(LaunchCommand(existing_id, ProfileId("claude"), "launch-1"))

        _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
        provider.refresh()

        after = await store.get(launched.session_id)
        assert after is not None
        assert after.state is launched.state
        assert after.project_id == existing_id
        assert not await store.claim_idempotency_key("launch-1")
        observation = await service.inspect(InspectQuery(launched.session_id))
        assert observation is not None and observation.live
    finally:
        connection.close()


async def test_boundary_refresh_replaces_the_catalogue_and_clears_cached_views(
    dev_root: Path, registry_path: Path
) -> None:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    boundary = PrivateBotBoundary(
        1,
        2,
        catalogue=provider.refresh().catalogue,
        catalogue_source=lambda: provider.refresh().catalogue,
    )
    boundary._project_views["all"] = boundary.catalogue

    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    await boundary.refresh_catalogue()

    assert "new-project" in {project.name for project in boundary.catalogue}
    assert boundary._project_views == {}


async def test_boundary_refresh_is_inert_without_a_catalogue_source() -> None:
    boundary = PrivateBotBoundary(1, 2, catalogue=())

    await boundary.refresh_catalogue()

    assert boundary.catalogue == ()
