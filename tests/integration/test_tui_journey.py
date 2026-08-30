"""One terminal journey: add a project, pick it, launch it, land in the shared store."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from backends import backend_for
from tui_filter import settle_filter
from tui_positions import position

from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import ProjectCreationService
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import ProjectCatalogueProvider
from remote_agents.domain.models import SessionState

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
    return root


@pytest.fixture
def registry_path(tmp_path: Path, dev_root: Path) -> Path:
    path = tmp_path / "projects-registry.yaml"
    path.write_text(_REGISTRY.format(existing=dev_root / "infra" / "existing"), encoding="utf-8")
    return path


async def test_the_terminal_creates_picks_and_launches_one_project(
    dev_root: Path, registry_path: Path, tmp_path: Path
) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        provider = ProjectCatalogueProvider(registry_path, dev_root)
        store = SQLiteSessionStore(connection)
        context = TuiContext(
            backend=backend_for(
                sessions=SessionService(store, FakeTerminal()),
                projects=ProjectCreationService(
                    FilesystemProjectWorkspace(dev_root),
                    RegistryProjectRecorder(
                        registry_path, dev_root, today=lambda: date(2026, 8, 5)
                    ),
                ),
                refresh_catalogue=lambda: provider.refresh().catalogue,
                catalogue=provider.refresh().catalogue,
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach", "-t", f"={session_id}"),
        )
        app = RemoteAgentsTui(context)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#filter").value = "brand-new"
            await pilot.press("enter")
            await pilot.pause()
            assert not (dev_root / "infra" / "brand-new").exists()

            await pilot.press("up")
            await pilot.press("enter")
            await pilot.pause()
            assert (dev_root / "infra" / "brand-new").is_dir()

            for character in "brand-new":
                await pilot.press(character)
            await settle_filter(pilot)
            # Filter -> rows -> project -> chooser (resting on Launch) -> the agent list,
            # which is the commit position: choosing an agent here *is* the launch.
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert position(app) == "PROFILES"
            # The project is the header's trail. It carried the agent too while a position
            # stood *after* the agent choice; the agent list stands before it, so what names
            # the agent at this position is the row under the cursor.
            trail = app.screen.sub_title or ""
            assert "brand-new" in trail

            # Down, not up, and one press rather than two. The agent list rests on Back — it is
            # a commit position, so DEC-007 forbids a repeated enter committing on it — and Down
            # from the last row wraps to the first agent.
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()

        created = next(
            project for project in provider.refresh().catalogue if project.name == "brand-new"
        )
        records = list(await store.list())
        assert [record.state for record in records] == [SessionState.RUNNING]
        assert str(records[0].project_id) == created.opaque_id
        # Launched with no name, which is what both surfaces now do. Naming happens afterwards
        # from the session's own detail -- covered by test_tui_rename.py and, across two store
        # connections, by test_tui_sessions_journey.py.
        assert records[0].display.custom_label is None
        assert app.return_value is not None
        assert app.return_value.session_id == str(records[0].session_id)
        assert "attach" in app.return_value.command
    finally:
        connection.close()
