"""Opt-in proof, on real tmux, that the console comes up whole and its keys work.

The count lives in `ConsolePaneSlot` and nowhere else here. This file said "three panes" in
its title and in an assertion, and both went stale on 2026-08-31 when the console gained a
fourth — red on `main` for four days, unnoticed because live tests are opt-in and no gate
ran the whole file.

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
import re
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import reserved_keys_by_profile
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.application.console import CONSOLE_BINDINGS, ConsoleComposer
from remote_agents.domain.models import SessionId
from remote_agents.ports.console import (
    ConsoleBindingAction,
    ConsolePaneSlot,
)

#: Strips the SGR escapes a `capture-pane -e` carries, so two captures compare as text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
#: A background-colour SGR, in either the 256-colour or the truecolor spelling.
_BACKGROUND = re.compile(r"\x1b\[[0-9;]*?48;[52];")
#: The display identity's sequence marker, which is the part of a row that survives a
#: re-lay at a different width.
_SEQUENCE = re.compile(r"#\d+")

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


async def test_the_console_comes_up_whole_and_its_keys_reach_every_pane(
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
        reserved_keys={},
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
        # Derived from the slot vocabulary rather than written out. This assertion said `3` and
        # had been red on `main` since 2026-08-31, when `ConsolePaneSlot` gained `LIMITS` and
        # the console became four panes ("Give the console its own agent-limits pane"). Nothing
        # noticed for four days because live tests are opt-in and no gate ran this file. A
        # literal count is a second declaration of how many panes there are, and the enum is the
        # first; this now reads the one that cannot go stale.
        assert len(panes) == len(ConsolePaneSlot), panes

        arrangement = await gateway.pane_arrangement()
        by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
        assert set(by_slot) == {slot.value for slot in ConsolePaneSlot}

        # Proportions, read off the server rather than asserted from the argv that asked
        # for them: the left pane takes ~60% of the width, and the feed ~a third of the
        # right-hand column's height. The column is every right-hand slot, not two of them —
        # written as `sessions + feed` it silently omitted the limits pane's rows and measured
        # the feed against a column shorter than the one it is in.
        by_id = {row[1]: row for row in panes}
        left = by_id[by_slot["surface"].pane_id]
        feed = by_id[by_slot["feed"].pane_id]
        right = [by_id[pane.pane_id] for slot, pane in by_slot.items() if slot != "surface"]
        total_width = left[2] + right[0][2] + 1
        assert 0.55 <= left[2] / total_width <= 0.65, (left, right)
        column = sum(row[3] for row in right) + len(right) - 1
        assert 0.28 <= feed[3] / column <= 0.40, (right, feed)

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
        slots = len(ConsolePaneSlot)
        seen = [by_slot["surface"].pane_id]
        for _ in range(slots):
            await _type(host_socket, "C-b")
            await _type(host_socket, "o")
            seen.append(await _active_pane(console_socket))
        assert len(set(seen[:slots])) == slots, f"prefix+o did not reach every pane: {seen}"
        assert seen[slots] == seen[0], (
            f"one press per pane must cycle back to where it started: {seen}"
        )
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
                f"('sleep','600'),Path('{tmp_path}'),projects_command=('true',),"
                "reserved_keys={});"
                "asyncio.run(c.show_projects())",
            ),
            pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
            reserved_keys={},
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
        reserved_keys={},
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
        reserved_keys={},
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
            reserved_keys={},
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


async def _record_numbered(home: Path, session_id: SessionId, sequence: int) -> None:
    """Like `_record_session`, but numbered, so three rows can be told apart on screen."""
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
                SessionDisplayIdentity("qualification", "claude", "regular", sequence),
                SessionState.RUNNING,
                datetime.now(UTC),
            )
        )
    finally:
        connection.close()


def _highlighted_session(capture: str) -> str | None:
    """Which *session* is drawn with the cursor's background, from an `-e` capture.

    Read off the escapes rather than by index, because the whole question is whether the row
    under the cursor is still the row the owner chose — an index would answer a different
    question and answer it wrongly on exactly the tick a row leaves. A session row is
    recognised by its project name; the pane's border and title carry backgrounds too.

    The `#N` display sequence and not the row's rendered text, and the difference is not
    pedantry: re-laying the columns for a narrower pane is exactly what a resize is now
    supposed to do, so the drawn row legitimately changes while the session under the cursor
    does not. Comparing whole rows made this test fail on the fix working — measured, with
    `#3` lit in both captures and only the column widths between them.
    """
    for line in capture.splitlines():
        if "qualification" not in line:
            continue
        # `48;` is the *background* half of an SGR, which is what `render_line` paints under
        # the highlighted row and under no other row of this list. Measured rather than
        # assumed: the theme resolves to a 256-colour background (`48;5;23`), and every
        # unhighlighted row carries `38;5;23` — the same number as a *foreground*. A detector
        # written for `48;2;` truecolor found nothing at all and reported "no cursor".
        if _BACKGROUND.search(line):
            marker = _SEQUENCE.search(_ANSI.sub("", line))
            return marker.group(0) if marker else None
    return None


async def test_the_sessions_cursor_survives_resize_and_tick(tmp_path: Path) -> None:
    """cursor_survives_resize_and_tick — the whole of Stage 1, on the console the owner gets.

    Three RUNNING sessions, the cursor moved off row 0 by real arrow keys, then the two things
    that used to move it back: the sessions pane resized (every DEC-040 exchange and every drag
    emits these) and one full ten-second refresh tick. Both were separate mechanisms — the
    resize reran the whole clear/refill and re-armed the deferred placement, and the tick
    re-read a store whose order was the planner's choice — and neither is visible in a unit
    test of a single screen, which is why this runs against real tmux and real pane processes.
    """
    _live_or_skip()

    home = _fabricated_home(tmp_path)
    for sequence in (1, 2, 3):
        await _record_numbered(home, SessionId.new(), sequence)

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
                str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"),
                "-m",
                "remote_agents",
                "pane",
                slot.name.lower(),
            )
            for slot in ConsolePaneSlot
        },
        reserved_keys={},
    )
    try:
        assert await composer.ensure() is True
        await asyncio.sleep(25.0)

        arrangement = await gateway.pane_arrangement()
        sessions_pane = next(
            pane.pane_id for pane in arrangement if pane.console_slot == "sessions"
        )

        async def capture() -> str:
            return await _run(
                "tmux", "-L", console_socket, "capture-pane", "-pe", "-t", sessions_pane
            )

        # One key at a time: a batched send-keys drops keys during a TUI redraw.
        for _ in range(2):
            await _run("tmux", "-L", console_socket, "send-keys", "-t", sessions_pane, "Down")
            await asyncio.sleep(1.0)

        chosen = _highlighted_session(await capture())
        assert chosen, "no row is drawn with the cursor after two Downs"

        for width in (48, 64):
            await _run(
                "tmux", "-L", console_socket, "resize-pane", "-t", sessions_pane, "-x", str(width)
            )
            await asyncio.sleep(2.0)

        assert _highlighted_session(await capture()) == chosen, "a resize moved the cursor"

        # One full refresh tick, plus room for the read to land and redraw.
        await asyncio.sleep(13.0)

        assert _highlighted_session(await capture()) == chosen, (
            "the ten-second tick moved the cursor"
        )
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass


async def test_the_published_selection_follows_the_cursor_over_real_tmux(tmp_path: Path) -> None:
    """The selection round-trips through a real tmux 3.4 server, written and read by two processes.

    **What this proves, and what it deliberately cannot.** The cursor -> publish half is driven
    in `tests/unit/adapters/tui/test_sessions_pane.py`; what no unit test can answer is whether
    a session-scoped user option survives a real server and is visible to a *different process*
    reading it back. That is what runs here: the gateway publishes exactly as the pane's
    capability does, and a separate `tmux show-options` process reads it.

    The half that is missing is missing for a reason already recorded in
    `adapters/tui/attach.py::hosting_mode`, not for want of trying. A pane surface inside a
    disposable console classifies as FOREIGN, so `console_publish_selection` is never wired and
    a keypress there publishes nothing. That strictness is deliberate and was paid for: when
    the predicate was widened to accept a test socket, a surface inside a throwaway console
    drove the owner's **real** one — panes split into their live console window, a root binding
    installed on their server — because the composition root hardcodes the composer's server to
    `remote-agents`. Driving the cursor here would mean either reintroducing that, or pointing
    this test at the owner's production console. Neither is a test worth having.

    So the gap stays named rather than papered over: until the composer's server stops being
    hardcoded, no live test can drive a pane surface's own keypress into a console.
    """
    _live_or_skip()

    home = _fabricated_home(tmp_path)
    for sequence in (1, 2):
        await _record_numbered(home, SessionId.new(), sequence)

    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())
    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        home,
        projects_command=("true",),
        pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
        reserved_keys={},
    )

    async def read_option_from_another_process() -> str:
        return (
            await _run(
                "tmux",
                "-L",
                console_socket,
                "show-options",
                "-qv",
                "-t",
                "ra-console:",
                "@remote_agents_selected_session",
            )
        ).strip()

    try:
        assert await composer.ensure() is True

        # Never published: an option that was never set reads back as the empty string, which
        # is the same thing "nothing is selected" writes. That equivalence is the reason the
        # decoder needs no branch for it, and it is a claim about tmux, so it is checked here.
        assert await read_option_from_another_process() == ""
        assert await gateway.read_selection() is None

        chosen = SessionId.new()
        await gateway.publish_selection(chosen)
        assert await read_option_from_another_process() == str(chosen)
        assert await gateway.read_selection() == chosen

        # The cursor resting on nothing, which is the publication DEC-062's mitigation is
        # worthless without.
        await gateway.publish_selection(None)
        assert await read_option_from_another_process() == ""
        assert await gateway.read_selection() is None
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass


async def test_the_selection_dies_with_the_console_that_published_it(tmp_path: Path) -> None:
    """Session scope is what makes a stale selection impossible to inherit.

    The option is deliberately on the console *session* rather than on a pane (DEC-038 governs
    identity, not this — see `SELECTED_SESSION_OPTION`). The cost of that choice would be a
    selection outliving its console; this is the check that it does not. A fresh console on the
    same socket name starts with nothing selected, so no chord can ever act on a session a
    previous console had highlighted.
    """
    _live_or_skip()

    home = _fabricated_home(tmp_path)
    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())

    def _composer() -> ConsoleComposer:
        return ConsoleComposer(
            gateway,
            ("sleep", "600"),
            home,
            projects_command=("true",),
            pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
            reserved_keys={},
        )

    try:
        assert await _composer().ensure() is True
        await gateway.publish_selection(SessionId.new())
        assert await gateway.read_selection() is not None

        await _run("tmux", "-L", console_socket, "kill-session", "-t", "ra-console:")
        assert await _composer().ensure() is True

        assert await gateway.read_selection() is None, (
            "a new console inherited the previous one's selection"
        )
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass


async def test_the_read_side_gate_answers_a_real_console_and_a_real_exchange(
    tmp_path: Path,
) -> None:
    """Who may read the console's selection, decided against a real tmux arrangement.

    **This is the half of Stage 3 that real tmux can answer, and the unit tests cannot.** The
    chord itself is a Textual binding inside a pane process, and BL-041 records why no live test
    can drive one: `hosting_mode` classifies a pane surface inside a *disposable* console as
    FOREIGN, deliberately, because the composition root hardcodes the composer's server — the
    one time that strictness was relaxed, a surface in a throwaway console drove the owner's
    real one. So the keypress is covered by `tests/unit/adapters/tui/test_tui_bindings.py`, and
    what runs here is the decision that keypress depends on.

    `holds_console_slot` filters a real `list-panes -a` for two facts at once: the pane carries
    one of the console's slot marks, **and** the console is still the window showing it. The
    unit cases assert that filter against hand-written listing lines; only a real server can say
    whether the marks this composer actually writes decode back through
    `ARRANGEMENT_FORMAT` — and, more to the point, what a real `swap-pane -d` does to the answer.

    That exchange is the case the gate exists for. The mark travels **with the pane** by design
    (DEC-038), so a projects pane parked in an agent's own window keeps the slot it was given
    and would go on reading the console's selection from a window that is not one of its panes —
    while two of the keys behind that selection end a session with no confirmation (DEC-018).
    Nothing about that is visible in a fabricated listing line: it is a fact about tmux moving
    panes between sessions, so it is asserted where tmux is real.
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
        pane_commands={slot: ("sleep", "600") for slot in ConsolePaneSlot},
        reserved_keys={},
    )

    try:
        assert await composer.ensure() is True

        arrangement = await gateway.pane_arrangement()
        marked = {pane.console_slot: pane.pane_id for pane in arrangement if pane.console_slot}
        assert len(marked) == len(ConsolePaneSlot), (
            f"the console did not come up with one marked pane per slot: {marked}"
        )

        # Every one of the console's own panes may read the selection.
        for slot, pane_id in marked.items():
            assert await gateway.holds_console_slot(pane_id) is True, (
                f"the console's own {slot} pane was refused the selection it is entitled to read"
            )

        # A pane on the same server that is not one of the console's is not entitled to it —
        # hazard case 1, a plain `remote-agents tui` started from any shell on this socket.
        outsider_session = f"ra-outsider-{SessionId.new().value.hex[:8]}"
        await _run(
            "tmux",
            "-L",
            console_socket,
            "new-session",
            "-d",
            "-s",
            outsider_session,
            "sleep",
            "600",
        )
        outsider = (
            await _run(
                "tmux",
                "-L",
                console_socket,
                "list-panes",
                "-t",
                f"{outsider_session}:",
                "-F",
                "#{pane_id}",
            )
        ).strip()
        assert outsider, "the outsider session produced no pane"
        assert await gateway.holds_console_slot(outsider) is False, (
            "a pane that is merely on the console's server was allowed to read its selection"
        )

        # Hazard case 2, driven rather than described: exchange the console's left pane out into
        # that session, exactly as DEC-040's `show_detail` does with an agent.
        # `console_slot` is decoded as the *wire* string, not the enum -- `HostedPane` keeps it
        # that way on purpose, so a console left running by an older version decodes as "not a
        # slot I know" instead of raising mid-listing. The projects pane is the one an exchange
        # moves (DEC-040), and its value is `surface` for the same compatibility reason.
        exiled = marked[ConsolePaneSlot.PROJECTS.value]
        await gateway.swap_panes(exiled, outsider)

        after = {pane.pane_id: pane for pane in await gateway.pane_arrangement()}
        assert after[exiled].console_slot, (
            "the slot mark did not travel with the pane, so this test is no longer about DEC-038"
        )
        assert after[exiled].on_console is False, (
            "the exchange did not move the pane off the console"
        )
        assert await gateway.holds_console_slot(exiled) is False, (
            "an exchanged-out console pane kept its right to read the console's selection"
        )

        # And the pane swapped *in* — on the console, carrying no mark of ours — is refused too.
        assert await gateway.holds_console_slot(outsider) is False
    finally:
        try:
            await _run("tmux", "-L", console_socket, "kill-server")
        except RuntimeError:
            pass


async def test_the_function_keys_reach_the_pane_the_owner_is_in_and_the_agent_that_reserves_one(
    tmp_path: Path,
) -> None:
    """The root F-key row's three destinations and its one refusal, on a console that is real.

    **Replaces this file's prefix-chord drill, which went with the layer it drove** -- named
    here by its subject rather than by its symbol, so that the retirement stays greppable. That
    test pressed `prefix + M-s` and asked where the chord landed; the row this drives is bound
    at the *root*, because the one position it exists for -- inside an agent the console has
    exchanged into its left pane, which owns that pane's keyboard (DEC-040) -- is the one a
    prefix binding could only reach by being pressed twice.
    What carries over verbatim is the harness: a second disposable server provides the
    **client**, because a `bind-key -n` binding is only meaningful to a client on a pty and
    `send-keys` writes into a pane without ever consulting a key table.

    Four presses, one console, and each is a different branch of
    `codec._forward_function_key_command`:

    * **F5 with the owner in the limits pane** (branch 1) -- the key goes back to the pane it was
      pressed in and the surface running there refreshes. Observed as content: Claude's stored
      Remote Control default is written into the fabricated HOME *after* the pane has drawn, and
      the row changes from `Claude's default` to `on` only once F5 is pressed. The six-second
      control window before the press is what makes that a redraw rather than a file watch.
    * **F2 from a displayed agent marked `opencode`** (branch 2) -- OpenCode binds F2, so the
      console hands it over instead of stealing it. Observed as *bytes in that pane's pty*,
      which is the only honest observation of "the agent received it": the pane is respawned as
      `sh -c 'stty raw -echo; cat > <file>'`, because a pane's pty starts in canonical mode and
      a plain `cat` handed an escape sequence with no newline in it writes nothing, for ever.
    * **F2 from the same displayed agent marked `claude`** (branch 3) -- the headline. Claude
      reserves nothing, so the key crosses from a pane the console does not own into the
      sessions pane, whose surface opens the five-row Settings screen. This is the position
      DEC-040 puts the owner in and the whole reason the row is bound at the root.
    * **F8 from a `remote-agents attach` client** -- and this one must do *nothing*. A tmux key
      table belongs to the **server**, managed agents attach on that same socket, and without
      the console-client guard the key fires from any client on it (DEC-073(3)). F8 is a
      graceful stop issued with no confirmation at all (DEC-018), so the failure it guards
      against is a session ending under an owner who pressed a key in a terminal with no console
      pane on screen. The client is built from `codec.attach_argv` with the production socket
      swapped for this test's, so the shape being refused is the shape `remote-agents attach`
      actually execs.

    **What the fourth case can and cannot observe, because the first draft of it observed the
    wrong thing and passed against the mutant.** A stop issued in a *disposable* console cannot
    complete: `SessionService` reaches the terminal through a port carrying the **production**
    socket name, so it finds no managed pane for a session that lives on this test's socket. So
    "the store's record is still RUNNING" is true whether or not the key arrived, and asserting
    it proved nothing -- measured, by removing the guard and watching the test stay green. What
    a *delivered* F8 provably does here is draw the stop's own refusal on the sessions pane, in
    these words: `The stop was never sent. Nothing was signalled to the agent and nothing was
    stopped, because this host could not match the session to a live pane it owns ...`. The word
    **stop** is therefore the marker, and it is self-validating: the same absence is asserted of
    the quiet pane *before* the press, so a marker that could never discriminate fails there
    rather than passing here. It is watched for throughout the settle rather than sampled at the
    end of it, because a Textual toast dismisses itself and a single late capture would miss one.

    **The order of the four is load-bearing, not arbitrary.** F8 is pressed while the sessions
    pane still rests on its list with a session under the cursor, because that is the only
    arrangement in which the key would have had something to stop -- pressed over the Settings
    screen the third case opens, the refusal would prove nothing. The two F2 cases run
    reservation-first for the same reason in reverse: the pass-through is checked while the
    sessions pane is demonstrably still on its list, so "Settings did not open" is a fact about
    this press rather than about a screen that was already there.

    **What is asserted, and what is deliberately not.** The mutation this was verified against
    is the removal of `_PRESSED_FROM_THE_CONSOLE` from `_forward_function_key_command`: with it
    gone the fourth case goes red -- the attached client's F8 reaches the sessions pane and the
    refusal above is drawn there -- which is also the proof that the case is not vacuous.
    BL-041 still stands for the surfaces themselves: what each F-key *does* inside a pane is
    unit-covered, and what this file adds is that the key arrives at the right process at all.
    Two of the four cases happen to prove both ends, because the destination surface's reaction
    is the only thing a `capture-pane` can see.
    """
    _live_or_skip()

    from remote_agents.adapters.tmux.codec import attach_argv, console_attach_argv
    from remote_agents.adapters.tui.screens.settings import (
        LIMITS_SOURCE_TITLE,
        PROJECT_ORDER_TITLE,
        SETTINGS_INSTRUCTION,
        SETTINGS_ROWS,
        THEME_TITLE,
    )
    from remote_agents.application.host_remote_control import HOST_REMOTE_CONTROL_TITLE
    from remote_agents.application.remote_control_default import REMOTE_CONTROL_DEFAULT_TITLE
    from remote_agents.domain.models import SessionState

    #: What a terminal sends for each key this presses, as the bytes a pane's pty receives.
    #: Measured through the nested attach this test uses rather than read off a terminfo entry:
    #: the script re-encodes the key on its way out, so the arriving sequence is the only thing
    #: that answers "which pty did it land in".
    sequences = {"F2": "\x1bOQ", "F5": "\x1b[15~", "F8": "\x1b[19~"}
    stored_default_row = f"{REMOTE_CONTROL_DEFAULT_TITLE} · Claude's default"
    redrawn_default_row = f"{REMOTE_CONTROL_DEFAULT_TITLE} · on"
    settings_titles = (
        REMOTE_CONTROL_DEFAULT_TITLE,
        HOST_REMOTE_CONTROL_TITLE,
        LIMITS_SOURCE_TITLE,
        THEME_TITLE,
        PROJECT_ORDER_TITLE,
    )

    home = _fabricated_home(tmp_path)
    console_socket = f"remote-agents-test-{SessionId.new().value.hex}"
    host_socket = f"remote-agents-test-host-{SessionId.new().value.hex}"
    attach_socket = f"remote-agents-test-attach-{SessionId.new().value.hex}"
    session_id = SessionId.new()
    agent_sink = tmp_path / "agent-pane.out"
    claude_settings = home / ".claude" / "settings.json"
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())

    async def capture(pane: str) -> str:
        return await _run("tmux", "-L", console_socket, "capture-pane", "-p", "-t", pane)

    async def draws(pane: str, text: str, *, seconds: float) -> str:
        """Poll a pane until it draws something, rather than sleeping a guessed interval.

        Three Textual apps start at once here and each key travels through a shell and two more
        `tmux` invocations, so how long any of it takes is a property of the host's load. A
        fixed wait turns a busy machine into a red test; a poll turns it into a slow one.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        drawn = ""
        while loop.time() < deadline:
            drawn = await capture(pane)
            if text in drawn:
                return drawn
            await asyncio.sleep(0.2)
        return drawn

    async def never_draws(pane: str, text: str, *, seconds: float) -> str | None:
        """Watch a pane for something that must never appear, and return it if it does.

        A watch rather than a settle-then-capture, which is what this started as. The thing
        being watched for is drawn by a Textual toast, and a toast dismisses itself -- so a
        single capture taken at the end of the wait can miss an announcement that was on
        screen for the whole middle of it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            drawn = await capture(pane)
            if text in drawn.lower():
                return drawn
            await asyncio.sleep(0.2)
        return None

    def handed_to_the_agent(key: str) -> bool:
        if not agent_sink.exists():
            return False
        return sequences[key] in agent_sink.read_text(errors="replace")

    async def waits_for_the_agent(key: str, *, seconds: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            if handed_to_the_agent(key):
                return True
            await asyncio.sleep(0.1)
        return False

    def production_client(argv: tuple[str, ...]) -> tuple[str, ...]:
        """One of the two production attach argvs, aimed at this test's socket instead.

        Built from the codec rather than spelled here, so what the drill attaches is the shape
        the owner's own `remote-agents attach` and console attach exec. Only the socket moves.
        """
        assert argv[:3] == ("tmux", "-L", "remote-agents"), argv
        return ("tmux", "-L", console_socket, *argv[3:])

    async def attach_client(socket: str, inner: tuple[str, ...]) -> None:
        await _run(
            "tmux", "-L", socket, "new-session", "-d", "-s", "host", "-x", "200", "-y", "50", *inner
        )
        await asyncio.sleep(2.0)

    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        home,
        projects_command=("true",),
        pane_commands={
            slot: (
                "env",
                f"HOME={home}",
                # The venv's interpreter directly, **not** `uv run`: four surfaces start at once
                # and four concurrent `uv run` invocations contend on uv's own lock, which
                # `test_each_pane_surface_renders_its_own_content_in_the_console` records
                # reproducing twice before it was diagnosed.
                str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"),
                "-m",
                "remote_agents",
                "pane",
                slot.name.lower(),
            )
            for slot in ConsolePaneSlot
        },
        # **The real fold, not `{}`.** Branch 2 exists only for a profile that declares the key,
        # and the composer refuses to be built without an answer at all — so the reservation
        # reaches the script from the provider's own descriptor (DEC-070) exactly as it does in
        # production. With `{}` the pass-through case would silently become a second branch-3.
        reserved_keys=reserved_keys_by_profile(),
    )
    try:
        assert await composer.ensure() is True
        # Anchors the argument about the limits pane's own sixty-second tick below. Its timer
        # starts when that surface mounts, which cannot be before this line returns.
        started = asyncio.get_running_loop().time()

        installed = await _run("tmux", "-L", console_socket, "list-keys", "-T", "root")
        for key in sequences:
            assert f" {key} " in installed, f"{key} is not bound at the root: {installed}"

        # A managed session, fabricated: schema-2 and pane-scoped (DEC-038) so it can be
        # displayed by exchange, and its pane is a raw-mode sink rather than a `sleep` because
        # one of the four cases is a question about bytes arriving in this pty.
        name = f"ra-{session_id}"
        await _run(
            "tmux",
            "-L",
            console_socket,
            "new-session",
            "-d",
            "-s",
            name,
            f"sh -c 'stty raw -echo; cat > {agent_sink}'",
        )
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

        await attach_client(host_socket, production_client(console_attach_argv()))
        arrangement = await gateway.pane_arrangement()
        by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
        limits_pane = by_slot["limits"].pane_id
        sessions_pane = by_slot["sessions"].pane_id

        # --- Branch 1: F5 pressed in one of the console's own panes ---------------------
        drawn = await draws(limits_pane, stored_default_row, seconds=40.0)
        assert stored_default_row in drawn, f"the limits pane never drew its own rows: {drawn!r}"
        await _run("tmux", "-L", console_socket, "select-pane", "-t", limits_pane)
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text('{"remoteControlAtStartup": true}', encoding="utf-8")
        # The control window: the pane does not watch this file, so what it draws after the
        # press is a consequence of the press. Without this the case would pass on a surface
        # that re-read on its own and never saw the key at all.
        await asyncio.sleep(6.0)
        assert stored_default_row in await capture(limits_pane), (
            "the limits pane picked the written file up without being asked, so the redraw "
            "below would prove nothing about F5"
        )

        await _type(host_socket, "F5")
        redrawn = await draws(limits_pane, redrawn_default_row, seconds=15.0)
        assert redrawn_default_row in redrawn, (
            f"F5 pressed in the limits pane did not reach the surface running there: {redrawn!r}"
        )
        # **The one confound this case has, bounded rather than waved away.** The limits pane
        # re-reads itself on a sixty-second timer (`_LIMITS_AUTO_REFRESH`), which would produce
        # the same row. That timer cannot start before `ensure` returned, so an observation
        # inside fifty-five seconds of `started` is the press's and not the tick's. Measured at
        # roughly thirty on this host; the assertion fails as *inconclusive* on a slower one
        # rather than crediting F5 with a redraw it may not have caused.
        assert asyncio.get_running_loop().time() - started < 55.0, (
            "the redraw landed too close to the limits pane's own sixty-second re-read for the "
            "press to be what caused it"
        )

        # --- DEC-073(3): the same row pressed from a plain agent attach ------------------
        listing = await draws(sessions_pane, "qualification", seconds=30.0)
        assert "qualification" in listing, f"the sessions pane lists no session: {listing!r}"
        # The marker, validated against the pane it is about to be asserted of. A delivered
        # graceful stop puts the row in `stop_requested` (`state_word`); the word `stop` alone
        # is no marker since the facelift, because the action line names `s stop` whenever a
        # row is highlighted. A word that were always absent would make the assertion below
        # unfalsifiable, so it is checked here first.
        assert "stop_requested" not in listing, (
            f"the quiet sessions pane already shows a stop, so it cannot be the marker: {listing!r}"
        )
        await attach_client(attach_socket, production_client(attach_argv(session_id)))
        # **The precondition, asserted rather than assumed.** `attach_client` returns as soon as
        # the outer *host* session exists, which says nothing about whether the inner attach
        # succeeded. If it did not, F8 below is pressed at a pane with no client behind it,
        # nothing happens, and the case passes green while proving nothing about the guard.
        clients = await _run(
            "tmux", "-L", console_socket, "list-clients", "-F", "#{client_session}"
        )
        assert name in clients, (
            f"no client attached to the agent session, so the refusal proves nothing: {clients!r}"
        )

        await _type(attach_socket, "F8")
        # Watched for at least as long as the arrivals above were given to happen in: this
        # asserts an *absence*, and a shorter window would be a race this host happens to win.
        reacted = await never_draws(sessions_pane, "stop_requested", seconds=10.0)
        assert reacted is None, (
            "an unconfirmed graceful stop (DEC-018) was delivered to the sessions pane by a "
            "client attached to the agent rather than to the console, which is exactly the "
            f"reach DEC-073(3) closed: {reacted!r}"
        )
        connection, store = _store(home)
        try:
            record = await store.get(session_id)
        finally:
            connection.close()
        # The second arm, and the weaker one by construction -- see the docstring: a stop
        # issued in a disposable console cannot complete, so this stays RUNNING either way. It
        # is kept because it is the fact the owner actually cares about, and because a future
        # console that *can* complete a stop must fail here rather than quietly widen.
        assert record is not None and record.state is SessionState.RUNNING, record
        assert "qualification" in await capture(sessions_pane), (
            "the sessions pane lost the session it was listing after a foreign client's F8"
        )
        assert not handed_to_the_agent("F8"), (
            "a refused root key was still delivered into the pressing client's own pane"
        )
        await _run("tmux", "-L", attach_socket, "kill-server")

        # --- Branch 2: the agent is displayed, and it reserves this key ------------------
        await composer.show(session_id)
        await asyncio.sleep(2.0)
        displayed = next(
            pane
            for pane in await gateway.pane_arrangement()
            if pane.on_console and pane.pane_index == 0
        )
        assert displayed.pane_id == agent_pane, "the agent was not exchanged into the left slot"

        await _run(
            "tmux",
            "-L",
            console_socket,
            "set-option",
            "-p",
            "-t",
            agent_pane,
            "@remote_agents_profile",
            "opencode",
        )
        await _run("tmux", "-L", console_socket, "select-pane", "-t", agent_pane)
        # The same self-validation the stop marker gets: "Settings did not open" is only a
        # fact about this press if Settings was demonstrably not open before it.
        assert SETTINGS_INSTRUCTION not in await capture(sessions_pane), (
            "the sessions pane was already showing Settings before the reserved key was pressed"
        )
        await _type(host_socket, "F2")
        assert await waits_for_the_agent("F2"), (
            "OpenCode's own F2 was taken from the pane the owner was typing in; the console "
            "must hand a reserved key over rather than steal it"
        )
        assert SETTINGS_INSTRUCTION not in await capture(sessions_pane), (
            "a key the displayed agent reserves also opened Settings in the sessions pane"
        )

        # --- Branch 3: the same key, the same pane, an agent that reserves nothing --------
        await _run(
            "tmux",
            "-L",
            console_socket,
            "set-option",
            "-p",
            "-t",
            agent_pane,
            "@remote_agents_profile",
            "claude",
        )
        handed_over = agent_sink.read_bytes()
        await _type(host_socket, "F2")
        opened = await draws(sessions_pane, SETTINGS_INSTRUCTION, seconds=20.0)
        assert SETTINGS_INSTRUCTION in opened, (
            "F2 pressed inside a displayed agent that reserves nothing did not reach the "
            f"sessions pane, which is the position the whole row exists for: {opened!r}"
        )
        # The count is the screen's own declaration, not a literal: a sixth row added to
        # `SETTINGS_ROWS` and not here must fail rather than pass by drawing five of six.
        assert len(SETTINGS_ROWS) == len(settings_titles)
        for title in settings_titles:
            assert title in opened, f"the Settings screen is missing {title!r}: {opened!r}"
        assert agent_sink.read_bytes() == handed_over, (
            "a key the displayed agent does not reserve was handed to it as well as forwarded"
        )
    finally:
        for socket in (attach_socket, host_socket, console_socket):
            try:
                await _run("tmux", "-L", socket, "kill-server")
            except RuntimeError:
                pass
