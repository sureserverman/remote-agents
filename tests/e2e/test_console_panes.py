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

import asyncio
import uuid
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.codec import console_target
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.application.console import ConsoleComposer, console_panes_binding
from remote_agents.ports.console import PANES_HIDDEN_OPTION, ConsolePaneSlot

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


# --- Task 2.2: ten toggles at a real client, on the four surfaces the console really runs ----

_REGISTRY = """version: 1
projects:
  - path: {project}
    name: qualification
    area: infra
    enabled: true
    added: 2026-09-08
"""

#: What a pane must have drawn before the toggles start, and must still draw after them.
_SESSIONS_PANE_TEXT = "No managed sessions"


def _fabricated_home(root: Path) -> Path:
    """A complete production HOME under tmp_path, so this reads nothing of the owner's."""
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


def _venv_python() -> str:
    """The venv's interpreter, **not** `uv run`.

    Four surfaces start at once, and four concurrent `uv run` invocations contend on uv's own
    lock: one loses, exits, tmux closes its pane, and the console comes up three panes. That
    was diagnosed twice in the live suite before it was written down; this is the same fix.
    """
    return str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3")


def _fold_script(root: Path) -> Path:
    """What `prefix h` runs here — the production composer, aimed at *this* socket.

    It cannot be `remote_agents console panes`: that verb builds its composer from
    `_console_composer()`, which is hard-wired to the `remote-agents` socket where the owner's
    live agents and live console are. A test that pressed the real key would fold the owner's
    real console. So the key runs the same `toggle_panes()` through the same gateway, with the
    socket the fixture made — the binding, the argv and the composer are production's; only
    the server is disposable.
    """
    script = root / "fold.py"
    script.write_text(
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from remote_agents.adapters.tmux.gateway import TmuxGateway\n"
        "from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner\n"
        "from remote_agents.application.console import ConsoleComposer\n"
        "composer = ConsoleComposer(\n"
        "    TmuxGateway(sys.argv[1], AsyncTmuxRunner()),\n"
        '    ("true",),\n'
        "    Path(sys.argv[2]),\n"
        '    projects_command=("true",),\n'
        ")\n"
        "asyncio.run(composer.toggle_panes())\n",
        encoding="utf-8",
    )
    return script


async def _run(*argv: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{argv} failed: {err.decode()}")
    return out.decode()


async def _cpu_seconds(pid: str) -> float:
    """Cumulative CPU time for one process, in seconds, asked the way both platforms answer.

    `ps -o cputime=` and not `/proc/<pid>/stat`: this suite runs on Ubuntu and on macOS, and a
    `/proc` reader asks Linux a real question and macOS nothing at all — the shape
    `tests/support/process_state.py` was written to stop repeating. Nor `%cpu`, which is an
    average over the process's *whole life*: a pane that pegged a core for ten seconds during
    the toggles and then went quiet reads as a small number once it has been alive a minute,
    which is precisely the reading this test must not accept.
    """
    raw = (await _run("ps", "-o", "cputime=", "-p", pid)).strip()
    if not raw:
        raise RuntimeError(f"pid {pid} is gone; a console pane died during the toggles")
    parts = [float(piece) for piece in raw.replace("-", ":").split(":")]
    seconds = 0.0
    for piece in parts:
        seconds = seconds * 60.0 + piece
    return seconds


async def _capture(socket: str, pane: str) -> str:
    return await _run("tmux", "-L", socket, "capture-pane", "-p", "-t", pane)


@pytest.fixture
async def console_with_surfaces(tmp_path):
    """The console as the owner really runs it: four Textual processes, and a real client.

    Two sockets, for the reason `tests/live/test_three_pane_console.py` states: a key binding
    is only meaningful to an attached *client*, so a second disposable server provides one — a
    pane running `tmux -L <console> attach-session`. Headless calls would prove the composer,
    which the unit tests already do; they would not prove the key.
    """
    home = _fabricated_home(tmp_path)
    console_socket = f"remote-agents-test-panes-{uuid.uuid4().hex[:8]}"
    host_socket = f"remote-agents-test-host-{uuid.uuid4().hex[:8]}"
    gateway = TmuxGateway(console_socket, AsyncTmuxRunner())
    composer = ConsoleComposer(
        gateway,
        ("sleep", "600"),
        home,
        projects_command=("true",),
        panes_command=(_venv_python(), str(_fold_script(tmp_path)), console_socket, str(home)),
        bindings=(console_panes_binding(),),
        pane_commands={
            slot: (
                "env",
                f"HOME={home}",
                _venv_python(),
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
        await _run(
            "tmux", "-L", host_socket, "new-session", "-d", "-s", "host",
            "-x", str(WINDOW_WIDTH), "-y", str(WINDOW_HEIGHT),
            "tmux", "-L", console_socket, "attach-session", "-t", console_target(),
        )
        yield gateway, console_socket, host_socket
    finally:
        for socket in (host_socket, console_socket):
            try:
                await _run("tmux", "-L", socket, "kill-server")
            except RuntimeError:
                pass


async def test_ten_toggles_at_a_real_client_leave_every_pane_idle(console_with_surfaces) -> None:
    """BL-039's hazard, measured rather than assumed — on the four surfaces, not on `sleep`.

    A console rebuild once locked the sessions pane into Textual's resize loop at 100 % CPU,
    and a fold is eight resizes in a quarter of a second. Ten of them is what the owner will
    do while making up their mind, so ten is what this presses — at a real attached client,
    through the real prefix binding, into the real composer.

    Two assertions and they are not the same one twice: **idle** (no pane accrues CPU time
    once the toggles stop) and **alive** (the sessions pane still draws its own text). A pane
    wedged in a redraw loop fails the first; a pane that crashed, or froze on the frame it
    had, fails the second while passing the first perfectly.
    """
    gateway, console_socket, host_socket = console_with_surfaces

    arrangement = await gateway.pane_arrangement()
    by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
    assert len(by_slot) == 4, arrangement
    sessions = by_slot[ConsolePaneSlot.SESSIONS.value]

    # Textual apps starting four interpreters: polled rather than slept, so a fast host is
    # not made to wait and a slow one is not failed for being slow.
    for _ in range(40):
        if _SESSIONS_PANE_TEXT in await _capture(console_socket, sessions.pane_id):
            break
        await asyncio.sleep(1.0)
    else:
        raise AssertionError(
            f"the sessions pane never drew {_SESSIONS_PANE_TEXT!r}: "
            f"{await _capture(console_socket, sessions.pane_id)!r}"
        )

    async def press() -> None:
        # Two send-keys, never one batch: a TUI mid-redraw drops the tail of a batched send.
        await _run("tmux", "-L", host_socket, "send-keys", "-t", "host:", "C-b")
        await _run("tmux", "-L", host_socket, "send-keys", "-t", "host:", "h")

    await press()
    await asyncio.sleep(1.0)
    assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == "1", (
        "the first press did not fold the column, so the binding never reached the composer"
    )
    assert await _zoomed(AsyncTmuxRunner(), console_socket) == "1"

    for _ in range(9):
        await press()
        await asyncio.sleep(1.0)

    assert await gateway.read_console_option(PANES_HIDDEN_OPTION) == "", (
        "ten toggles left the column folded; an even number must return it"
    )
    assert await _zoomed(AsyncTmuxRunner(), console_socket) == "0"

    pids = (
        await _run(
            "tmux", "-L", console_socket, "list-panes", "-t", console_target(),
            "-F", "#{pane_id}|#{pane_pid}",
        )
    ).split()
    before = {entry: await _cpu_seconds(entry.split("|")[1]) for entry in pids}
    await asyncio.sleep(6.0)
    after = {entry: await _cpu_seconds(entry.split("|")[1]) for entry in pids}
    burned = {entry: after[entry] - before[entry] for entry in pids}

    # **A second of CPU over six, per pane, and the threshold is the instrument's floor
    # rather than a judgement.** `ps` reports cumulative CPU time to the second on both
    # platforms, so a percentage under about 17 % cannot be resolved over a window this size;
    # asserting "under 5 %" against a tool with one-second granularity would be asserting
    # nothing. What it must catch it catches with room to spare: BL-039's signature is a
    # pegged core, which is six seconds here, and even a third of a core is two.
    assert all(seconds <= 1.0 for seconds in burned.values()), (
        f"a console pane is still working six seconds after the last toggle: {burned}"
    )
    assert _SESSIONS_PANE_TEXT in await _capture(console_socket, sessions.pane_id), (
        "the sessions pane stopped drawing its own text; it is idle because it is stuck"
    )
