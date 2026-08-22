"""Integration tests for runtime catalogue refresh after a project is created."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.application.commands import InspectQuery, LaunchCommand
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.project_catalog import search_catalogue
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import ProjectCatalogueProvider
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.session_store import ProjectUsage


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


class _InvariantDict(dict):
    """Record every moment a watched key is absent immediately after a mutating call."""

    def __init__(self, watched: ProjectId, misses: list[str]) -> None:
        super().__init__()
        self._watched = watched
        self._misses = misses

    def clear(self) -> None:
        super().clear()
        self._check()

    def update(self, *args: object, **kwargs: object) -> None:
        super().update(*args, **kwargs)  # type: ignore[arg-type]
        self._check()

    def __delitem__(self, key: object) -> None:
        super().__delitem__(key)
        self._check()

    def _check(self) -> None:
        if self._watched not in self:
            self._misses.append("absent")


def test_a_surviving_project_is_never_absent_during_a_refresh(
    dev_root: Path, registry_path: Path
) -> None:
    """Consumers read the shared map unlocked, so a refresh must never hide a live project."""
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    provider.refresh()
    existing_id = ProjectId(_opaque_id(dev_root / "infra" / "existing"))
    misses: list[str] = []
    instrumented = _InvariantDict(existing_id, misses)
    instrumented.update(dict(provider.paths))
    provider._paths = instrumented

    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    provider.refresh()

    assert existing_id in provider.paths
    assert misses == []


def test_refresh_drops_a_project_that_left_the_registry(
    dev_root: Path, registry_path: Path
) -> None:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    created = _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    provider.refresh()
    assert ProjectId(_opaque_id(created.path)) in provider.paths

    created.path.rmdir()
    registry_path.write_text(
        _REGISTRY.format(existing=dev_root / "infra" / "existing"), encoding="utf-8"
    )
    provider.refresh()

    assert ProjectId(_opaque_id(created.path)) not in provider.paths


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
    assert "existing" not in {project.name for project in snapshot.catalogue}


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
    boundary = build_private_bot(
        1,
        2,
        backend=backend_for(
            catalogue=provider.refresh().catalogue,
            refresh_catalogue=lambda: provider.refresh().catalogue,
        ),
    )
    boundary._project_views["all"] = boundary.catalogue

    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    await boundary.refresh_catalogue()

    assert "new-project" in {project.name for project in boundary.catalogue}
    assert boundary._project_views == {}


async def test_boundary_refresh_is_inert_without_a_catalogue_source() -> None:
    boundary = build_private_bot(1, 2, backend=backend_for(catalogue=()))

    await boundary.refresh_catalogue()

    assert boundary.catalogue == ()


class _UsageLauncher(SessionUseCaseDouble):
    """A launcher that reports usage and nothing else — the ranking's only dependency."""

    def __init__(self, usage) -> None:
        self.usage = usage
        self.reads = 0

    async def project_usage(self):
        self.reads += 1
        return self.usage

    async def list_sessions(self):
        return []

    async def refresh_readiness(self) -> None:
        return None


def _usage(opaque_id: str, count: int, days_ago: int) -> ProjectUsage:
    return ProjectUsage(ProjectId(opaque_id), count, datetime.now(UTC) - timedelta(days=days_ago))


async def _ranked_boundary(dev_root: Path, registry_path: Path, usage) -> PrivateBotBoundary:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    launcher = _UsageLauncher(usage)
    boundary = build_private_bot(
        1,
        2,
        backend=backend_for(
            catalogue=provider.refresh().catalogue,
            refresh_catalogue=lambda: provider.refresh().catalogue,
            sessions=launcher,
        ),
        profiles=(ProfileAvailability("claude", True),),
    )
    await boundary.refresh_catalogue()
    return boundary


async def test_ranked_catalogue_puts_the_recently_used_project_first_everywhere(
    dev_root: Path, registry_path: Path
) -> None:
    """One ranking at the source reaches Launch, Resume and search alike.

    The two pickers share `_projects_reply` and search filters the same tuple, so ordering
    `self.catalogue` on refresh is what makes all three agree — and is why no picker has to
    know a ranking exists. Asserted on all three rather than on the tuple, because "the
    catalogue is sorted" is not the claim the owner cares about.
    """
    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    catalogue = provider.refresh().catalogue
    newest = next(project for project in catalogue if project.name == "new-project")
    assert catalogue[0].name != "new-project", "unranked, it is not already first"

    boundary = await _ranked_boundary(dev_root, registry_path, [_usage(newest.opaque_id, 3, 1)])

    launch = boundary._projects_reply(boundary.catalogue, view_id="all")
    resume = boundary._projects_reply(boundary.catalogue, view_id="all", flow="resume")
    found = boundary._projects_reply(search_catalogue(boundary.catalogue, "e"), view_id="search")
    assert boundary.catalogue[0].name == "new-project"
    for screen in (launch, resume, found):
        assert screen.keyboard[0][0].text == "new-project", screen.text


async def test_a_ranked_catalogue_is_re_read_on_the_next_refresh(
    dev_root: Path, registry_path: Path
) -> None:
    """A session launched during the run changes the next render's order, not this one's.

    Usage is read on refresh rather than per render, so the order is stable while the owner
    pages through it and moves when the catalogue is next re-read. Both halves matter: the
    first is why a list does not reshuffle under a thumb, the second is why yesterday's
    ranking does not outlive the day.
    """
    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    catalogue = provider.refresh().catalogue
    newest = next(project for project in catalogue if project.name == "new-project")
    existing = next(project for project in catalogue if project.name == "existing")
    launcher = _UsageLauncher([_usage(existing.opaque_id, 5, 1)])
    boundary = build_private_bot(
        1,
        2,
        backend=backend_for(
            catalogue=catalogue,
            refresh_catalogue=lambda: provider.refresh().catalogue,
            sessions=launcher,
        ),
    )

    await boundary.refresh_catalogue()
    assert boundary.catalogue[0].name == "existing"
    assert launcher.reads == 1, "one usage read per refresh, not one per project"

    # A session for the other project lands while the bot is running.
    launcher.usage = [_usage(existing.opaque_id, 5, 1), _usage(newest.opaque_id, 40, 0)]
    assert boundary.catalogue[0].name == "existing", "the drawn order does not move under them"

    await boundary.refresh_catalogue()

    assert boundary.catalogue[0].name == "new-project"
    assert launcher.reads == 2


def _buttons(rendered: dict[str, object]) -> list[tuple[str, str]]:
    markup = rendered.get("reply_markup")
    if markup is None:
        return []
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


class _CountingSource:
    """A catalogue source that records how often it was asked, not just what it answered."""

    def __init__(self, provider: ProjectCatalogueProvider) -> None:
        self._provider = provider
        self.reads = 0

    def __call__(self) -> tuple:
        self.reads += 1
        return self._provider.refresh().catalogue


def _picker_boundary(
    dev_root: Path, registry_path: Path
) -> tuple[PrivateBotBoundary, _CountingSource]:
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    source = _CountingSource(provider)
    boundary = build_private_bot(
        1, 2, backend=backend_for(catalogue=provider.refresh().catalogue, refresh_catalogue=source)
    )
    return boundary, source


async def test_opening_launch_picks_up_a_project_created_outside_the_bot(
    dev_root: Path, registry_path: Path
) -> None:
    """The gap Refresh existed to cover: a project the bot did not create.

    Creation *through* the bot has always refreshed at the end of its own flow, so the only
    way to hold a stale catalogue was to add a project by some other route -- an editor, the
    TUI, the registry by hand -- and then open the picker.
    """
    boundary, _ = _picker_boundary(dev_root, registry_path)
    assert "new-project" not in {project.name for project in boundary.catalogue}

    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    projects = await boundary._reply_for("launch.open", "projects")

    assert "new-project" in {text for text, _ in _buttons(projects)}


async def test_opening_resume_picks_up_a_project_created_outside_the_bot(
    dev_root: Path, registry_path: Path
) -> None:
    boundary, _ = _picker_boundary(dev_root, registry_path)

    _service(dev_root, registry_path).create(CreateProjectCommand("infra", "new-project"))
    projects = await boundary._reply_for("resume.open", "projects")

    assert "new-project" in {text for text, _ in _buttons(projects)}


async def test_paging_a_picker_does_not_re_read_the_catalogue(
    dev_root: Path, registry_path: Path
) -> None:
    """Opening is where a new order is expected; paging is where it must not arrive.

    A refresh clears `_project_views` and re-ranks, so re-reading here would reshuffle the
    very list the owner is paging through -- the invariant
    `test_a_ranked_catalogue_is_re_read_on_the_next_refresh` states from the other side.
    """
    boundary, source = _picker_boundary(dev_root, registry_path)

    await boundary._reply_for("launch.open", "projects")
    assert source.reads == 1
    await boundary._reply_for("launch.page", "all|1")
    await boundary._reply_for("resume.projects", "all|1")

    assert source.reads == 1


async def test_an_unranked_catalogue_survives_a_launcher_that_cannot_report_usage(
    dev_root: Path, registry_path: Path
) -> None:
    """Not a degraded mode: it is the composition every TUI-less test uses."""
    provider = ProjectCatalogueProvider(registry_path, dev_root)
    unranked = provider.refresh().catalogue
    boundary = build_private_bot(
        1,
        2,
        backend=backend_for(
            catalogue=unranked, refresh_catalogue=lambda: provider.refresh().catalogue
        ),
    )

    await boundary.refresh_catalogue()

    assert boundary.catalogue == unranked
