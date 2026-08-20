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
        "tmux", "-L", socket, "list-panes", "-t", "ra-console:",
        "-F", "#{pane_index}|#{pane_id}|#{pane_width}|#{pane_height}",
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
                "tmux", "-L", console_socket, "list-windows",
                "-t", "ra-console:", "-F", "#{window_index}",
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

        # The key budget, installed on this socket and nowhere else.
        keys = await _run("tmux", "-L", console_socket, "list-keys", "-T", "root")
        for binding in CONSOLE_BINDINGS:
            assert f" {binding.key} " in keys, f"{binding.key} is not bound: {keys}"

        # A real client, so the bindings are exercised as bindings.
        await _run(
            "tmux", "-L", host_socket, "new-session", "-d", "-s", "host", "-x", "200", "-y", "50",
            "tmux", "-L", console_socket, "attach-session", "-t", "ra-console:",
        )
        await asyncio.sleep(2.0)
        assert await _active_pane(console_socket) == by_slot["surface"].pane_id, (
            "the console must rest on the projects pane, not on whatever was split last"
        )

        focus_key = next(
            binding.key
            for binding in CONSOLE_BINDINGS
            if binding.action is ConsoleBindingAction.FOCUS_NEXT_PANE
        )
        await _type(host_socket, focus_key)
        second = await _active_pane(console_socket)
        assert second != by_slot["surface"].pane_id, "the focus key moved nothing"
        await _type(host_socket, focus_key)
        third = await _active_pane(console_socket)
        assert third not in {by_slot["surface"].pane_id, second}, "the focus key does not cycle"
        await _type(host_socket, focus_key)
        assert await _active_pane(console_socket) == by_slot["surface"].pane_id, (
            "three presses over three panes must come back to where they started"
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
                "python3", "-c",
                "import asyncio,sys;"
                "from remote_agents.adapters.tmux.gateway import TmuxGateway;"
                "from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner;"
                "from remote_agents.application.console import ConsoleComposer;"
                "from pathlib import Path;"
                f"c=ConsoleComposer(TmuxGateway('{console_socket}',AsyncTmuxRunner()),"
                f"('sleep','600'),Path('{tmp_path}'));"
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
        await _run(
            "tmux", "-L", console_socket, "new-session", "-d", "-s", name, "sleep", "600"
        )
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
            "tmux", "-L", host_socket, "new-session", "-d", "-s", "host", "-x", "200", "-y", "50",
            "tmux", "-L", console_socket, "attach-session", "-t", "ra-console:",
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
                "uv",
                "run",
                "--project",
                str(Path(__file__).resolve().parents[2]),
                "remote-agents",
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
