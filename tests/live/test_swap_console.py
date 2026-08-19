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

import os
from pathlib import Path

import pytest
from live_probe import MARKER, probe_profile

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.console import ConsoleComposer
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
        self.composer = ConsoleComposer(self.gateway, ("sleep", "600"), home)

    async def build(self, home: Path) -> None:
        """A console shaped like the real one: a surface pane and a second pane beside it.

        The second pane is not decoration. Killing a window's last pane destroys its session
        (Claim 11's neighbour), so a console of one pane is one force-stop away from being
        gone — the hazard this sub-plan names, and the reason the three-pane design exists.
        Every drive here therefore runs against a console that has more than the slot.
        """
        await self.gateway.create_console(("sleep", "600"), home)
        await self.runner.run(*self.base, "split-window", "-d", "-t", "ra-console:", "sleep", "600")

    async def start(self) -> SessionId:
        session_id = SessionId.new()
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

        await console.composer.show_projects()

        assert await console.slot_pane() == surface, "the surface did not come back to the slot"
        assert await console.home_panes(agent_session) == [agent], "the agent did not go home"
        assert {"ra-console", f"ra-{agent_session}"} <= await console.sessions()
        assert MARKER in await console.gateway.capture(agent_session)
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
