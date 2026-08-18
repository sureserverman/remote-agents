"""Opening a session routes by where the surface is hosted, and only one route exits.

Three hostings, three behaviors, none guessed at runtime by the flows themselves:

- **bare shell** — today's contract, byte for byte: the app exits with an `AttachRequest`
  and `attach_to` execs the attach command, the process becoming the attached client.
- **a client on our own tmux server** (the console) — the session is opened by switching
  the client, and the surface stays alive; nothing exits, nothing nests.
- **a foreign tmux client** — refused, exactly as before: the attach command is printed
  rather than nested, and a started session is never lost.

The app end of the seam is one method: `_open_or_leave` consumes the optional
`open_in_console` capability (DEC-007's widening pattern — a host wiring nothing gets the
exec contract unchanged), so `launch` and `issue_resume` cannot each invent their own
answer to "does opening a session end the surface".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.tui.app import AttachRequest, RemoteAgentsTui
from remote_agents.adapters.tui.attach import HostingMode, attach_to, hosting_mode
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION = "01234567-89ab-cdef-0123-456789abcdef"
_OURS = {"TMUX": "/tmp/tmux-1000/remote-agents,12345,0"}
_FOREIGN = {"TMUX": "/tmp/tmux-1000/default,999,1"}


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


def _context(open_in_console=None) -> TuiContext:
    return TuiContext(
        launcher=object(),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        open_in_console=open_in_console,
    )


def test_hosting_mode_reads_the_tmux_socket_not_just_its_presence() -> None:
    assert hosting_mode({}) is HostingMode.BARE
    assert hosting_mode(_OURS) is HostingMode.CONSOLE
    assert hosting_mode(_FOREIGN) is HostingMode.FOREIGN
    # A malformed TMUX value is somebody's tmux, never ours: refuse to nest.
    assert hosting_mode({"TMUX": "garbage"}) is HostingMode.FOREIGN


def test_bare_shell_execs_the_attach_command_unchanged() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    request = AttachRequest(_SESSION, ("tmux", "-L", "remote-agents", "attach-session"))
    code = attach_to(request, environment={}, exec_argv=lambda p, a: calls.append((p, a)))
    assert code == 0
    assert calls == [("tmux", request.argv)]


def test_our_own_client_switches_instead_of_nesting() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    request = AttachRequest(_SESSION, ("tmux", "-L", "remote-agents", "attach-session"))
    code = attach_to(request, environment=_OURS, exec_argv=lambda p, a: calls.append((p, a)))
    assert code == 0
    assert calls == [
        (
            "tmux",
            ("tmux", "-L", "remote-agents", "switch-client", "-t", f"ra-{_SESSION}:"),
        )
    ]


def test_a_foreign_client_is_still_refused_with_the_command_printed() -> None:
    reported: list[str] = []
    request = AttachRequest(_SESSION, ("tmux", "-L", "remote-agents", "attach-session"))
    code = attach_to(
        request,
        environment=_FOREIGN,
        exec_argv=lambda p, a: pytest.fail("a foreign client must never exec"),
        report=reported.append,
    )
    assert code == 0
    assert len(reported) == 1 and request.command in reported[0]


async def test_the_app_opens_in_console_and_stays_alive_when_the_capability_is_wired() -> None:
    opened: list[str] = []

    async def opener(session_id: str) -> None:
        opened.append(session_id)

    app = RemoteAgentsTui(_context(open_in_console=opener))
    async with app.run_test() as pilot:
        await app._open_or_leave(_SESSION)
        await pilot.pause()
        assert opened == [_SESSION]
        assert app.is_running, "the surface must stay alive after a console open"
    assert app.return_value is None


async def test_the_app_exits_with_an_attach_request_when_no_capability_is_wired() -> None:
    app = RemoteAgentsTui(_context())
    async with app.run_test() as pilot:
        await app._open_or_leave(_SESSION)
        await pilot.pause()
    result = app.return_value
    assert isinstance(result, AttachRequest) and result.session_id == _SESSION
