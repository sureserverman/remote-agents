"""What `quit` means depends on where the surface is hosted — DEC-096, closing BL-097.

`F10` is `quit`, borrowed from htop and mc (DEC-095), and `ctrl+q` and `q` reach the same
action. Off a console that means "leave the app": the process ends and the terminal comes
back, which is what those two programs mean by the key.

**In a console surface pane it used to mean "destroy the pane you are reading".** Those panes
carry no `remain-on-exit`, so ending the process closed the pane and tmux reflowed the layout
over the gap with nothing to rebuild it. The answer shipped for that was de-advertisement —
the key stayed bound and the console's footer stopped offering it — which was the cheap honest
half of a fix.

This is the other half. Under console hosting the key now means what the owner meant by it:
**leave remote-agents**. The whole console goes, the shell comes back, and every agent session
keeps running.

**The teardown is launched, not run here**, and that is the point of the seam rather than an
implementation detail. A process that ran the teardown in-process would be inside the session
it is killing: if it died between "send the agent home" and "kill", nobody would finish the
job or report that it had not been done. So the action starts `remote-agents console close`
detached and returns; the closer outlives the pane.

The hosting seam is `TuiContext.console_close`, wired by the composition root exactly as every
other console-only capability is (DEC-046). These tests state hosting the way production
states it — a composed field — rather than by setting `$TMUX`, which the app deliberately does
not read.
"""

from __future__ import annotations

from pathlib import Path

from backends import backend_for
from test_tui_snapshots import settle
from tui_feedback import announcements

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _RecordingLauncher:
    """Stands in for the composition root's detached launch, and counts it."""

    def __init__(self) -> None:
        self.launches = 0

    async def __call__(self) -> None:
        self.launches += 1


def _context(launcher: _RecordingLauncher | None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=object(),  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        console_hosted=launcher is not None,
        console_close=launcher,
    )


async def _typing_a_project_name(app: RemoteAgentsTui, pilot) -> None:
    """The shortest route to a screen whose `work_in_flight` is true, as BL-025's tests use."""
    await app.show_areas()
    await settle(app, pilot)
    await app.screen.choose("infra")
    await pilot.pause()
    await pilot.press(*"orbit-relay")
    await pilot.pause()


async def test_under_console_hosting_quit_launches_the_closer_and_the_app_stays_up() -> None:
    """The key's new meaning, and the half that is easy to get wrong.

    The app must **not** exit. Exiting ends this pane's process, which is precisely the defect
    BL-097 records — and it would race the closer, which has an agent to send home before it
    may kill anything. The pane goes away because tmux removes the session out from under it,
    which is the closer's job and not this action's.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("f10")
        await pilot.pause()

        assert launcher.launches == 1, "F10 under console hosting did not start the closer"
        assert app.is_running, (
            "the surface exited its own pane instead of letting the closer remove the console; "
            "that is BL-097 happening again, now racing a teardown"
        )


async def test_under_console_hosting_the_warning_still_comes_first_and_launches_nothing() -> None:
    """DEC-027 and DEC-025 are untouched: the unsaved-work warning still asks once.

    The console makes the press cost *more*, not less — one key now closes four panes — so the
    warning matters more here than off a console. The first press must launch nothing at all:
    a closer started "just in case" would tear the console down behind a warning the owner was
    still reading.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _typing_a_project_name(app, pilot)
        assert app.screen.work_in_flight, "nothing was in flight, so quit has nothing to warn on"

        await pilot.press("f10")
        await pilot.pause()

        assert launcher.launches == 0, "the warning press started the teardown anyway"
        assert app.is_running
        warned = announcements(app, severity="warning")
        assert warned and "orbit-relay" in warned[-1], (
            f"the warning has to name what is about to be lost; the surface said {warned}"
        )

        await pilot.press("f10")
        await pilot.pause()

        assert launcher.launches == 1, "the second press must leave, as it always has"


async def test_under_console_hosting_a_surface_already_leaving_launches_nothing() -> None:
    """`_leaving` still returns early, before the launch and before anything else.

    The surface has already decided to leave and is carrying an attach request; answering the
    key here would replace it. Under console hosting the same early return also stops a second
    teardown being launched at a console that is already going.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._leaving = True

        await pilot.press("f10")
        await pilot.pause()

        assert launcher.launches == 0, "a surface already leaving started a teardown"


async def test_off_a_console_quit_leaves_the_app_exactly_as_it_always_did() -> None:
    """The other half, and a test asserting only the first would pass without it.

    A bare `remote-agents tui` owns its terminal: `quit` ends the app and hands it back. There
    is no console to close and nothing to launch — a surface that tried would be running a
    teardown against the owner's *real* console from a process that is not in it.
    """
    app = RemoteAgentsTui(_context(None))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("f10")
        await pilot.pause()

        assert not app.is_running, "off a console F10 must still leave the app"


async def test_a_launch_that_fails_leaves_the_console_standing_and_the_surface_alive() -> None:
    """A wired capability may not take the surface down — and here that rule IS the feature.

    A spawn fails for reasons that arrive exactly when an owner reaches for quit: a host out
    of file descriptors or memory, an executable that moved under a half-finished upgrade.
    Unguarded, the exception leaves `action_quit` and reaches the app's crash path, which ends
    this process — and ending this process inside a console pane is BL-097, arriving through a
    new door. Worse than the original, which was only a key doing the wrong thing.

    So both halves are asserted: the surface survives, and it does **not** fall through to the
    ordinary quit, which would close the pane exactly as the old defect did.

    Found by Stage 1's Tier-2 review, which noted this file's own siblings
    (`console_read_selection`, `console_holds_slot`) already guard at their call sites.
    """

    class _Refuses:
        launches = 0

        async def __call__(self) -> None:
            raise OSError("cannot allocate memory")

    app = RemoteAgentsTui(_context(_Refuses()))  # type: ignore[arg-type]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("f10")
        await pilot.pause()

        assert app.is_running, (
            "a failed spawn ended the pane, which is BL-097 through a different door"
        )
        assert announcements(app, severity="error"), "the owner was told nothing"
