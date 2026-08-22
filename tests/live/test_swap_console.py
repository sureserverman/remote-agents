"""The exchange, driven end to end against real tmux by the composer that will do it.

Not a `swap-pane` test. `tests/contract/adapters/tmux/test_feature_probe.py` Claim 12
already pins what tmux does with the command; what is unproven until here is the
**choreography** — that `ConsoleComposer` reads the arrangement, picks the right two panes,
and leaves the server in a state the next call can read correctly. A recording double
cannot prove that, because the double is written by the same hand as the composer and would
agree with it about exactly the thing worth doubting.

The precedent is Sub-plan 1's Task 3.2, driven against real tmux for the same reason.

Everything runs on a disposable socket named `remote-agents-test-<hex>`. The production
server is never touched, which matters more here than anywhere else in the suite: this file
moves panes belonging to live agent sessions, and the real server has the owner's.
"""

from __future__ import annotations

# Two drills were removed here when the tab mechanism retired (Sub-plan 3, Task 2.4), and the
# coverage they represented is worth naming rather than quietly losing:
#
# * `test_a_tabbed_session_that_never_moved_is_attached_at_home` — that a session linked into
#   the console as a tab, but never displaced, still hands out an attach command naming its
#   *own* session. It exercised DEC-039's correction: tmux lists a linked window's panes under
#   both sessions, and `inventory`'s first-wins dedup was choosing by alphabetical order.
# * `test_interruption_a_tabbed_agent_left_displayed_still_recovers` — the same duplicate seen
#   by `pane_arrangement` rather than `inventory`, which made recovery talk about "session
#   None's window" forever.
#
# Both needed `link_session_window` to set up, and no code path can produce a linked window
# any more — the codec cannot build the verb and an architecture test keeps it that way. The
# *decoding* rule they proved is still enforced, at unit level, where the listing lines are
# supplied directly rather than produced by a mechanism that no longer exists:
# `tests/unit/adapters/tmux/test_inventory_console_safety.py` (which session hosts a pane is
# not decided by alphabetical order) and `tests/unit/adapters/tmux/test_arrangement.py`.
import os
from pathlib import Path

import pytest
from live_probe import MARKER, probe_profile, process_gone

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.commands import (
    ForceStopCommand,
    GracefulStopCommand,
    LaunchCommand,
)
from remote_agents.application.console import ConsoleComposer
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_PROFILE = ProfileId("probe")
_PROJECT = ProjectId("qualification")


class LiveConsole:
    """The disposable server, its gateway, and the composer under drive."""

    def __init__(self, socket: str, home: Path) -> None:
        self.socket = socket
        self.runner = AsyncTmuxRunner()
        self.base = ("tmux", "-L", socket)
        self.gateway = TmuxGateway(socket, self.runner, intent_directory=home / "intents")
        self.terminal = TmuxTerminal(
            self.gateway, {_PROJECT: home}, {_PROFILE: probe_profile()}, startup_timeout=15
        )
        self.composer = ConsoleComposer(
            self.gateway, ("sleep", "600"), home, projects_command=("true",)
        )
        self.home = home

    async def build(self, home: Path) -> None:
        """A console shaped like the real one: a surface pane and a second pane beside it.

        **Built through `ensure` and `settle`, not through `create_console`.** That pair is
        the composer's own start path, and `settle` is what marks the left slot as the projects
        surface — every exchange here depends on that mark to find it again.

        Built by calling the gateway directly, the fixture
        produced a console the production code would never produce — an unmarked surface — and
        three drives in this file failed the moment the surface stopped being inferred from
        absence of identity. A live fixture that skips the path production takes is a fixture
        testing something nobody ships.

        The second pane is not decoration. Killing a window's last pane destroys its session
        (Claim 11's neighbour), so a console of one pane is one force-stop away from being
        gone — the hazard this sub-plan names, and the reason the three-pane design exists.
        Every drive here therefore runs against a console that has more than the slot.
        """
        assert await self.composer.ensure(), "the console could not be prepared"
        assert (await self.composer.settle()).settled, "the console did not start at rest"
        await self.runner.run(*self.base, "split-window", "-d", "-t", "ra-console:", "sleep", "600")
        marked = [pane for pane in await self.gateway.pane_arrangement() if pane.surface]
        assert len(marked) == 1, (
            f"the console start path did not mark exactly one surface: {marked}"
        )

    async def start(self, session_id: SessionId | None = None) -> SessionId:
        """Launch a stand-in agent, optionally under a chosen id.

        The id is choosable because one test needs to bracket the literal string `console` in
        tmux's alphabetical listing order, and random ids make that a coin flip rather than a
        test — see `test_a_tabbed_session_that_never_moved_is_attached_at_home`.
        """
        session_id = SessionId.new() if session_id is None else session_id
        started = await self.terminal.launch(session_id, _PROJECT, _PROFILE)
        assert started.live, f"the stand-in agent did not start: {started.detail}"
        return session_id

    async def slot_pane(self) -> str:
        """The pane currently in the console's left slot, read as a position every time."""
        panes = await self.runner.run(
            *self.base, "list-panes", "-t", "ra-console:0", "-F", "#{pane_index}|#{pane_id}"
        )
        return min(
            (line.split("|") for line in panes.splitlines() if line),
            key=lambda field: int(field[0]),
        )[1]

    async def console_panes(self) -> list[str]:
        """Every pane the console's own window 0 currently holds, in position order."""
        panes = await self.runner.run(
            *self.base, "list-panes", "-t", "ra-console:0", "-F", "#{pane_id}"
        )
        return [line.strip() for line in panes.splitlines() if line.strip()]

    async def home_panes(self, session_id: SessionId) -> list[str]:
        panes = await self.runner.run(
            *self.base, "list-panes", "-t", f"ra-{session_id}:", "-F", "#{pane_id}"
        )
        return [line.strip() for line in panes.splitlines() if line.strip()]

    async def sessions(self) -> set[str]:
        return set(
            (await self.runner.run(*self.base, "list-sessions", "-F", "#{session_name}")).split()
        )

    async def teardown(self) -> None:
        try:
            await self.runner.run(*self.base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / self.socket).unlink(missing_ok=True)


def live_console(home: Path) -> LiveConsole:
    return LiveConsole(f"remote-agents-test-{SessionId.new().value.hex}", home)


async def test_the_swap_round_trip_leaves_both_sessions_alive_and_everything_home(
    tmp_path: Path,
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    console = live_console(tmp_path)
    try:
        await console.build(tmp_path)
        surface = await console.slot_pane()
        agent_session = await console.start()
        agent = (await console.home_panes(agent_session))[0]

        await console.composer.show(agent_session)

        assert await console.slot_pane() == agent, "the console's left slot does not hold the agent"
        assert await console.home_panes(agent_session) == [surface], (
            "the agent's own window does not hold the surface the exchange displaced"
        )
        assert {"ra-console", f"ra-{agent_session}"} <= await console.sessions(), (
            "an exchange took a session with it"
        )
        # The agent is still the agent wherever it is being shown: the capture that feeds
        # readiness, trust and quiet-watching reads it through the console (Sub-plan 1).
        assert MARKER in await console.gateway.capture(agent_session)
        # And the copyable attach command names where the pane is being shown, not the window
        # it started in — the Stage 1 gate's carried obligation, proved against real hosting
        # rather than against a decoded string. Naming `ra-<uuid>:` here would hand the owner
        # a command that attaches them to the projects surface with nothing reporting an error.
        displaced = await console.terminal.copy_attach(agent_session)
        assert displaced is not None and displaced.endswith("-t ra-console:"), (
            f"a displaced agent's attach command does not follow it: {displaced}"
        )

        await console.composer.show_projects()

        assert await console.slot_pane() == surface, "the surface did not come back to the slot"
        assert await console.home_panes(agent_session) == [agent], "the agent did not go home"
        assert {"ra-console", f"ra-{agent_session}"} <= await console.sessions()
        assert MARKER in await console.gateway.capture(agent_session)
        home = await console.terminal.copy_attach(agent_session)
        assert home is not None and home.endswith(f"-t ra-{agent_session}:"), (
            f"an agent back home is not attached at home: {home}"
        )
    finally:
        await console.teardown()


async def test_changing_agents_never_hosts_one_agent_in_the_others_session(
    tmp_path: Path,
) -> None:
    """The two-exchange rule, where its failure is visible: in tmux's own hosting.

    One exchange would put agent A's pane in `ra-B`'s window. Both processes keep running
    and nothing raises — the record still says two sessions, and each session's window still
    holds *a* pane — so the only place the damage shows is which session hosts which pane.
    That is exactly what is asserted here, rather than the count of `swap-pane` calls, which
    a unit test can already check and which would pass for a composer that issued two
    exchanges against the wrong ends.
    """
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    console = live_console(tmp_path)
    try:
        await console.build(tmp_path)
        surface = await console.slot_pane()
        first = await console.start()
        second = await console.start()
        first_pane = (await console.home_panes(first))[0]
        second_pane = (await console.home_panes(second))[0]

        await console.composer.show(first)
        await console.composer.show(second)

        assert await console.slot_pane() == second_pane, "the second agent is not being shown"
        assert await console.home_panes(first) == [first_pane], (
            "the first agent was not sent home — a direct exchange crossed the two sessions"
        )
        assert await console.home_panes(second) == [surface], (
            "the surface is not parked in the shown agent's window"
        )
        assert {"ra-console", f"ra-{first}", f"ra-{second}"} <= await console.sessions()
        assert MARKER in await console.gateway.capture(first)
        assert MARKER in await console.gateway.capture(second)

        await console.composer.show_projects()

        assert await console.slot_pane() == surface
        assert await console.home_panes(second) == [second_pane]
    finally:
        await console.teardown()


async def test_interruption_the_other_writer_kills_the_shown_agents_pane(tmp_path: Path) -> None:
    """A second process ends the session while the console is displaying it.

    Standing in for the bot, which is a different process with no composer and so cannot ask
    the console to step aside the way a local stop does (DEC-005). It kills the pane, and
    because a displayed pane physically lives in the console's window, the console loses a
    pane with it — while the projects surface is left in the now-agentless session's window.

    **The console cannot buy that surface back, and must not try.** `swap-pane` trades rather
    than moves, so exchanging the stranded surface with whatever tmux shifted into the slot
    would send *that* console pane out into the defunct session — one pane shorter each time,
    with the defunct session kept alive holding it. So what is asserted is the honest outcome:
    the panes the console still has stay its own, the exchange does not happen, and nothing
    reports rest.

    An earlier version of this test asserted the exchange as success and checked only that the
    slot held the surface and the console still existed. It passed while the console was being
    dismantled a pane at a time, which is the shape of a test agreeing with a bug.
    """
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    console = live_console(tmp_path)
    try:
        await console.build(tmp_path)
        surface = await console.slot_pane()
        agent_session = await console.start()
        await console.composer.show(agent_session)
        assert await console.slot_pane() != surface, "the agent was not displayed"
        before = set(await console.console_panes())

        result = await console.terminal.force_stop(agent_session)
        assert not result.live, f"the other writer's stop did not land: {result.detail}"

        await console.composer.sync(())

        remaining = set(await console.console_panes())
        assert remaining <= before, f"the console gained a pane it did not own: {remaining}"
        assert surface not in remaining, (
            "the surface was traded back into the console, which exiles one of its own panes"
        )
        assert await console.gateway.console_exists() is True, "the console did not survive"
        assert not (await console.composer.recover()).settled, (
            "a console whose surface is stranded outside it reported rest"
        )
    finally:
        await console.teardown()


async def test_interruption_killing_the_console_destroys_the_agent_it_was_showing(
    tmp_path: Path,
) -> None:
    """The swap model's sharpest accepted cost, asserted rather than hoped for.

    This task was planned as "kill the console, confirm the agent's session survives with its
    pane back home or recoverable". tmux does neither: a displayed pane physically lives in
    the console's window, so `kill-session` **destroys the agent's process**, and
    `remain-on-exit` does not help because it governs a process exiting rather than tmux
    killing the pane out from under it. Probed and pinned as Claim 12's neighbour.

    What makes it dangerous rather than merely destructive is the asymmetry asserted below:
    the agent's **session name survives without its agent** — and what keeps it alive is the
    console's own projects surface, still parked in that window. So `list-sessions` shows a
    session that has nothing in it but our dashboard. Reconciliation is *not* fooled, which
    the assertions below check rather than assume: no pane decodes for that identity any more,
    so `inspect` answers `None` and the record is ended honestly. The cost is the agent's
    process, not the record — and a stranded surface, which is the state `recover` reports and
    cannot fix.

    That is why DEC-036's documented "killing ra-console is always safe" no longer holds under
    this model, and why the runbook has to stop saying it (Stage 3). No start-time recovery
    can undo it — the process is gone.

    Asserted here so that if tmux ever changes, the recorded accepted cost is re-examined
    rather than quietly outliving its reason.
    """
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    console = live_console(tmp_path)
    try:
        await console.build(tmp_path)
        agent_session = await console.start()
        agent_pane = (await console.home_panes(agent_session))[0]
        agent_pid = (
            await console.runner.run(
                *console.base, "display-message", "-p", "-t", agent_pane, "#{pane_pid}"
            )
        ).strip()
        await console.composer.show(agent_session)

        await console.runner.run(*console.base, "kill-session", "-t", "ra-console:")

        assert f"ra-{agent_session}" in await console.sessions(), (
            "the agent's own session did not survive the console being killed"
        )
        assert await process_gone(agent_pid), (
            "the displayed agent outlived the console — if tmux now preserves a swapped pane, "
            "the accepted cost recorded against the swap model needs re-examining"
        )
        # And the record must not read as alive: nothing decodes for this identity any more.
        assert await console.gateway.pane_for(agent_session) is None
        assert await console.terminal.inspect(agent_session) is None, (
            "an agent destroyed with the console still reports as an observable session"
        )
    finally:
        await console.teardown()


async def test_integration_the_record_of_a_displaced_agents_stop_is_honest(tmp_path: Path) -> None:
    """Sub-plans 1 and 2 together: the addressing proven through the composer's choreography.

    Sub-plan 1 already proves every pane-following operation reaches an agent displaced by a
    raw `swap-pane`. What is unproven until here is the same thing when the displacement was
    made by the **composer**, and — the half `doctor --history` actually reads — that the
    durable history of such a stop says what happened.

    That distinction is the whole of DEC-022 and DEC-038's third accepted cost. A stop that
    silently missed the agent does not raise; it writes `graceful_stop_never_sent` or
    `graceful_stop_timed_out` and leaves the agent running. So the assertion is on the events,
    not on an exception: `pane_exited` then `cleanup_confirmed` is the record of a stop that
    reached the agent, and it is exactly what the owner reads back.

    `doctor --history` itself is a database read with no tmux in it, so what is exercised here
    is the store it prints from, composed as production composes it.
    """
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    console = live_console(tmp_path)
    connection = open_database(tmp_path / "sessions.sqlite3", migrations=MIGRATIONS)
    try:
        await console.build(tmp_path)
        surface = await console.slot_pane()
        store = SQLiteSessionStore(connection)
        service = SessionService(store, console.terminal, hide_in_console=console.composer.hide)

        graceful = await service.launch(
            LaunchCommand(_PROJECT, _PROFILE, idempotency_key="graceful-displaced")
        )
        await console.terminal.confirm_ready(graceful.session_id, _PROFILE)
        graceful_pane = (await console.home_panes(graceful.session_id))[0]
        await console.composer.show(graceful.session_id)
        assert await console.slot_pane() == graceful_pane, "the agent was not displayed"

        await service.graceful_stop(GracefulStopCommand(graceful.session_id, _PROFILE))

        events = [event.event_type for event in await store.events(graceful.session_id)]
        assert "pane_exited" in events and "cleanup_confirmed" in events, events
        assert not any("never_sent" in event or "timed_out" in event for event in events), (
            f"the stop of a displaced agent recorded a failure it did not have: {events}"
        )
        assert await console.slot_pane() == surface, (
            "the console kept showing the session it had just stopped"
        )

        forced = await service.launch(
            LaunchCommand(_PROJECT, _PROFILE, idempotency_key="forced-displaced")
        )
        await console.terminal.confirm_ready(forced.session_id, _PROFILE)
        forced_pane = (await console.home_panes(forced.session_id))[0]
        forced_pid = (
            await console.runner.run(
                *console.base, "display-message", "-p", "-t", forced_pane, "#{pane_pid}"
            )
        ).strip()
        await console.composer.show(forced.session_id)

        await service.force_stop(ForceStopCommand(forced.session_id))

        assert await process_gone(forced_pid), "a force stop on a displaced agent left it running"
        forced_events = [event.event_type for event in await store.events(forced.session_id)]
        assert "verified_force_stop" in forced_events, forced_events
        assert await console.gateway.console_exists() is True, (
            "stopping a displayed agent took the console with it"
        )
    finally:
        connection.close()
        await console.teardown()
