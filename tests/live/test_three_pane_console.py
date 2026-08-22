"""Opt-in proof, on real tmux, that the console comes up as three panes and its keys work.

Everything the composer does was proven headless in `tests/unit/application`; what cannot be
proven there is the arrangement a real tmux server actually produces, and whether a real
*client* pressing the console's two root keys reaches what the key budget claims. Both are
the whole point of Stage 2, so both are driven here.

Two sockets, deliberately. The console lives on one; a second disposable server provides the
**client** — a pane running `tmux -L <console> attach-session` — because a root binding is
only meaningful to an attached client, and headless `select-pane` calls would prove the argv
rather than the binding. That is the shape the owner actually uses: a terminal, attached.

The pane surfaces run against a fabricated HOME rather than the owner's, so this test reads
and writes nothing of theirs.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.application.console import CONSOLE_BINDINGS, ConsoleComposer
from remote_agents.domain.models import SessionId
from remote_agents.ports.console import ConsoleBindingAction, ConsolePaneSlot

_REGISTRY = """version: 1
projects:
  - path: {project}
    name: qualification
    area: infra
    enabled: true
    added: 2026-08-20
"""


def _live_or_skip() -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")


def _fabricated_home(root: Path) -> Path:
    """A complete production HOME — config, registry, one project — under tmp_path."""
    home = root / "home"
    project = home / "dev" / "infra" / "qualification"
    project.mkdir(parents=True)
    registry = home / "projects-registry.yaml"
    registry.write_text(_REGISTRY.format(project=project), encoding="utf-8")
    config_directory = home / ".config" / "remote-agents"
    config_directory.mkdir(parents=True)
    state = home / ".local" / "state" / "remote-agents"
    state.mkdir(parents=True)
    (config_directory / "config.toml").write_text(
        f'[paths]\ndev_root = "{home / "dev"}"\n'
        f'registry_path = "{registry}"\n'
        f'database_path = "{state / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    return home


async def _run(*argv: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{argv} failed: {err.decode()}")
    return out.decode()


async def _panes(socket: str) -> list[tuple[int, str, int, int]]:
    listing = await _run(
        "tmux",
        "-L",
        socket,
        "list-panes",
        "-t",
        "ra-console:",
        "-F",
        "#{pane_index}|#{pane_id}|#{pane_width}|#{pane_height}",
    )
    rows = []
    for line in listing.splitlines():
        index, pane_id, width, height = line.split("|")
        rows.append((int(index), pane_id, int(width), int(height)))
    return rows


async def _active_pane(socket: str) -> str:
    return (
        await _run("tmux", "-L", socket, "display-message", "-p", "-t", "ra-console:", "#{pane_id}")
    ).strip()


async def _type(host_socket: str, key: str) -> None:
    """One key at a time, then settle: a batched send-keys drops keys during a TUI redraw."""
    await _run("tmux", "-L", host_socket, "send-keys", "-t", "host:", key)
    await asyncio.sleep(1.0)


async def test_the_console_comes_up_as_three_panes_and_its_keys_reach_them(
    tmp_path: Path,
) -> None:
    _live_or_skip()

    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    host_socket = f"remote-agents-test-host-{SessionId.new().value.hex}"
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())
    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        tmp_path,
        projects_command=("true",),
        # Stand-ins: this test is about the arrangement and the keys. What the surfaces
        # render is the second test below, and every one of their behaviours is pinned
        # headless in tests/unit/adapters/tui.
        pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
    )
    try:
        assert await composer.ensure() is True

        windows = (
            await _run(
                "tmux",
                "-L",
                console_socket,
                "list-windows",
                "-t",
                "ra-console:",
                "-F",
                "#{window_index}",
            )
        ).split()
        assert windows == ["0"], f"the console is one window, not {windows}"

        panes = await _panes(console_socket)
        assert len(panes) == 3, panes

        arrangement = await gateway.pane_arrangement()
        by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
        assert set(by_slot) == {slot.value for slot in ConsolePaneSlot}

        # Proportions, read off the server rather than asserted from the argv that asked
        # for them: the left pane takes ~60% of the width, and the feed ~a third of the
        # right-hand column's height.
        left = next(row for row in panes if row[1] == by_slot["surface"].pane_id)
        sessions = next(row for row in panes if row[1] == by_slot["sessions"].pane_id)
        feed = next(row for row in panes if row[1] == by_slot["feed"].pane_id)
        total_width = left[2] + sessions[2] + 1
        assert 0.55 <= left[2] / total_width <= 0.65, (left, sessions)
        column = sessions[3] + feed[3] + 1
        assert 0.28 <= feed[3] / column <= 0.40, (sessions, feed)

        # The key budget — one key — installed on this socket and nowhere else.
        keys = await _run("tmux", "-L", console_socket, "list-keys", "-T", "root")
        assert len(CONSOLE_BINDINGS) == 1
        for binding in CONSOLE_BINDINGS:
            assert f" {binding.key} " in keys, f"{binding.key} is not bound: {keys}"

        # A real client, so the bindings are exercised as bindings.
        await _run(
            "tmux",
            "-L",
            host_socket,
            "new-session",
            "-d",
            "-s",
            "host",
            "-x",
            "200",
            "-y",
            "50",
            "tmux",
            "-L",
            console_socket,
            "attach-session",
            "-t",
            "ra-console:",
        )
        await asyncio.sleep(2.0)
        assert await _active_pane(console_socket) == by_slot["surface"].pane_id, (
            "the console must rest on the projects pane, not on whatever was split last"
        )

        # Focus moves on tmux's own prefix chord, which is why the console spends no key on
        # it. The claim that a displayed agent swallows the prefix is false — tmux intercepts
        # it in the *client*, before any key reaches the pane — and this is where that is
        # proved rather than asserted, because it is the whole argument for a one-key budget.
        seen = [by_slot["surface"].pane_id]
        for _ in range(3):
            await _type(host_socket, "C-b")
            await _type(host_socket, "o")
            seen.append(await _active_pane(console_socket))
        assert len(set(seen[:3])) == 3, f"prefix+o did not reach all three panes: {seen}"
        assert seen[3] == seen[0], f"three presses over three panes must cycle back: {seen}"
    finally:
        for socket in (host_socket, console_socket):
            try:
                await _run("tmux", "-L", socket, "kill-server")
            except RuntimeError:
                pass


async def test_the_projects_key_brings_the_surface_back_from_a_displayed_agent(
    tmp_path: Path,
) -> None:
    """The route back, driven as the owner drives it: a real client, a real keypress.

    The key runs our own program rather than a tmux command, because tmux cannot read our
    pane marks and work out which exchange brings the surface home. That indirection is
    exactly what a headless test cannot check, so it is checked here.
    """
    _live_or_skip()

    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    host_socket = f"remote-agents-test-host-{SessionId.new().value.hex}"
    session_id = SessionId.new()
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())
    projects_key = next(
        binding.key
        for binding in CONSOLE_BINDINGS
        if binding.action is ConsoleBindingAction.SHOW_PROJECTS
    )
    try:
        composer = ConsoleComposer(
            gateway,
            ("sleep", "600"),
            tmp_path,
            # What the projects key runs. `-c` so the child composer reaches this socket
            # rather than the owner's real one.
            projects_command=(
                "python3",
                "-c",
                "import asyncio,sys;"
                "from remote_agents.adapters.tmux.gateway import TmuxGateway;"
                "from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner;"
                "from remote_agents.application.console import ConsoleComposer;"
                "from pathlib import Path;"
                f"c=ConsoleComposer(TmuxGateway('{console_socket}',AsyncTmuxRunner()),"
                f"('sleep','600'),Path('{tmp_path}'),projects_command=('true',));"
                "asyncio.run(c.show_projects())",
            ),
            pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
        )
        assert await composer.ensure() is True
        surface = next(
            pane for pane in await gateway.pane_arrangement() if pane.console_slot == "surface"
        )

        # A managed session, fabricated: what is under test is the exchange and the key.
        name = f"ra-{session_id}"
        await _run("tmux", "-L", console_socket, "new-session", "-d", "-s", name, "sleep", "600")
        agent_pane = (
            await _run(
                "tmux", "-L", console_socket, "list-panes", "-t", f"={name}:", "-F", "#{pane_id}"
            )
        ).strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
        ):
            await _run(
                "tmux", "-L", console_socket, "set-option", "-p", "-t", agent_pane, option, value
            )

        await composer.show(session_id)
        displayed = [
            pane
            for pane in await gateway.pane_arrangement()
            if pane.on_console and pane.pane_index == 0
        ]
        assert displayed and displayed[0].pane_id == agent_pane, "the agent was not displayed"

        await _run(
            "tmux",
            "-L",
            host_socket,
            "new-session",
            "-d",
            "-s",
            "host",
            "-x",
            "200",
            "-y",
            "50",
            "tmux",
            "-L",
            console_socket,
            "attach-session",
            "-t",
            "ra-console:",
        )
        await asyncio.sleep(2.0)
        await _type(host_socket, projects_key)
        await asyncio.sleep(2.0)

        home = [
            pane
            for pane in await gateway.pane_arrangement()
            if pane.on_console and pane.pane_index == 0
        ]
        assert home and home[0].pane_id == surface.pane_id, (
            "the projects key did not bring the surface back to the left slot"
        )
    finally:
        for socket in (host_socket, console_socket):
            try:
                await _run("tmux", "-L", socket, "kill-server")
            except RuntimeError:
                pass


async def test_each_pane_surface_renders_its_own_content_in_the_console(
    tmp_path: Path,
) -> None:
    """The real `remote-agents pane` processes, in the real three-pane window.

    Against a fabricated HOME, so this reads and writes nothing of the owner's. What it
    proves that the headless tests cannot: the three processes start, compose over one
    SQLite file at the same time, and each draws its own surface rather than the same one.
    """
    _live_or_skip()

    home = _fabricated_home(tmp_path)
    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())
    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        home,
        projects_command=("true",),
        pane_commands={
            slot: (
                "env",
                f"HOME={home}",
                # The venv's interpreter directly, **not** `uv run`. Three surfaces start at
                # once, and three concurrent `uv run` invocations contend on uv's own lock:
                # one loses, exits, and tmux closes its pane, so the console comes up two
                # panes and the test fails somewhere unrelated. Reproduced twice before it
                # was diagnosed — a single `uv run` of the same command is perfectly fine.
                str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"),
                "-m",
                "remote_agents",
                "pane",
                slot.name.lower(),
            )
            for slot in ConsolePaneSlot
        },
    )
    try:
        assert await composer.ensure() is True
        # The surfaces are Textual apps starting three interpreters; give them room.
        await asyncio.sleep(25.0)

        arrangement = await gateway.pane_arrangement()
        by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
        rendered = {}
        for slot, pane in by_slot.items():
            rendered[slot] = await _run(
                "tmux", "-L", console_socket, "capture-pane", "-p", "-t", pane.pane_id
            )

        assert "Choose a project" in rendered["surface"], rendered["surface"]
        assert "qualification" in rendered["surface"], rendered["surface"]
        assert "No managed sessions" in rendered["sessions"], rendered["sessions"]
        assert "No notifications yet." in rendered["feed"], rendered["feed"]
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass


async def test_the_owner_journey_through_the_three_pane_console(tmp_path: Path) -> None:
    """The whole flow, in the console the owner actually gets, driven by real keys.

    Open a session from the sessions pane; the agent appears in the left slot while the
    sessions list and the feed stay on screen beside it; an observation lands in the feed
    *while the agent is in front*; the session is stopped; the projects surface comes back.

    **What is driven by keypress and what is driven by call.** Two things and only two are
    keypresses here: **focus moving** between the panes (`prefix + o`), and **`d` opening the
    session detail while the agent is displayed** — which is the claim that matters, because
    the sessions pane being usable with an agent in front is the whole reason it is the swap
    controller. Everything else is a call, each for a reason:

    - The **launch** runs a real agent binary. The path from the projects pane into
      `SessionService.launch` is already driven against a real agent by
      `test_add_project_and_tui_journey.py`; starting a second one here would prove nothing
      new. The agent is fabricated — a `sleep` carrying schema-2 pane marks, which is what
      every console live test uses.
    - The **exchange** is `composer.show`, because a pane surface bakes in the production
      socket name — a surface inside a disposable console must therefore classify as FOREIGN,
      or it drives the owner's real console, which is what happened when it briefly did not.
      The key-driven exchange has its own proof in
      `test_the_projects_key_brings_the_surface_back_from_a_displayed_agent`.
    - The **stop** is a store event, not `SessionService.force_stop`: what this journey is
      about is the console's reaction to a session ending — the surface coming back — and the
      stop mechanism itself is driven end-to-end in `test_swap_console.py`'s integration
      drill. **So this test does not prove a stop initiated from the sessions pane reaches
      the agent's process**, and does not claim to.
    - The **route back** here is `composer.sync`, standing in for the reload that notices the
      other writer. F12 is pressed as a key in the projects-key test, not in this one.
    """
    _live_or_skip()

    home = _fabricated_home(tmp_path)
    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    host_socket = f"remote-agents-test-host-{SessionId.new().value.hex}"
    session_id = SessionId.new()
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())
    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        home,
        projects_command=("true",),
        pane_commands={
            slot: (
                "env",
                f"HOME={home}",
                # The venv's interpreter directly, **not** `uv run`. Three surfaces start at
                # once, and three concurrent `uv run` invocations contend on uv's own lock:
                # one loses, exits, and tmux closes its pane, so the console comes up two
                # panes and the test fails somewhere unrelated. Reproduced twice before it
                # was diagnosed — a single `uv run` of the same command is perfectly fine.
                str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"),
                "-m",
                "remote_agents",
                "pane",
                slot.name.lower(),
            )
            for slot in ConsolePaneSlot
        },
    )
    try:
        assert await composer.ensure() is True

        # A managed session, fabricated. Marked schema-2 and pane-scoped (DEC-038) so it can
        # be displayed by exchange at all.
        name = f"ra-{session_id}"
        await _run("tmux", "-L", console_socket, "new-session", "-d", "-s", name, "sleep", "600")
        agent_pane = (
            await _run(
                "tmux", "-L", console_socket, "list-panes", "-t", f"={name}:", "-F", "#{pane_id}"
            )
        ).strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "claude"),
        ):
            await _run(
                "tmux", "-L", console_socket, "set-option", "-p", "-t", agent_pane, option, value
            )
        await _record_session(home, session_id)

        await asyncio.sleep(25.0)
        arrangement = await gateway.pane_arrangement()
        by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
        surface = by_slot["surface"]

        await _run(
            "tmux",
            "-L",
            host_socket,
            "new-session",
            "-d",
            "-s",
            "host",
            "-x",
            "200",
            "-y",
            "50",
            "tmux",
            "-L",
            console_socket,
            "attach-session",
            "-t",
            "ra-console:",
        )
        await asyncio.sleep(3.0)

        # The sessions pane lists it, and Enter there opens it.
        listing = await _run(
            "tmux", "-L", console_socket, "capture-pane", "-p", "-t", by_slot["sessions"].pane_id
        )
        assert "qualification" in listing, f"the sessions pane does not list it: {listing}"

        await _type(host_socket, "C-b")
        await _type(host_socket, "o")  # focus moves to the sessions pane
        assert await _active_pane(console_socket) == by_slot["sessions"].pane_id

        # The exchange itself is driven through the composer rather than by pressing Enter,
        # and the reason is a harness limit worth naming rather than hiding. A pane surface
        # builds its composer with the **production** socket name baked in
        # (`bootstrap._console_composer`), so a surface running inside a *disposable* console
        # would drive the owner's real server instead of this test's. The seam it would use is
        # unit-driven — `_console_opener` in `test_sessions_pane.py` — and the *key*-driven
        # exchange has its own live proof next door, in the projects-key test, where the
        # binding runs a command carrying this socket. What is left undriven anywhere live is
        # precisely: a pane surface's own keypress reaching a composer on a test socket.
        await composer.show(session_id)
        await asyncio.sleep(2.0)

        displayed = await gateway.pane_arrangement()
        left = next(pane for pane in displayed if pane.on_console and pane.pane_index == 0)
        assert left.pane_id == agent_pane, "showing the session did not put the agent in front"

        # The right panes are still on screen beside it — the whole point of the redesign.
        still_there = {pane.pane_id for pane in displayed if pane.on_console}
        assert by_slot["sessions"].pane_id in still_there
        assert by_slot["feed"].pane_id in still_there
        parked = next(pane for pane in displayed if pane.pane_id == surface.pane_id)
        assert parked.host == session_id, "the surface did not go to the agent's own window"

        # An observation arrives while the agent is in front.
        await _observe(home, session_id, "May I push to main?")
        await asyncio.sleep(14.0)
        feed = await _run(
            "tmux", "-L", console_socket, "capture-pane", "-p", "-t", by_slot["feed"].pane_id
        )
        assert "May I push to main?" in feed, f"the feed did not carry the observation: {feed}"

        # The detail is one key away from the sessions pane, with the agent still displayed —
        # this part *is* a keypress, on the pane that stays visible, which is the whole reason
        # the sessions pane is the swap controller.
        await _type(host_socket, "d")
        await asyncio.sleep(3.0)
        detail = await _run(
            "tmux", "-L", console_socket, "capture-pane", "-p", "-t", by_slot["sessions"].pane_id
        )
        assert "stop" in detail.lower(), f"no stop is offered from the displayed session: {detail}"

        # The stop, recorded as the detail screen's own action records it.
        await _end_session(home, session_id)
        await composer.sync(())
        await asyncio.sleep(14.0)

        home_again = await gateway.pane_arrangement()
        back = next(pane for pane in home_again if pane.on_console and pane.pane_index == 0)
        assert back.pane_id == surface.pane_id, (
            "the projects surface did not come back after the session was stopped"
        )
    finally:
        for socket in (host_socket, console_socket):
            try:
                await _run("tmux", "-L", socket, "kill-server")
            except RuntimeError:
                pass


def _store(home: Path):
    """A real store over the fabricated HOME's database, migrations applied."""
    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore

    connection = open_database(
        home / ".local" / "state" / "remote-agents" / "sessions.sqlite3", migrations=MIGRATIONS
    )
    return connection, SQLiteSessionStore(connection)


async def _record_session(home: Path, session_id: SessionId) -> None:
    """Put a RUNNING record in the store, so the sessions pane has something to list."""
    from datetime import UTC, datetime

    from remote_agents.domain.models import (
        ProfileId,
        ProjectId,
        SessionDisplayIdentity,
        SessionRecord,
        SessionState,
    )

    connection, store = _store(home)
    try:
        await store.save(
            SessionRecord(
                session_id,
                ProjectId("qualification"),
                ProfileId("claude"),
                SessionDisplayIdentity("qualification", "claude", "regular", 1),
                SessionState.RUNNING,
                datetime.now(UTC),
            )
        )
    finally:
        connection.close()


async def _observe(home: Path, session_id: SessionId, detail: str) -> None:
    """Append one observation to the durable table the feed pane reads."""
    from datetime import UTC, datetime

    from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
    from remote_agents.ports.agent_activity import (
        ActivityConfidence,
        ActivityKind,
        AgentActivity,
    )

    connection, _ = _store(home)
    try:
        await SQLiteActivityStore(connection).append(
            AgentActivity(
                str(session_id),
                ActivityKind.NEEDS_ANSWER,
                detail,
                datetime.now(UTC),
                ActivityConfidence.REPORTED,
            )
        )
    finally:
        connection.close()


async def _end_session(home: Path, session_id: SessionId) -> None:
    """Record the stop the sessions pane's detail screen would issue."""
    from remote_agents.domain.state_machine import LifecycleEvent

    connection, store = _store(home)
    try:
        await store.record_event(session_id, LifecycleEvent.VERIFIED_FORCE_STOP)
    finally:
        connection.close()


async def test_a_console_killed_while_displaying_names_the_session_it_stranded(
    tmp_path: Path,
) -> None:
    """The runbook's dangerous step, pinned — because nothing covered it and it is advice.

    Step 8 of the console acceptance checklist tells the operator to kill `ra-console` while
    an agent is displayed, and promises that a fresh console *names the defunct `ra-<uuid>`
    still holding an old projects surface*. That promise is the only thing standing between
    the operator and a stranded session they never hear about, and it is worth a test rather
    than a trace: a Stage 3 review traced the code and concluded the report could not be
    produced, because a freshly built console marks its own left pane before `settle` runs.

    The trace missed why it does not. A slot counts as present if **any pane anywhere** carries
    its mark, and the stranded surface still carries it — so the fresh console does not adopt
    its own pane, `_adopt_surface` finds exactly one marked pane, sees that its host holds no
    agent, disowns it, and says so. Which is the behaviour the runbook describes.
    """
    _live_or_skip()

    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    session_id = SessionId.new()
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())

    def _composer() -> ConsoleComposer:
        return ConsoleComposer(
            TmuxGateway(console_socket, AsyncTmuxRunner()),
            ("sleep", "600"),
            tmp_path,
            projects_command=("true",),
            pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
        )

    try:
        assert await _composer().ensure() is True

        name = f"ra-{session_id}"
        await _run("tmux", "-L", console_socket, "new-session", "-d", "-s", name, "sleep", "600")
        agent_pane = (
            await _run(
                "tmux", "-L", console_socket, "list-panes", "-t", f"={name}:", "-F", "#{pane_id}"
            )
        ).strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
        ):
            await _run(
                "tmux", "-L", console_socket, "set-option", "-p", "-t", agent_pane, option, value
            )
        await _composer().show(session_id)

        # The dangerous command, exactly as the checklist gives it.
        await _run("tmux", "-L", console_socket, "kill-session", "-t", "ra-console")

        # DEC-040's first accepted cost: the displayed agent went with the console, and its
        # session name did not. That is why the checklist calls this step dangerous.
        remaining = await gateway.pane_arrangement()
        assert not any(pane.session_id == session_id for pane in remaining), (
            "the displayed agent's pane survived a console kill, which DEC-040 says it cannot"
        )
        assert any(pane.console_slot == "surface" for pane in remaining), (
            "the stranded projects surface is what keeps the defunct session alive"
        )

        fresh = _composer()
        assert await fresh.ensure() is True
        report = await fresh.settle()

        assert any(str(session_id) in note for note in report.blocked), (
            f"a restarted console did not name the session it stranded: {report.blocked}"
        )
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass


async def test_a_pane_surface_in_a_test_console_never_reaches_the_production_server(
    tmp_path: Path,
) -> None:
    """The guard on the damage this file did once, asserted where it happened.

    A surface takes its hosting from `$TMUX` and its composer's server from the composition
    root, which hardcodes the production socket. So a surface inside a disposable console must
    classify as **FOREIGN**: anything else and these very tests split panes into the owner's
    live console and install a root binding on their server — which is not hypothetical, it is
    what four leaked panes on this machine were.

    Checked against `hosting_mode` directly rather than by watching the production server,
    because the honest assertion is about the rule, and watching would mean touching the thing
    that must not be touched.
    """
    _live_or_skip()

    from remote_agents.adapters.tui.attach import HostingMode, hosting_mode

    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    try:
        await _run(
            "tmux", "-L", console_socket, "new-session", "-d", "-s", "ra-console", "sleep", "60"
        )
        inside = (
            await _run(
                "tmux",
                "-L",
                console_socket,
                "display-message",
                "-p",
                "-t",
                "ra-console:",
                "#{socket_path}",
            )
        ).strip()
        assert hosting_mode({"TMUX": f"{inside},1,0"}) is HostingMode.FOREIGN, (
            "a surface in a disposable console would build a composer against the owner's "
            "production tmux server"
        )
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass
