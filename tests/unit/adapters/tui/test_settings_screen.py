"""The terminal's Settings position: two providers' Remote Control, one row each.

The screen exists because the premise check moved the subject. `remoteControlAtStartup` governs
every Claude session on this machine and Codex's enrollment governs every Codex one, so neither
belongs on a session detail and neither is a property of a launch -- they are two facts about
the host, which makes them siblings and makes one screen holding both the right home rather
than a convenient one (plan Stage 3, `docs/acceptance-2026-09-11-surface-refresh.md` section 8).

What this file pins, in the order the owner meets it:

* **One key from the dashboard opens it**, and that key is `SETTINGS_KEY` rather than a literal
  here: a test spelling the key itself would keep passing after the binding moved.
* **Both rows read their current value on mount**, and a capability nobody wired reads
  *unavailable* rather than vanishing (DEC-009/DEC-061). A missing row is indistinguishable
  from a surface that forgot to draw one.
* **Every word on either row comes from `application/`** -- `REMOTE_CONTROL_DEFAULT_LABELS`,
  `REMOTE_CONTROL_DEFAULT_TITLE`, `HOST_REMOTE_CONTROL_TITLE` -- asserted against those tables
  rather than against string literals, so a surface that re-spelled one fails here instead of
  drifting away from the bot's screen, which renders the same two rows from the same tables
  (DEC-007).
* **Enter on the Claude row advances exactly one state and writes exactly once**, and the
  status line then names the new value in words (DEC-062). `PROVIDER_DEFAULT` is worded as
  *Claude's default* and never as any form of off, because an unset key resolves to an
  account-level default measured to be **on** -- a row saying "off" there would state the
  opposite of what the pane does.
* **Enter on the Codex row asks its confirmation from the screen handler** (DEC-025 as DEC-068
  extends it), which is asserted behaviourally: the question is raised by a real keypress and
  the app must still answer the next one. A caller that awaited the modal from the App's pump
  draws the modal correctly and then answers nothing at all, quit included -- so "the surface
  is still alive" is the assertion that tells the two apart, exactly as
  `test_tui_host_remote_control.py` records.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from backends import FakeHostRemoteControl, SessionUseCaseDouble, backend_for
from textual.widgets import OptionList
from tui_feedback import announcements, status

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens.confirm import (
    ConfirmScreen,
    HostRemoteControlConfirmModal,
)
from remote_agents.adapters.tui.screens.dashboard import (
    SETTINGS_KEY,
    DashboardScreen,
    host_remote_control_line,
)
from remote_agents.adapters.tui.screens.settings import (
    SettingsScreen,
    remote_control_default_line,
)
from remote_agents.application.host_remote_control import HOST_REMOTE_CONTROL_TITLE
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_LABELS,
    REMOTE_CONTROL_DEFAULT_TITLE,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import HostConnection, RemoteControlDefault

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")


class _Launcher(SessionUseCaseDouble):
    def __init__(self, records: tuple[SessionRecord, ...] = ()) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record() -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


class FakeRemoteControlDefault:
    """A scripted `ports.remote_control_default.RemoteControlDefaultPort`.

    Local to this file rather than added to `tests/support/backends.py` on purpose: the port's
    whole contract is two methods that never raise, so there is nothing here a second test
    would want that it could not state in four lines -- and the support module is being edited
    by the bot's own task in the same stage.

    It keeps the one contract a surface can observe: a write lands and the next read returns
    it. `writes` is a list rather than a count because "exactly one write per press" is the
    property, and a count cannot say which value a second write carried.
    """

    def __init__(self, value: RemoteControlDefault = RemoteControlDefault.PROVIDER_DEFAULT) -> None:
        self.value = value
        self.writes: list[RemoteControlDefault] = []
        self.reads = 0

    async def read(self) -> RemoteControlDefault:
        self.reads += 1
        return self.value

    async def write(self, value: RemoteControlDefault) -> None:
        self.writes.append(value)
        self.value = value


def _context(
    *,
    host_remote_control: object | None = None,
    claude_default: object | None = None,
) -> TuiContext:
    """A surface wired with whichever of the two settings capabilities a test is about.

    `replace` rather than a `backend_for` parameter: the helper mirrors `Backend`'s fields by
    hand and this stage's new field is not among them yet, so stating it here keeps this file
    from depending on a support-module edit that belongs to another task.
    """
    backend = backend_for(
        sessions=_Launcher((_record(),)),  # type: ignore[arg-type]
        projects=object(),  # type: ignore[arg-type]
        refresh_catalogue=lambda: (_PROJECT,),
        catalogue=(_PROJECT,),
        host_remote_control=host_remote_control,
    )
    return TuiContext(
        backend=replace(backend, claude_remote_control_default=claude_default),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    """Every row the position on screen drew, in order."""
    choices = app.screen.query_one("#choices", OptionList)
    return [str(choices.get_option_at_index(index).prompt) for index in range(choices.option_count)]


def _row(app: RemoteAgentsTui, title: str) -> str:
    """The one row naming `title`, which is how a row is found without counting positions."""
    drawn = _rows(app)
    matching = [row for row in drawn if title in row]
    assert len(matching) == 1, f"expected exactly one {title} row, drew {drawn}"
    return matching[0]


async def _until(condition, *, timeout: float = 5.0, why: str = "") -> None:
    """Wait for a condition without waiting on a pump that is suspended by design.

    The same helper `test_tui_host_remote_control.py` carries, and for the same reason: a
    confirmation raised from a screen handler suspends that screen's queue until it is
    answered, and `Pilot.press` finishes by waiting for exactly that queue to drain. A test
    that pressed and then awaited the press would be waiting for the thing under test to stop
    being true.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {why}")
        await asyncio.sleep(0.01)


async def _release_any_question(app: RemoteAgentsTui) -> None:
    """Answer whatever confirmation is still open, so teardown can finish.

    Answered rather than popped: `ask_to_confirm` is suspended on `push_screen_wait`, which
    resolves on the modal's *result*, so popping the modal leaves that caller waiting for ever
    -- on a pump `run_test`'s teardown waits for. The file would then stop rather than fail,
    which reports nothing at all.
    """
    while len(app.screen_stack) > 1 and isinstance(app.screen, ConfirmScreen):
        app.screen._answer(False)
        await asyncio.sleep(0)


async def _open_settings(app: RemoteAgentsTui, pilot) -> None:
    """Reach the screen the way the owner does -- the dashboard's one key."""
    await pilot.press(SETTINGS_KEY)
    await pilot.pause()
    assert isinstance(app.screen, SettingsScreen), f"the key left us on {type(app.screen).__name__}"


# --- The render -----------------------------------------------------------------------


def test_the_claude_row_names_the_fact_and_the_state_from_the_application_s_tables() -> None:
    """Module-level, so all four readings are checked without driving a Textual app."""
    for value, label in REMOTE_CONTROL_DEFAULT_LABELS.items():
        assert remote_control_default_line(value) == f"{REMOTE_CONTROL_DEFAULT_TITLE} · {label}"


def test_an_unwired_claude_default_renders_unavailable_rather_than_nothing() -> None:
    """DEC-009/DEC-061: a declared absence is a reading, and the row states it."""
    assert remote_control_default_line(None) == f"{REMOTE_CONTROL_DEFAULT_TITLE} · unavailable"


def test_the_provider_default_is_never_worded_as_an_off() -> None:
    """The measured constraint, asserted on the row rather than trusted to the table.

    An unset `remoteControlAtStartup` resolves to an account-level default which on this
    owner's account is **on** (acceptance section 8), so a row reading "off" for that state
    would state the opposite of what a launched pane does -- the direction of wrongness an
    owner acts on by not acting.
    """
    line = remote_control_default_line(RemoteControlDefault.PROVIDER_DEFAULT)
    assert "off" not in line.casefold()
    assert line != remote_control_default_line(RemoteControlDefault.OFF)


async def test_the_screen_mounts_showing_both_current_values() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    port = FakeRemoteControlDefault(RemoteControlDefault.ON)
    app = RemoteAgentsTui(_context(host_remote_control=control, claude_default=port))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        assert _row(app, REMOTE_CONTROL_DEFAULT_TITLE) == remote_control_default_line(
            RemoteControlDefault.ON
        )
        assert _row(app, HOST_REMOTE_CONTROL_TITLE) == f"{HOST_REMOTE_CONTROL_TITLE} · off"


async def test_a_host_that_wired_neither_capability_still_draws_both_rows() -> None:
    """Two declared absences, both stated. Omitting a row hides the wiring question."""
    app = RemoteAgentsTui(_context())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        assert _row(app, REMOTE_CONTROL_DEFAULT_TITLE).endswith("· unavailable")
        assert _row(app, HOST_REMOTE_CONTROL_TITLE) == host_remote_control_line(None)


# --- The Claude row -------------------------------------------------------------------


async def test_enter_on_the_claude_row_advances_exactly_one_state_and_writes_once() -> None:
    """One press, one advance, one write -- and the row reads the new value back.

    The read-back matters as much as the write: the press writes through a port whose
    `PROVIDER_DEFAULT` is a key *removal*, so a surface that drew what it intended rather than
    what the file now says would be the one thing this row must never do.
    """
    port = FakeRemoteControlDefault(RemoteControlDefault.PROVIDER_DEFAULT)
    app = RemoteAgentsTui(_context(claude_default=port))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert port.writes == [RemoteControlDefault.ON], "one press must be one write"
        assert _row(app, REMOTE_CONTROL_DEFAULT_TITLE) == remote_control_default_line(
            RemoteControlDefault.ON
        )


async def test_the_press_names_the_new_value_in_words() -> None:
    """DEC-062's half of this task: the owner is told the outcome, not left to infer it."""
    port = FakeRemoteControlDefault(RemoteControlDefault.ON)
    app = RemoteAgentsTui(_context(claude_default=port))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        said = status(app)
        assert REMOTE_CONTROL_DEFAULT_LABELS[RemoteControlDefault.OFF] in said, said
        assert REMOTE_CONTROL_DEFAULT_TITLE in said, said


async def test_three_presses_return_the_claude_row_to_where_it_started() -> None:
    """What makes one row able to reach all three states, driven rather than reasoned."""
    port = FakeRemoteControlDefault(RemoteControlDefault.ON)
    app = RemoteAgentsTui(_context(claude_default=port))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        for _ in range(3):
            await pilot.press("enter")
            await pilot.pause()
        assert port.writes == [
            RemoteControlDefault.OFF,
            RemoteControlDefault.PROVIDER_DEFAULT,
            RemoteControlDefault.ON,
        ]
        assert _row(app, REMOTE_CONTROL_DEFAULT_TITLE) == remote_control_default_line(
            RemoteControlDefault.ON
        )


async def test_the_claude_row_is_inert_where_no_port_is_wired() -> None:
    """A dead-end row is worse than an absent one: no write, no crash, no false report."""
    app = RemoteAgentsTui(_context(host_remote_control=FakeHostRemoteControl()))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert _row(app, REMOTE_CONTROL_DEFAULT_TITLE).endswith("· unavailable")
        assert not announcements(app)


# --- The Codex row --------------------------------------------------------------------


async def test_enter_on_the_codex_row_asks_from_the_screen_handler_and_leaves_us_answering() -> (
    None
):
    """DEC-025/DEC-068's property, proven by the app answering the *next* key.

    A confirmation awaited from a binding body runs on the App's message-pump task, so the
    modal draws correctly and then nothing is delivered ever again -- quit included. Pressing
    Enter on the row reaches `choose` on this screen's own pump instead, which is where every
    confirmation in this tree is raised from, and the proof is that escape still lands.
    """
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(host_remote_control=control))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        await pilot.press("down")
        pressing = asyncio.create_task(pilot.press("enter"))
        dismissing: asyncio.Task[None] | None = None
        try:
            await _until(
                lambda: isinstance(app.screen, HostRemoteControlConfirmModal),
                why="the row to raise the confirmation",
            )
            dismissing = asyncio.create_task(pilot.press("escape"))
            await _until(
                lambda: isinstance(app.screen, SettingsScreen),
                why="the app to answer a key while the confirmation was open",
            )
            await asyncio.wait_for(asyncio.gather(pressing, dismissing), timeout=10)
            assert not [call for call in control.calls if call.startswith("set_state")], (
                "a dismissed question must not have changed the machine"
            )
        finally:
            for task in (pressing, dismissing):
                if task is not None and not task.done():
                    task.cancel()
            await _release_any_question(app)


async def test_confirming_the_codex_row_issues_one_command_and_redraws_it() -> None:
    """The round trip, driven as a task because the handler suspends its own pump."""
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(host_remote_control=control))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        asking = asyncio.create_task(screen.confirm_codex_remote_control())
        await pilot.pause()
        # Down onto the confirm row, deliberately: the abort is the resting one (DEC-007).
        await pilot.press("down")
        await pilot.press("enter")
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        assert "set_state:active" in control.calls
        assert _row(app, HOST_REMOTE_CONTROL_TITLE) == f"{HOST_REMOTE_CONTROL_TITLE} · on"
        assert HOST_REMOTE_CONTROL_TITLE in status(app), (
            "the outcome is named in words here too (DEC-062)"
        )


async def test_declining_the_codex_row_issues_nothing_and_leaves_the_row_alone() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(host_remote_control=control))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        asking = asyncio.create_task(screen.confirm_codex_remote_control())
        await pilot.pause()
        await pilot.press("enter")  # the resting row is Cancel
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        assert not [call for call in control.calls if call.startswith("set_state")]
        assert _row(app, HOST_REMOTE_CONTROL_TITLE) == f"{HOST_REMOTE_CONTROL_TITLE} · off"


async def test_the_codex_row_is_inert_where_no_host_control_is_wired() -> None:
    app = RemoteAgentsTui(_context(claude_default=FakeRemoteControlDefault()))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert _row(app, HOST_REMOTE_CONTROL_TITLE).endswith("· unavailable")


# --- Getting in and out ----------------------------------------------------------------


async def test_the_dashboard_key_opens_the_settings_screen() -> None:
    """One key, registered in the console's key-budget declaration (DEC-052/DEC-062)."""
    app = RemoteAgentsTui(_context(claude_default=FakeRemoteControlDefault()))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await _open_settings(app, pilot)


async def test_escape_returns_to_the_dashboard() -> None:
    """Pushed rather than switched, so the way out is the position it was opened from."""
    app = RemoteAgentsTui(_context(claude_default=FakeRemoteControlDefault()))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_settings(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
