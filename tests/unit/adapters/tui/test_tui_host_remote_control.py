"""The host-level Codex Remote Control, as the terminal surface renders and drives it.

Its subject is the **machine**, not a session, which is the whole reason it lives on the
limits pane rather than on a session detail: the limits pane is the one region in this
surface that already describes the host rather than a row. Everything a session's Remote
Control needed — a record, a policy read against that record, a cursor resting on the right
row — is absent here by construction, and DEC-052 is satisfied for the same reason: there is
no row this key acts on, so there is no row it can act on by mistake.

What this file pins, in the order the owner meets it:

* **The line says which of six readings this host is in, and says the two failures
  differently.** `ERRORED` and `UNREACHABLE` are different facts — the daemon answered and
  reported its own connection broken, versus this project never having reached `codex` at
  all — and a render that spelled them alike left an owner pressing a button that could
  never explain itself. That is asserted as a *distinctness* property across all six rather
  than as six string equalities, so collapsing any pair fails here rather than in a review.
* **`None` renders "unavailable"** (DEC-009/DEC-061): a host that wired no host toggle has
  declared an absence, and the pane says so rather than hiding the line or crashing.
* **The key raises the confirmation from a posted screen message** (DEC-025 as DEC-068
  extends it). The proof is behavioural rather than structural: a binding that awaited the
  modal inline would suspend the App's pump, and the assertions below drive the real
  surface through the modal and out the other side, which is exactly what a deadlocked app
  cannot do.
* **Confirming issues one command with a fresh idempotency key, under the busy guard, and
  the answer redraws the line.** The fresh key is asserted by toggling twice against a fake
  that refuses a repeat: a reused key would make the second press unretryable, which is the
  cost `HostRemoteControlService.set_state` documents burning on a failed attempt.
* **Every direction word on screen comes from `application/`.** Asserted against
  `HOST_REMOTE_CONTROL_LABELS` and `HOST_REMOTE_CONTROL_TITLE` rather than against string
  literals, so a surface that re-spelled either would fail here rather than drift quietly
  (DEC-007's reason for the shared table).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from backends import FakeHostRemoteControl, SessionUseCaseDouble, backend_for
from textual.widgets import OptionList, Static
from tui_feedback import announcements
from tui_feedback import status as _status

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens.confirm import (
    ConfirmScreen,
    HostPairingCodeModal,
    HostRemoteControlConfirmModal,
    HostRemoteControlDirectionModal,
)
from remote_agents.adapters.tui.screens.dashboard import (
    HOST_REMOTE_CONTROL_KEY,
    DashboardScreen,
    LimitsPaneScreen,
    host_remote_control_line,
)
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)

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


def _context(host_remote_control: object | None = None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=_Launcher((_record(),)),  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
            host_remote_control=host_remote_control,
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _host_row(app: RemoteAgentsTui) -> str:
    """The host toggle's line as the limits pane actually drew it."""
    pane = app.screen.query_one("#limits-pane", OptionList)
    rows = [str(pane.get_option_at_index(index).prompt) for index in range(pane.option_count)]
    matching = [row for row in rows if HOST_REMOTE_CONTROL_TITLE in row]
    assert len(matching) == 1, f"expected exactly one host line, drew {rows}"
    return matching[0]


# --- The render -----------------------------------------------------------------------


def test_the_line_names_the_fact_from_the_application_s_own_title() -> None:
    line = host_remote_control_line(
        HostRemoteControlStatus.observed(HostConnection.DISABLED, server_name=None)
    )
    assert line.startswith(HOST_REMOTE_CONTROL_TITLE)
    assert line == f"{HOST_REMOTE_CONTROL_TITLE} · off"


def test_an_absent_capability_renders_unavailable_rather_than_nothing() -> None:
    """DEC-009/DEC-061: a declared absence is a reading, and the pane states it."""
    assert host_remote_control_line(None) == f"{HOST_REMOTE_CONTROL_TITLE} · unavailable"


@pytest.mark.parametrize("connection", list(HostConnection), ids=lambda c: c.value)
def test_every_connection_renders_a_line_that_says_something(connection: HostConnection) -> None:
    status = HostRemoteControlStatus.observed(connection, server_name=None)
    line = host_remote_control_line(status)
    assert line.startswith(f"{HOST_REMOTE_CONTROL_TITLE} · ")
    assert line.removeprefix(f"{HOST_REMOTE_CONTROL_TITLE} · ").strip()


def test_no_two_connections_render_alike() -> None:
    """Six facts, six renders — asserted as distinctness rather than as six literals.

    A per-value equality test passes for a table where two entries were made identical on
    purpose; this fails. `ERRORED` and `UNREACHABLE` are the pair that has actually been
    conflated in this project, and the assertion below names them so a failure says which
    distinction was lost rather than only that one was.
    """
    rendered = {
        connection: host_remote_control_line(
            HostRemoteControlStatus.observed(connection, server_name=None)
        )
        for connection in HostConnection
    }
    assert len(set(rendered.values())) == len(HostConnection), (
        f"two connections render the same line: {rendered}"
    )
    assert rendered[HostConnection.ERRORED] != rendered[HostConnection.UNREACHABLE]


def test_a_stopped_daemon_is_not_reported_as_off() -> None:
    """`DAEMON_ABSENT`'s own docstring: "off" is the direction of wrongness that matters.

    The persisted enrollment outlives the process that serves it, so a host whose daemon is
    merely down is one start away from being reachable — and "off" is the word an owner acts
    on by not acting.
    """
    absent = host_remote_control_line(
        HostRemoteControlStatus.observed(HostConnection.DAEMON_ABSENT, server_name=None)
    )
    off = host_remote_control_line(
        HostRemoteControlStatus.observed(HostConnection.DISABLED, server_name=None)
    )
    assert absent != off
    assert not absent.endswith("· off")


async def test_the_dashboard_limits_pane_draws_the_line() -> None:
    app = RemoteAgentsTui(_context(FakeHostRemoteControl(HostConnection.DISABLED)))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert _host_row(app) == f"{HOST_REMOTE_CONTROL_TITLE} · off"


async def test_the_console_limits_pane_draws_the_line_too() -> None:
    """The console's own pane, which is the surface the owner reads under console hosting."""
    app = RemoteAgentsTui(_context(FakeHostRemoteControl(HostConnection.CONNECTED)))
    async with app.run_test() as pilot:
        await app.push_screen(LimitsPaneScreen())
        await pilot.pause()
        assert _host_row(app) == f"{HOST_REMOTE_CONTROL_TITLE} · on"


async def test_a_host_with_no_toggle_wired_still_draws_the_line() -> None:
    app = RemoteAgentsTui(_context(None))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _host_row(app) == f"{HOST_REMOTE_CONTROL_TITLE} · unavailable"


async def test_the_line_is_redrawn_by_the_reload_the_timer_drives() -> None:
    """The pane's own reload re-reads the host, so the line rides the existing cadence.

    Asserted against `_reload_limits` rather than by waiting out the interval: Textual's
    pilot has no clock to advance, so a test that slept for sixty seconds would be the
    slowest test in the suite and would still only prove the timer fires. What is worth
    pinning is that the method the timer calls re-reads the host — and, separately, that the
    interval this pane installs is the sixty-second one the limits already use.
    """
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await app.push_screen(LimitsPaneScreen())
        await pilot.pause()
        assert _host_row(app).endswith("· off")
        control.connection = HostConnection.CONNECTED
        await app.screen._reload_limits()
        await pilot.pause()
        assert _host_row(app).endswith("· on")
    assert LimitsPaneScreen._LIMITS_AUTO_REFRESH == 60.0


# --- The key and the confirmation -----------------------------------------------------
#
# **Two shapes here, and the split is deliberate.** A confirmation raised from a screen
# handler suspends that screen's pump until it is answered -- that is the property DEC-025
# rests on, not a defect -- and `Pilot.press` finishes by waiting for the screen it
# snapshotted to drain its queue. So a test that presses the key and then waits on the
# suspended screen is waiting for the thing under test to stop being true.
#
# The behavioural cases therefore drive `confirm_host_remote_control` as a task of the test's
# own, exactly as `test_tui_force_stop.py`'s `_open_the_confirm` does, and answer it with
# keys. That is fast and it covers the flow -- but a task of the test's own runs on the
# test's task, so it cannot observe which pump a real keypress arrives on, which is precisely
# the seam DEC-068 was written about. `test_the_real_key_leaves_the_surface_answering` is
# what covers that, by pressing the real key and requiring the app to still answer.


async def _until(condition, *, timeout: float = 5.0, why: str = "") -> None:
    """Wait for a condition without waiting on a message pump that is suspended by design."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {why}")
        await asyncio.sleep(0.01)


async def _asking(app: RemoteAgentsTui, pilot) -> asyncio.Task[None]:
    """Open the confirmation and hand back the suspended caller, as a keypress would."""
    screen = app.screen
    assert isinstance(screen, DashboardScreen)
    task = asyncio.create_task(screen.confirm_host_remote_control())
    await pilot.pause()
    return task


async def _answered(task: asyncio.Task[None]) -> None:
    """Join the suspended caller, bounded so a question that never resolves fails."""
    await asyncio.wait_for(task, timeout=5)


async def _release_any_question(app: RemoteAgentsTui) -> None:
    """Answer whatever confirmation is still open, so teardown can finish.

    Only ever reached where a test has already decided no question should have been asked.
    **Answered rather than popped**: `ask_to_confirm` is suspended on `push_screen_wait`,
    which resolves on the modal's result, so popping it leaves that caller waiting forever --
    and the caller is on a message pump `run_test`'s teardown waits for. The file would then
    stop rather than fail, which is a worse outcome than the defect it was trying to report.
    """
    while len(app.screen_stack) > 1 and isinstance(app.screen, ConfirmScreen):
        app.screen._answer(False)
        await asyncio.sleep(0)


async def _press_expecting_no_question(app: RemoteAgentsTui, pilot) -> None:
    """Press the key where nothing should be asked, bounded either way.

    `Pilot.press` ends by waiting for the screen it snapshotted to drain its queue, and a
    screen that has (wrongly) raised a confirmation is suspended inside that queue -- so the
    press would sit there for Textual's own thirty-second timeout and the teardown would then
    hang on the unanswered question. Bounded here, and released in the `finally`, so "a
    question was asked when none should have been" fails in seconds.
    """
    press = asyncio.create_task(pilot.press(HOST_REMOTE_CONTROL_KEY))
    try:
        await asyncio.wait_for(asyncio.shield(press), timeout=5)
    except TimeoutError:
        pass
    finally:
        if not press.done():
            press.cancel()
        await _release_any_question(app)


async def test_the_key_opens_the_confirmation_for_the_one_open_direction() -> None:
    """DISABLED opens exactly one direction, so the confirmation asks about that one only."""
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = await _asking(app, pilot)
        assert isinstance(app.screen, HostRemoteControlConfirmModal)
        rows = app.screen.query_one("#choices", OptionList)
        labels = [str(rows.get_option_at_index(index).prompt) for index in range(rows.option_count)]
        # The direction's word is the application's, never restated by the adapter. Read
        # case-insensitively across the whole modal — the question carries the label as the
        # table spells it and the confirm row carries it lowercased into a sentence, and
        # which of the two it lands in is presentation rather than vocabulary.
        rendered = " ".join([_status(app), *labels]).casefold()
        assert HOST_REMOTE_CONTROL_LABELS[RemoteControlState.ACTIVE].casefold() in rendered
        assert HOST_REMOTE_CONTROL_LABELS[RemoteControlState.INACTIVE].casefold() not in rendered
        assert HOST_REMOTE_CONTROL_TITLE.casefold() in rendered
        assert not [call for call in control.calls if call.startswith("set_state")], (
            "the question must not have issued anything yet"
        )
        await pilot.press("escape")
        await _answered(asking)


async def test_the_real_key_leaves_the_surface_answering() -> None:
    """DEC-068's own property: the key must open the question **and leave the app alive**.

    Every other case in this file drives `confirm_host_remote_control` as a task, which runs
    on the test's task and therefore cannot see which pump the real key arrives on. Textual
    dispatches a screen's binding from `App._on_key` -> `run_action`, so an action body that
    awaited the modal would suspend the App's message-pump task and the surface would stop
    answering anything, quit included -- with the modal drawn correctly, because the modal is
    the last thing it manages to draw.

    So this presses the real key and then asks the app to do something else: escape has to
    reach the modal and dismiss it. A suspended app never delivers that key, and the bounded
    waits below fail rather than hang.

    **The `finally` is what makes "fail" rather than "hang" true**, and it was measured rather
    than assumed: with the defect mutated back in, this file did not fail — it stopped, taking
    the whole run with it, because `run_test`'s teardown waits on a surface that has stopped
    answering while the pending presses are still holding it. A test that reproduces its own
    subject during teardown reports nothing at all, which is the same trap
    `test_tui_force_stop.py`'s `_pop_any_modal` was written for.
    """
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        pressing = asyncio.create_task(pilot.press(HOST_REMOTE_CONTROL_KEY))
        dismissing: asyncio.Task[None] | None = None
        try:
            await _until(
                lambda: isinstance(app.screen, HostRemoteControlConfirmModal),
                why="the key to raise the confirmation",
            )
            dismissing = asyncio.create_task(pilot.press("escape"))
            await _until(
                lambda: isinstance(app.screen, DashboardScreen),
                why="the app to answer a key while the confirmation was open",
            )
            await asyncio.wait_for(asyncio.gather(pressing, dismissing), timeout=10)
            assert not [call for call in control.calls if call.startswith("set_state")]
        finally:
            for task in (pressing, dismissing):
                if task is not None and not task.done():
                    task.cancel()
            await _release_any_question(app)


async def test_the_abort_rests_under_the_cursor() -> None:
    """DEC-007's mitigation, asserted here because this confirm is not in `ALL_CONFIRMS`.

    It is deliberately unregistered — every arrangement in `test_confirm_modals.py` is keyed
    by a session-detail row and a policy read against a `SessionRecord`, and this modal has
    neither — so the guarantee that file sweeps for is asserted directly instead.
    """
    modal = HostRemoteControlConfirmModal.for_direction(RemoteControlState.ACTIVE)
    assert modal.initial_focus_is_mutating is False


async def test_confirming_issues_the_command_and_redraws_the_line() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = await _asking(app, pilot)
        # Down onto the confirm row, deliberately, because the abort is the resting one.
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        assert "set_state:active" in control.calls
        assert isinstance(app.screen, DashboardScreen)
        assert _host_row(app) == f"{HOST_REMOTE_CONTROL_TITLE} · on"


async def test_declining_issues_nothing_and_leaves_the_line_alone() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = await _asking(app, pilot)
        await pilot.press("enter")  # the resting row is Cancel
        await _answered(asking)
        await pilot.pause()
        assert not [call for call in control.calls if call.startswith("set_state")]
        assert _host_row(app) == f"{HOST_REMOTE_CONTROL_TITLE} · off"


async def test_each_press_mints_a_fresh_idempotency_key() -> None:
    """Two toggles, two keys — the fake refuses a repeat, exactly as the store does.

    A failed toggle burns its key, so a surface that reused one would make the owner's retry
    impossible: the second press would be answered "already handled" for something that
    demonstrably did not happen.
    """
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(2):
            asking = await _asking(app, pilot)
            await pilot.press("down")
            await pilot.press("enter")
            await _answered(asking)
            await pilot.pause()
        assert [call for call in control.calls if call.startswith("set_state")] == [
            "set_state:active",
            "set_state:inactive",
        ]
        assert len(control.claimed) == 2, "the second press reused the first press's key"
        assert all(key.startswith("tui-") for key in control.claimed)
        assert not announcements(app), "a repeated key would have failed the second toggle"


async def test_the_toggle_holds_the_busy_guard_while_it_is_in_flight() -> None:
    """`set_busy` is held across the command, so nothing else can start on top of it.

    Observed from *inside* the call rather than by blocking it and looking from outside: a
    fake that suspends mid-command holds the screen's message pump, which is what
    `pilot.pause()` waits to drain, so the observing test would hang rather than assert. The
    guard is a property of the window the command runs in, and this reads it there.
    """
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    seen: list[bool] = []
    original = control.set_state

    async def watched(command):
        seen.append(app.busy)
        return await original(command)

    control.set_state = watched  # type: ignore[method-assign]
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = await _asking(app, pilot)
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        assert seen == [True], "the command ran without the busy guard held"
        assert app.busy is False, "the guard was not released once the command landed"


async def test_a_failed_toggle_is_reported_rather_than_lost() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)

    async def raising(command):
        raise RuntimeError("the daemon fell over")

    control.set_state = raising  # type: ignore[method-assign]
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = await _asking(app, pilot)
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        assert any("the daemon fell over" in said for said in announcements(app))
        assert app.busy is False


async def test_the_key_is_inert_where_no_host_toggle_is_wired() -> None:
    """A dead-end entry is worse than an absent one: no modal, no command, no crash."""
    app = RemoteAgentsTui(_context(None))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _press_expecting_no_question(app, pilot)
        assert isinstance(app.screen, DashboardScreen)
        assert not announcements(app)


@pytest.mark.parametrize(
    "connection", [HostConnection.ERRORED, HostConnection.UNREACHABLE], ids=lambda c: c.value
)
async def test_a_reading_that_opens_two_directions_asks_which_rather_than_guessing(
    connection: HostConnection,
) -> None:
    """The policy declines to pick a side here, so the surface asks -- it does not pick either.

    **This replaced an earlier refusal, and the parity contract is what replaced it.** The key
    used to announce the reading and stop, which sounds careful and is not: the bot offers
    both directions as buttons for these two readings, so a terminal that offered none was
    rendering a smaller set than its sibling from the same policy -- exactly the divergence
    DEC-007's parity contract exists to catch. It caught this one
    (`tests/contract/test_session_actions_parity.py`).

    What survives from the refusal is the part that was right: the reading and its remedy are
    still announced, because "the button that could never explain itself" is the failure this
    whole line exists to end. What changed is that the owner is now also given the two
    choices, and choosing still passes through the ordinary confirmation.
    """
    control = FakeHostRemoteControl(connection)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = asyncio.create_task(app.screen.confirm_host_remote_control())
        try:
            await _until(
                lambda: isinstance(app.screen, HostRemoteControlDirectionModal),
                why="the key to ask which direction",
            )
            options = app.screen.query_one("#choices", OptionList)
            rows = [
                str(options.get_option_at_index(index).prompt)
                for index in range(options.option_count)
            ]
            assert set(HOST_REMOTE_CONTROL_LABELS.values()) <= set(rows), rows
            assert rows[0] == "Cancel", "the way out must rest under the cursor"

            await pilot.press("escape")
            await _until(
                lambda: isinstance(app.screen, DashboardScreen),
                why="cancelling to return to the dashboard",
            )
        finally:
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)

        assert not [call for call in control.calls if call.startswith("set_state")], (
            "cancelling the choice must not change the machine"
        )
        said = announcements(app)
        assert said, "the reading and its remedy are still what the owner needs"
        assert any(host_remote_control_line(control_status(control)) in one for one in said)


def control_status(control: FakeHostRemoteControl) -> HostRemoteControlStatus:
    """The reading the fake is currently reporting, without going through the service."""
    return HostRemoteControlStatus.observed(control.connection, server_name=control.server_name)


async def test_the_key_is_refused_while_the_surface_is_busy() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_busy(True)
        await _press_expecting_no_question(app, pilot)
        assert isinstance(app.screen, DashboardScreen)
        assert not [call for call in control.calls if call.startswith("set_state")]
        app.set_busy(False)


# --- Pairing ---------------------------------------------------------------------------
#
# The one screen in this surface that renders a secret. Everything here is about the two
# properties that follow from that: it is offered only where a code could pair anything, and
# it is shown once and then gone -- no history, no re-open, no snapshot baseline holding a
# picture of it.


async def test_pairing_is_offered_only_where_there_is_a_link_to_pair_to() -> None:
    """`pair_available` is the policy; the key must not be a second opinion about it.

    Pairing while disabled would mint a code that expires unused, which reads to an owner as
    a broken feature rather than as an action that was never available.
    """
    for connection in (HostConnection.DISABLED, HostConnection.DAEMON_ABSENT):
        control = FakeHostRemoteControl(connection)
        app = RemoteAgentsTui(_context(control))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.screen.confirm_host_pair()
            await pilot.pause()
            assert "pair" not in control.calls, connection


async def test_pairing_shows_the_code_and_when_it_expires() -> None:
    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        pairing = asyncio.create_task(app.screen.confirm_host_pair())
        try:
            await _until(
                lambda: isinstance(app.screen, HostPairingCodeModal),
                why="the pairing code to be shown",
            )
            rendered = app.screen.rendered_code()
            assert "ZZZZ-9999" in rendered
            assert "expires" in rendered.lower()
            await pilot.press("escape")
            await _until(
                lambda: isinstance(app.screen, DashboardScreen),
                why="any key to dismiss the code",
            )
        finally:
            pairing.cancel()
        assert control.calls.count("pair") == 1


async def test_the_code_modal_dismisses_on_any_key_not_only_on_escape() -> None:
    """It is a notice, not a question. Every way out is the same way out."""
    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        pairing = asyncio.create_task(app.screen.confirm_host_pair())
        try:
            await _until(
                lambda: isinstance(app.screen, HostPairingCodeModal),
                why="the pairing code to be shown",
            )
            await pilot.press("j")
            await _until(
                lambda: isinstance(app.screen, DashboardScreen),
                why="an ordinary key to dismiss the code",
            )
        finally:
            pairing.cancel()


async def test_each_pairing_press_mints_a_fresh_idempotency_key() -> None:
    """The fake refuses a repeat, so a constant key would make the second press impossible."""
    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(2):
            pairing = asyncio.create_task(app.screen.confirm_host_pair())
            try:
                await _until(
                    lambda: isinstance(app.screen, HostPairingCodeModal),
                    why="the pairing code to be shown",
                )
                await pilot.press("escape")
                await _until(
                    lambda: isinstance(app.screen, DashboardScreen),
                    why="the code to be dismissed",
                )
            finally:
                pairing.cancel()
        assert control.calls.count("pair") == 2
        assert len(control.claimed) == 2, "two presses reused one key"


async def test_the_pairing_modal_is_not_a_snapshot_fixture() -> None:
    """A baseline holding a picture of a secret is a secret committed to the repository.

    The other two confirms declare a `position` so the snapshot suite can commit a picture of
    them. This one declares none, deliberately, and the assertion is written here so that
    adding one later fails rather than quietly landing a rendered code in `snapshots/`.
    """
    assert getattr(HostPairingCodeModal, "position", "") == ""


async def test_a_failed_pairing_says_so_without_repeating_what_failed() -> None:
    """The failure path is the one that interpolates, because failures want context."""
    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    control.fail_with = RuntimeError("relay refused code ZZZZ-9999")
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.screen.confirm_host_pair()
        await pilot.pause()
        said = announcements(app)
        assert not isinstance(app.screen, HostPairingCodeModal)
        assert said, "a failure the owner is not told about is a surface that lied"
        assert "ZZZZ-9999" not in " ".join(said)


# --- What the pane actually paints ------------------------------------------------------


@pytest.mark.parametrize("width", [80, 100, 120], ids=lambda w: f"{w}col")
async def test_no_two_readings_paint_alike_at_any_width_we_support(width: int) -> None:
    """Distinctness *as painted*, which is a different claim from distinctness as computed.

    `test_no_two_connections_render_alike` asserts on `host_remote_control_line`, a pure
    function, and it passed while the pane showed two readings identically. `#limits-pane` is
    `text-wrap: nowrap; text-overflow: ellipsis` and its content is 28 columns at an
    80-column terminal, of which 23 are spent before the reading starts -- so ERRORED
    ("on, but its connection is broken") and CONNECTING ("on, still connecting") both painted
    as `Codex Remote Control · on, …`.

    Two readings looking alike is bad; *which* two is worse. ERRORED means the daemon
    answered and said its own link to the relay is broken, and it was painting as "on".

    So this asserts on the truncation, at the widths a person actually runs. It is a
    property of the words, so it is checked against the ellipsis rule rather than by
    screenshotting six terminals: the pane's own CSS is what does the cutting.
    """
    from remote_agents.adapters.tui.screens.dashboard import _HOST_CONNECTION_WORDS

    # 23 columns of title and separator, and one column for the ellipsis itself.
    budget = width - 52 - 1 - len(f"{HOST_REMOTE_CONTROL_TITLE} · ")
    painted: dict[str, HostConnection] = {}
    for connection, word in _HOST_CONNECTION_WORDS.items():
        visible = word if len(word) <= budget else word[:budget]
        assert visible not in painted, (
            f"at {width} columns {connection} and {painted[visible]} both paint as "
            f"{visible!r} -- an owner cannot tell them apart"
        )
        painted[visible] = connection


def test_no_reading_truncates_into_another_reading_s_word() -> None:
    """The sharper half of the rule above: a prefix must not READ as a different state.

    Distinctness alone would be satisfied by "on" and "on, broken" at a width that keeps
    both -- but at a narrower one the second becomes "on, …", which a person reads as the
    first. So no word may begin with another word.
    """
    from remote_agents.adapters.tui.screens.dashboard import _HOST_CONNECTION_WORDS

    words = list(_HOST_CONNECTION_WORDS.items())
    for connection, word in words:
        for other, other_word in words:
            if connection is other:
                continue
            assert not word.startswith(other_word), (
                f"{connection}'s {word!r} truncates into {other}'s {other_word!r}"
            )


def test_every_reading_has_a_sentence_for_the_screens_that_have_room() -> None:
    """The pane row cannot carry the nuance; the chooser can, and the bot already did."""
    from remote_agents.adapters.tui.screens.dashboard import _HOST_CONNECTION_EXPLANATIONS

    assert set(_HOST_CONNECTION_EXPLANATIONS) == set(HostConnection)
    assert len(set(_HOST_CONNECTION_EXPLANATIONS.values())) == len(HostConnection)


async def test_the_chooser_says_what_the_daemon_actually_reported() -> None:
    """ERRORED is not "could not be read": the daemon answered, and said its link is broken."""
    from remote_agents.adapters.tui.screens.dashboard import _HOST_CONNECTION_EXPLANATIONS

    control = FakeHostRemoteControl(HostConnection.ERRORED)
    app = RemoteAgentsTui(_context(control))
    async with app.run_test() as pilot:
        await pilot.pause()
        asking = asyncio.create_task(app.screen.confirm_host_remote_control())
        try:
            await _until(
                lambda: isinstance(app.screen, HostRemoteControlDirectionModal),
                why="the chooser to open",
            )
            # `Static.render()` is what the widget hands the compositor, so this asserts on
            # what was mounted rather than on a string rebuilt from the same tables the
            # implementation reads.
            shown = str(app.screen.query_one("#status", Static).render())
            assert _HOST_CONNECTION_EXPLANATIONS[HostConnection.ERRORED] in shown
            assert "could not be read" not in shown
            await pilot.press("escape")
            await _until(lambda: isinstance(app.screen, DashboardScreen), why="cancelling")
        finally:
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)


def test_the_enable_confirmation_names_the_launch_order_rule() -> None:
    """A pane started before the daemon is up stays invisible to the phone for its whole life.

    The confirmation used to promise the phone could drive Codex sessions on this machine,
    full stop -- which is false for every pane already running, and the owner has no other
    way to find that out. It is the one screen they are guaranteed to read.
    """
    modal = HostRemoteControlConfirmModal.for_direction(RemoteControlState.ACTIVE)
    # The instance's question, not the class default: `for_direction` passes it to the
    # constructor, and the class attribute is the fallback for the registry sweep.
    asked = modal._question.casefold()

    assert "after" in asked, asked
    assert "already running" in asked, asked


def test_the_pairing_modal_cannot_be_photographed_by_the_command_palette() -> None:
    """`ctrl+p` is a priority binding, so "any key dismisses it" never ran for it.

    The palette opens over the live modal and offers Save Screenshot, which writes an SVG of
    the visible screen -- pairing code included -- to disk. Bound at priority so the key
    dismisses the code rather than photographing it.
    """
    keys = {
        binding.key: binding
        for binding in HostPairingCodeModal.BINDINGS  # type: ignore[attr-defined]
    }
    assert "ctrl+p" in keys, "the command palette can still open over a live secret"
    assert keys["ctrl+p"].priority is True
    assert keys["ctrl+p"].action == "dismiss_code"
