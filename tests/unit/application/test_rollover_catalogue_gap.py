"""The rollover pass finds a project the service has not read yet, without the owner's help.

Measured on the live service, 2026-10-31: a project created after the service's last catalogue
read and launched from the console sat with its `ready` unread until the bot opened a picker,
and a destination created the same way would have been refused as unregistered."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.rollover_destinations import (
    DestinationRefused,
    RolloverDestinations,
)
from remote_agents.composition import telegram
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
        catalogue.paths, _feature, refresh=CatalogueGap.reader(catalogue.refresh)
    )

    resolved = await destinations.resolve(
        HandoffDestination(root, "feature", "p", "Task 1.1", None)
    )

    assert resolved.project_id == ProjectId("new")
    assert catalogue.reads == 1


async def test_a_destination_still_unknown_after_the_read_is_refused(tmp_path: Path) -> None:
    catalogue = _Catalogue({}, {})
    destinations = RolloverDestinations(
        catalogue.paths, _feature, refresh=CatalogueGap.reader(catalogue.refresh)
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
        catalogue.paths, _feature, refresh=CatalogueGap.reader(catalogue.refresh)
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
    assert "refresh_projects=projects.refresh" in inspect.getsource(telegram)
