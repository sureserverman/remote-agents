"""The rollover pass finds a project the service has not read yet, without the owner's help.

Measured on the live service, 2026-10-31: a project created after the service's last catalogue
read and launched from the console sat with its `ready` unread until the bot opened a picker,
and a destination created the same way would have been refused as unregistered."""

from __future__ import annotations

import ast
import inspect
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.rollover_destinations import (
    DestinationRefused,
    RolloverDestinations,
)
from remote_agents.composition import backend, telegram
from remote_agents.composition.backend import ProjectCatalogueProvider
from remote_agents.composition.service import CatalogueGap, build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.ports.handoff_envelopes import HandoffDestination


def _record(project: str) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=SessionId.new(),
        project_id=ProjectId(project),
        profile_id=ProfileId("claude"),
        state=SessionState.RUNNING,
    )


async def _feature(_root: Path) -> str:
    return "feature"


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Catalogue:
    def __init__(self, paths: dict[ProjectId, Path], adds: dict[ProjectId, Path]) -> None:
        self.paths, self.adds, self.reads = paths, adds, 0

    def refresh(self) -> None:
        self.reads += 1
        self.paths.update(self.adds)


async def test_a_session_on_an_unknown_project_rereads_the_catalogue(tmp_path: Path) -> None:
    catalogue = _Catalogue({}, {ProjectId("new"): tmp_path})
    gap = CatalogueGap(catalogue.paths, catalogue.refresh, _Clock())

    await gap.close((_record("new"),))

    assert catalogue.reads == 1
    assert catalogue.paths[ProjectId("new")] == tmp_path


async def test_a_known_project_reads_nothing(tmp_path: Path) -> None:
    catalogue = _Catalogue({ProjectId("known"): tmp_path}, {})
    gap = CatalogueGap(catalogue.paths, catalogue.refresh, _Clock())

    await gap.close((_record("known"),))

    assert catalogue.reads == 0


async def test_a_project_still_unknown_is_retried_only_after_the_interval() -> None:
    """A session on a removed project must not make every pass walk the development root."""
    catalogue = _Catalogue({}, {})
    clock = _Clock()
    gap = CatalogueGap(catalogue.paths, catalogue.refresh, clock, retry_seconds=60.0)

    await gap.close((_record("gone"),))
    clock.now += 59.0
    await gap.close((_record("gone"),))
    assert catalogue.reads == 1
    clock.now += 1.0
    await gap.close((_record("gone"),))
    assert catalogue.reads == 2


async def test_a_failed_read_never_stops_the_pass() -> None:
    def broken() -> None:
        raise OSError("registry unreadable")

    await CatalogueGap({}, broken, _Clock()).close((_record("new"),))


async def test_a_destination_created_since_the_last_read_is_found(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    catalogue = _Catalogue({}, {ProjectId("new"): root})
    destinations = RolloverDestinations(
        catalogue.paths,
        _feature,
        refresh=CatalogueGap(catalogue.paths, catalogue.refresh, _Clock()).read_for,
    )

    resolved = await destinations.resolve(
        HandoffDestination(root, "feature", "p", "Task 1.1", None)
    )

    assert resolved.project_id == ProjectId("new")
    assert catalogue.reads == 1


async def test_a_destination_still_unknown_after_the_read_is_refused(tmp_path: Path) -> None:
    catalogue = _Catalogue({}, {})
    destinations = RolloverDestinations(
        catalogue.paths,
        _feature,
        refresh=CatalogueGap(catalogue.paths, catalogue.refresh, _Clock()).read_for,
    )

    with pytest.raises(DestinationRefused, match="destination-unregistered"):
        await destinations.resolve(
            HandoffDestination(tmp_path.resolve(), "feature", "p", "Task 1.1", None)
        )
    assert catalogue.reads == 1


async def test_a_known_destination_reads_nothing(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    catalogue = _Catalogue({ProjectId("known"): root}, {})
    destinations = RolloverDestinations(
        catalogue.paths,
        _feature,
        refresh=CatalogueGap(catalogue.paths, catalogue.refresh, _Clock()).read_for,
    )

    await destinations.resolve(HandoffDestination(root, "feature", "p", "Task 1.1", None))

    assert catalogue.reads == 0


async def test_the_rollover_pass_itself_closes_the_gap(tmp_path: Path) -> None:
    catalogue = _Catalogue({}, {ProjectId("new"): tmp_path})

    async def list_sessions() -> tuple[SimpleNamespace, ...]:
        return (_record("new"),)

    async def enabled() -> bool:
        return True

    connection = open_database(tmp_path / "s.sqlite3", migrations=MIGRATIONS)
    try:
        rollover_pass = build_rollover_pass(
            connection=connection,
            sessions=SimpleNamespace(
                list_sessions=list_sessions,
                launch=None,
                graceful_stop=None,
                continuation_permissions=None,
            ),
            terminal=SimpleNamespace(pane_idle=None, send_prompt=None),
            enabled=enabled,
            project_paths=catalogue.paths,
            adopt_templates={},
            refresh_projects=catalogue.refresh,
        )
        await rollover_pass.run_once()
    finally:
        connection.close()

    assert catalogue.reads == 1


def test_the_service_hands_the_pass_its_catalogue() -> None:
    tree = ast.parse(inspect.getsource(telegram))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_rollover_pass"
    ]
    assert len(calls) == 1
    handed = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert handed["refresh_projects"] == "projects.refresh"


async def test_an_ended_session_on_a_removed_project_reads_nothing() -> None:
    catalogue = _Catalogue({}, {})
    ended = _record("gone")
    ended.state = SessionState.ENDED

    await CatalogueGap(catalogue.paths, catalogue.refresh, _Clock()).close((ended,))

    assert catalogue.reads == 0


async def test_a_refused_destination_reads_once_per_interval_for_that_root(tmp_path: Path) -> None:
    catalogue = _Catalogue({}, {})
    clock = _Clock()
    gap = CatalogueGap(catalogue.paths, catalogue.refresh, clock, retry_seconds=60.0)

    await gap.read_for(tmp_path / "a")
    await gap.read_for(tmp_path / "a")
    assert catalogue.reads == 1
    await gap.read_for(tmp_path / "b")
    assert catalogue.reads == 2
    clock.now += 60.0
    await gap.read_for(tmp_path / "a")
    assert catalogue.reads == 3


async def test_a_hung_read_lets_the_pass_go_on() -> None:
    release = threading.Event()
    gap = CatalogueGap({}, lambda: release.wait(5), _Clock(), read_timeout=0.01)

    await gap.close((_record("new"),))
    release.set()


async def test_with_the_switch_off_the_pass_reads_nothing(tmp_path: Path) -> None:
    catalogue = _Catalogue({}, {ProjectId("new"): tmp_path})

    async def list_sessions() -> tuple[SimpleNamespace, ...]:
        return (_record("new"),)

    async def disabled() -> bool:
        return False

    connection = open_database(tmp_path / "s.sqlite3", migrations=MIGRATIONS)
    try:
        await build_rollover_pass(
            connection=connection,
            sessions=SimpleNamespace(
                list_sessions=list_sessions,
                launch=None,
                graceful_stop=None,
                continuation_permissions=None,
            ),
            terminal=SimpleNamespace(pane_idle=None, send_prompt=None),
            enabled=disabled,
            project_paths=catalogue.paths,
            adopt_templates={},
            refresh_projects=catalogue.refresh,
        ).run_once()
    finally:
        connection.close()

    assert catalogue.reads == 0


def _provider(tmp_path: Path) -> tuple[ProjectCatalogueProvider, Path, Path]:
    registered = tmp_path / "elsewhere" / "kept"
    registered.mkdir(parents=True)
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\nprojects:\n"
        f"  - path: {registered}\n    name: kept\n    area: a\n    enabled: true\n"
        "    added: 2026-10-31\n"
    )
    dev = tmp_path / "dev"
    dev.mkdir()
    return ProjectCatalogueProvider(registry, dev), registry, registered


def test_a_degraded_registry_read_removes_no_project(tmp_path: Path) -> None:
    provider, registry, registered = _provider(tmp_path)
    provider.refresh()
    assert registered.resolve() in provider.paths.values()

    registry.write_text("{ not yaml")
    assert provider.refresh().registry_error is not None

    assert registered.resolve() in provider.paths.values()


def test_a_project_really_gone_from_a_good_registry_is_removed(tmp_path: Path) -> None:
    provider, registry, registered = _provider(tmp_path)
    provider.refresh()

    registry.write_text("version: 1\nprojects: []\n")
    provider.refresh()

    assert registered.resolve() not in provider.paths.values()


def test_refreshes_run_one_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _registry, _registered = _provider(tmp_path)
    running, most = [0], [0]
    real = backend.load_registry

    def counted(path: Path):
        running[0] += 1
        most[0] = max(most[0], running[0])
        time.sleep(0.02)
        running[0] -= 1
        return real(path)

    monkeypatch.setattr(backend, "load_registry", counted)
    threads = [threading.Thread(target=provider.refresh) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert most[0] == 1
