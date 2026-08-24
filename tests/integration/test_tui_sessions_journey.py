"""The terminal can see and reach a session another process launched.

This is the recovery hole Stage 2 closes. The session is launched through a separate
composition on its own SQLite connection — the shape a running `serve` actually has.

What each half of that actually proves, because the two differ:

- **Listing and detail** are genuinely cross-connection. `SessionService` holds no cache;
  `list_sessions` is a live SELECT on connection B, so the row on screen came off disk
  after connection A wrote it.
- **Attach** is not. `FakeTerminal` is deliberately *shared* between the two compositions,
  because it stands in for the tmux server — a genuinely shared out-of-process resource.
  `copy_attach` asks it whether the pane is live and owned, so an unshared second fake
  would return None for a session it never launched and this test would fail for a reason
  that says nothing about the store.

Do not "clean up" the shared terminal: it is load-bearing, and removing it would either
break these assertions or make them vacuous. The process axis is covered separately by
tests/integration/test_cross_process_management.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from backends import backend_for
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.application.session_views import with_project_names
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.screen.query_one("#status").content)


def _breadcrumb(app: RemoteAgentsTui) -> str:
    """The header trail, which is where the session's own name lives since the status split."""
    return app.screen.sub_title or ""


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


async def test_the_terminal_lists_inspects_and_reaches_a_session_it_never_launched(
    database: Path,
) -> None:
    service_connection = open_database(database)
    terminal_connection = open_database(database)
    try:
        # Connection A — stands in for the running service.
        terminal = FakeTerminal()
        service = SessionService(SQLiteSessionStore(service_connection), terminal)
        launched = await service.launch(
            LaunchCommand(ProjectId("opaque-existing"), ProfileId("claude"), "service-key")
        )

        # Connection B — the terminal's own composition, sharing only the database file.
        context = TuiContext(
            backend=backend_for(
                sessions=SessionService(SQLiteSessionStore(terminal_connection), terminal),
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=lambda: (_PROJECT,),
                catalogue=(_PROJECT,),
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
        )
        app = RemoteAgentsTui(context)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()

            rows = _rows(app)
            assert len(rows) == 1, "the terminal must see the service's session"
            assert "running" in rows[0]

            await pilot.press("enter")
            await pilot.pause()
            assert position(app) == "SESSION_DETAIL"
            detail = _status(app)
            # Asserted against the *named* record, not the stored one. This test is the only
            # member of its class that ever failed, and the reason is that it is the only one
            # that launches through the real `SessionService` into a real store -- so
            # `launched.display.project_slug` is genuinely the catalogue's opaque_id, which is
            # what production holds and what every hand-built fixture had been guessing wrong.
            # The surface now resolves that to the project's name, so comparing the breadcrumb
            # against the raw record would be asserting the defect Stage 1 removed.
            (named,) = with_project_names((launched,), (_PROJECT,))
            assert named.display.rendered in _breadcrumb(app)
            assert launched.display.project_slug not in _breadcrumb(app)
            assert "running" in detail

            # Pressing enter rather than calling _resolve_detail: the detail step's own
            # selection wiring is otherwise never exercised by a keystroke.
            await pilot.press("enter")
            await pilot.pause()
            attach = _status(app)

        assert str(launched.session_id) in attach, "the attach command must name the session"
        # Byte for byte what the owner would paste, from the same codec production uses.
        assert " ".join(attach_argv(launched.session_id)) in attach
    finally:
        service_connection.close()
        terminal_connection.close()


async def test_a_rename_typed_locally_is_on_disk_for_the_other_writer_to_read(
    database: Path,
) -> None:
    """Renaming is a *write*, so the cross-connection claim has to be made in that direction.

    Every other assertion in this file reads what the service wrote. This is the only one that
    writes from the terminal and reads back through the service's own connection, which is what
    makes it a claim about the shared store rather than about one `SessionService` instance
    agreeing with itself — the store holds no cache, so a rename that never reached disk would
    still be visible to the connection that issued it.

    Driven through the screen's own `submit`, not `SessionService.rename`: the assertion is
    about what the surface does with a typed name, and calling the service directly would prove
    the store works and leave the entry untested.
    """
    service_connection = open_database(database)
    terminal_connection = open_database(database)
    try:
        terminal = FakeTerminal()
        service = SessionService(SQLiteSessionStore(service_connection), terminal)
        launched = await service.launch(
            LaunchCommand(ProjectId("opaque-existing"), ProfileId("claude"), "service-key")
        )
        assert launched.display.custom_label is None, "the fixture must start with no name"

        context = TuiContext(
            backend=backend_for(
                sessions=SessionService(SQLiteSessionStore(terminal_connection), terminal),
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=lambda: (_PROJECT,),
                catalogue=(_PROJECT,),
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
        )
        app = RemoteAgentsTui(context)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert position(app) == "SESSION_DETAIL"

            await app.screen.choose("rename")
            await pilot.pause()
            assert position(app) == "RENAME"
            await app.screen.submit("nightly")
            await pilot.pause()

            # Back on the detail, which re-read the session for itself on the way back.
            assert position(app) == "SESSION_DETAIL"
            trail = _breadcrumb(app)

            # And the list behind it, which is a second read of the same write through the
            # same connection — the name has to reach the row the owner picks from, not only
            # the screen that set it.
            await pilot.press("ctrl+s")
            await pilot.pause()
            listed = _rows(app)

        assert "nightly" in trail, f"the detail came back naming {trail!r}"
        assert any("nightly" in row for row in listed), f"the list showed {listed}"
        # The other writer's connection, which has never seen the terminal's.
        reread = await service.list_sessions()
        named = next(item for item in reread if item.session_id == launched.session_id)
        assert named.display.custom_label == "nightly"
    finally:
        service_connection.close()
        terminal_connection.close()


async def test_a_session_stopped_by_the_service_leaves_the_terminal_list(
    database: Path,
) -> None:
    """The terminal reads the store live rather than caching what it first saw."""
    service_connection = open_database(database)
    terminal_connection = open_database(database)
    try:
        terminal = FakeTerminal()
        service = SessionService(SQLiteSessionStore(service_connection), terminal)
        launched = await service.launch(
            LaunchCommand(ProjectId("opaque-existing"), ProfileId("claude"), "service-key")
        )
        context = TuiContext(
            backend=backend_for(
                sessions=SessionService(SQLiteSessionStore(terminal_connection), terminal),
                projects=object(),  # type: ignore[arg-type]
                refresh_catalogue=lambda: (_PROJECT,),
                catalogue=(_PROJECT,),
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
        )
        app = RemoteAgentsTui(context)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(_rows(app)) == 1

            from remote_agents.application.commands import ForceStopCommand

            await service.force_stop(ForceStopCommand(launched.session_id))

            await pilot.press("ctrl+s")
            await pilot.pause()
            rows = _rows(app)
            status = _status(app)

        assert rows == ["No managed sessions on this host."]
        assert "no managed sessions" in status.casefold()
    finally:
        service_connection.close()
        terminal_connection.close()
