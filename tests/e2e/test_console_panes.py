"""The fold, against a real tmux server with a real four-pane console window.

Everything the composer does to fold the column is measured against tmux's own answers here,
because the unit tests can only pin the argv we *send*. Three things in this feature are true
only of real tmux and were measured rather than reasoned about:

- `resize-pane -Z` is a toggle, so re-sending it in the state you want undoes it;
- `swap-pane -d` silently unzooms the window, which is why the hidden state is an option of
  ours and gets re-applied after every exchange;
- a pane's floor is one column, so "slid off the edge" means the window's width minus one.

Run alone. It starts and kills its own server on a `remote-agents-test-` socket and never
touches `-L remote-agents`, where the owner's live agents are.
"""

from __future__ import annotations

import uuid

import pytest

from remote_agents.adapters.tmux.codec import console_target
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.ports.console import PANES_HIDDEN_OPTION

WINDOW_WIDTH = 183
WINDOW_HEIGHT = 44


@pytest.fixture
async def console(tmp_path):
    """A four-pane console window on a throwaway server, in the shape the real one has."""
    socket = f"remote-agents-test-panes-{uuid.uuid4().hex[:8]}"
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner)
    session = console_target().rstrip(":")
    await runner.run(
        "tmux", "-L", socket, "new-session", "-d", "-s", session,
        "-x", str(WINDOW_WIDTH), "-y", str(WINDOW_HEIGHT), "sleep 300",
    )
    # projects | (sessions / limits / feed) -- the console's own shape.
    for direction in ("-h", "-v", "-v"):
        await runner.run(
            "tmux", "-L", socket, "split-window", direction, "-t", console_target(), "sleep 300"
        )
    await runner.run(
        "tmux", "-L", socket, "set-window-option", "-t", console_target(),
        "main-pane-width", "60%",
    )
    await runner.run(
        "tmux", "-L", socket, "select-layout", "-t", console_target(), "main-vertical"
    )
    try:
        yield gateway, runner, socket
    finally:
        await runner.run("tmux", "-L", socket, "kill-server")


async def _widths(runner, socket) -> list[int]:
    output = await runner.run(
        "tmux", "-L", socket, "list-panes", "-t", console_target(), "-F", "#{pane_width}"
    )
    return [int(line) for line in output.split()]


async def _zoomed(runner, socket) -> str:
    output = await runner.run(
        "tmux", "-L", socket, "display-message", "-p", "-t", console_target(),
        "#{window_zoomed_flag}",
    )
    return output.strip()


async def test_the_gateway_reads_a_real_console_window_and_moves_its_split(console) -> None:
    """The four verbs against tmux: geometry, resize, zoom, option -- each answered for real."""
    gateway, runner, socket = console

    geometry = await gateway.console_pane_geometry()
    panes = [entry for entry in geometry if entry[0]]
    window = next(width for pane_id, width in geometry if not pane_id)
    assert len(panes) == 4, geometry
    assert window == WINDOW_WIDTH

    left = max(panes, key=lambda entry: entry[1])
    assert left[1] == 109, f"60% of 183 is 109 columns to tmux, not {left[1]}"

    # Window minus two: the right column keeps one column and the divider takes one. Asking
    # for 182 is silently clamped to 181, which is how the composer's first version came to
    # record a width it never reached.
    await gateway.resize_console_pane(left[0], WINDOW_WIDTH - 2)
    assert max(await _widths(runner, socket)) == WINDOW_WIDTH - 2

    assert await _zoomed(runner, socket) == "0"
    await gateway.zoom_console_pane(left[0], wanted=True)
    assert await _zoomed(runner, socket) == "1"

    # Idempotent: the verb reads the flag first, so asking again does not toggle it back.
    await gateway.zoom_console_pane(left[0], wanted=True)
    assert await _zoomed(runner, socket) == "1", (
        "the second call unzoomed; the reassert after every exchange would do this"
    )

    await gateway.zoom_console_pane(left[0], wanted=False)
    assert await _zoomed(runner, socket) == "0"


async def test_a_swap_unzooms_the_window_which_is_why_the_option_exists(console) -> None:
    """The measurement the whole design rests on, asserted rather than remembered.

    If a future tmux stopped unzooming on `swap-pane -d`, the option and the reassert would be
    belt and braces rather than the mechanism -- and this test is where that would be noticed.
    """
    gateway, runner, socket = console

    geometry = await gateway.console_pane_geometry()
    panes = [entry for entry in geometry if entry[0]]
    left = max(panes, key=lambda entry: entry[1])
    other = min(panes, key=lambda entry: entry[1])

    await gateway.zoom_console_pane(left[0], wanted=True)
    assert await _zoomed(runner, socket) == "1"

    await runner.run("tmux", "-L", socket, "swap-pane", "-d", "-s", left[0], "-t", other[0])

    assert await _zoomed(runner, socket) == "0", (
        "tmux no longer unzooms on swap-pane -d; the hidden-state option may now be redundant"
    )
    # And ours survived it, which is the half that makes the reassert possible.
    assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == ""
    await gateway.write_console_option(PANES_HIDDEN_OPTION, "1")
    await runner.run("tmux", "-L", socket, "swap-pane", "-d", "-s", left[0], "-t", other[0])
    assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == "1", (
        "the option did not survive an exchange, so nothing could re-apply the fold"
    )
