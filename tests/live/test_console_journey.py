"""Opt-in composer journey against real tmux: the exchange works, sessions never depend on it.

This proves the *composer's* journey on a disposable socket: `ensure` creates the console,
`open` exchanges the agent's pane into the left slot and sends the projects surface to live
in the agent's own window, `show_projects` brings it back, and killing the console leaves the
managed session observable — DEC-006, one layer up from where the raw operations prove it.

**It used to be a journey about tabs**, because that is how the console used to show a
session: link its window in, select it, unlink it when the session ended. That mechanism
retired with Sub-plan 3's Task 2.4, and the questions it was really asking — does the console
reach the agent, and does the session survive the console — are the ones asked here now.

The managed session is fabricated (options set by hand) rather than launched through an
agent profile: what is under test is window composition, and real launches carry their
own live proofs elsewhere in this directory.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.application.console import ConsoleComposer
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.console import ConsolePaneSlot


def _record(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("qualification"),
        ProfileId("claude"),
        SessionDisplayIdentity("qualification", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


async def test_the_composer_journey_holds_on_real_tmux(tmp_path: Path) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    session_id = SessionId.new()
    socket = f"remote-agents-test-{session_id.value.hex}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner)
    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        tmp_path,
        projects_command=("true",),
        # The real shape: three panes. `sleep` stands in for the surfaces, which are driven
        # for real in `test_three_pane_console.py` — what this file is about is the
        # composer's journey over them.
        pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
    )
    base = ("tmux", "-L", socket)
    try:
        assert await composer.ensure() is True
        assert await gateway.console_exists() is True
        # ensure is idempotent against a console that already exists
        assert await composer.ensure() is True

        name = f"ra-{session_id}"
        await runner.run(*base, "new-session", "-d", "-s", name, "sleep", "600")
        # Pane-scoped, schema 2 — DEC-038. A session-scoped mark names no pane, so such a
        # session is manageable but cannot be *displayed* by exchange, which is exactly what
        # this journey drives. The fixture carried the schema-1 shape from before that
        # decision and the marks went on the session; it silently produced a session the
        # composer could not show, and the exchange assertions below then had nothing to find.
        agent_pane = (
            await runner.run(*base, "list-panes", "-t", f"={name}:", "-F", "#{pane_id}")
        ).strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "claude"),
        ):
            await runner.run(*base, "set-option", "-p", "-t", agent_pane, option, value)

        # Showing a session is an exchange now, not a tab. The console's left slot ends up
        # holding the agent's own pane, and the projects surface goes to live in the agent's
        # window until it is swapped back.
        arrangement = await gateway.pane_arrangement()
        surface = next(pane for pane in arrangement if pane.surface)
        assert any(pane.session_id == session_id for pane in arrangement), (
            "the fabricated session carries no pane identity, so nothing could display it"
        )

        await composer.open(session_id)

        after = await gateway.pane_arrangement()
        displayed = next(pane for pane in after if pane.on_console and pane.pane_index == 0)
        assert displayed.pane_id == agent_pane, "the agent was not shown"
        parked = next(pane for pane in after if pane.pane_id == surface.pane_id)
        assert parked.host == session_id, "the surface did not go to the agent's window"

        # The route back — what the projects key runs.
        await composer.show_projects()
        home = await gateway.pane_arrangement()
        assert (
            next(pane for pane in home if pane.on_console and pane.pane_index == 0).pane_id
            == surface.pane_id
        ), "the projects surface did not come back"

        # DEC-006, re-proved under the swap model: with nothing displayed, killing the
        # console leaves the session alive and observable.
        await runner.run(*base, "kill-session", "-t", "ra-console")
        inventory = await gateway.inventory()
        assert [pane.session_id for pane in inventory.managed] == [session_id]
    finally:
        try:
            await runner.run(*base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)
