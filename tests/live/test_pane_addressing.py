"""Opt-in proof, on real tmux, that the whole action set follows a displaced pane.

Stage 2's goal end to end. Each operation here is one that reads or writes a pane, and
each would have failed *silently* under session addressing once the agent's pane is hosted
somewhere else — a capture returning the wrong screen, a keystroke landing in the wrong
terminal, a kill reporting success over a live agent. Silence is the reason this test
exists rather than a unit test alone: nothing raises, so nothing else would notice.

The pane is displaced by a real `swap-pane` into the console, which is the exchange the
console will perform. What is asserted is not that tmux swapped, but that readiness,
capture, trust classification, a graceful stop's key sequence and a force stop each still
act on the agent afterwards.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_MARKER = "PANE-ADDRESSING-LIVE"
_PROFILE = ProfileId("probe")
_PROJECT = ProjectId("qualification")


def probe_profile() -> LaunchProfile:
    """A stand-in agent: prints a readiness banner, then waits to be stopped.

    No curated profile is needed to prove addressing, and using one would make the test
    depend on an agent binary being installed. `C-c` is a real graceful sequence here —
    tmux gives the pane a pty, so the key reaches the foreground process group.
    """
    shell = "/bin/sh"
    return LaunchProfile(
        executable=shell,
        argv=(shell, "-c", f"echo {_MARKER}; sleep 300"),
        environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        readiness_marker=_MARKER,
        graceful_keys=("C-c",),
    )


async def process_gone(pid: str, *, within: float = 10.0) -> bool:
    """Whether a pid has stopped running, polled — and a zombie counts as stopped.

    The pane reporting `pane_dead` is tmux noticing the exit; the process leaving the table
    is its parent reaping it, and those are not the same instant. A bare `/proc/<pid>` check
    raced the second one. A zombie is an exited process by every meaning this test cares
    about, so it is read from the status rather than from the directory's existence.
    """
    deadline = asyncio.get_running_loop().time() + within
    status = Path(f"/proc/{pid}/stat")
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = status.read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state == "Z":
            return True
        await asyncio.sleep(0.1)
    return False


async def displace(runner: AsyncTmuxRunner, base: tuple[str, ...], session_id: SessionId) -> str:
    """Swap one managed pane into the console's left slot and return the pane that moved.

    The slot is re-read on every call, and that is not incidental. A pane id is a handle to
    a *pane*, not to a position: after one exchange the pane that used to sit in the left
    slot is living in some agent's home window, and swapping against its id would put the
    next agent there instead of into the console. Written once with the slot captured at
    setup, this test did exactly that. It is the same mistake Sub-plan 2 forbids the
    composer — "who is in the left pane" is derived at every read, never remembered.
    """
    agent = (
        await runner.run(*base, "list-panes", "-t", f"ra-{session_id}:", "-F", "#{pane_id}")
    ).strip()
    slot = (
        (await runner.run(*base, "list-panes", "-t", "ra-console:", "-F", "#{pane_id}"))
        .splitlines()[0]
        .strip()
    )
    await runner.run(*base, "swap-pane", "-s", agent, "-t", slot)
    hosting = await runner.run(*base, "display-message", "-p", "-t", agent, "#{session_name}")
    assert hosting.strip() == "ra-console", "the pane did not actually move"
    return agent


async def test_every_pane_following_operation_reaches_a_displaced_agent(tmp_path: Path) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")

    socket = f"remote-agents-test-{SessionId.new().value.hex}"
    runner = AsyncTmuxRunner()
    base = ("tmux", "-L", socket)
    gateway = TmuxGateway(socket, runner, intent_directory=tmp_path / "intents")
    terminal = TmuxTerminal(
        gateway, {_PROJECT: tmp_path}, {_PROFILE: probe_profile()}, startup_timeout=15
    )
    try:
        await gateway.create_console(("sleep", "600"), tmp_path)
        # Claim 7: `remain-on-exit` is a window option and does not travel with a swapped
        # pane, so a console window that is not armed loses the agent's pane outright when
        # the agent exits — and its own session with it. Arming it here is what Sub-plan 2
        # must do at console construction; without it this test would be measuring that gap
        # rather than addressing.
        await runner.run(*base, "set-option", "-t", "ra-console:", "remain-on-exit", "on")
        # The console window carries more than one pane, as the three-pane design does: the
        # left slot is what the swap exchanges, and the others are what keep the window from
        # being emptied when the slot's occupant is killed. Written the first time without
        # this, the test destroyed the console by force-stopping a displaced agent — which is
        # the "never reduce the console window to zero panes" hazard Sub-plan 2 names,
        # reproduced by accident. It belongs in the fixture because it is a property of the
        # console's shape, not of addressing.
        await runner.run(*base, "split-window", "-d", "-t", "ra-console:", "sleep", "600")

        graceful = SessionId.new()
        launched = await terminal.launch(graceful, _PROJECT, _PROFILE)
        assert launched.live, f"the stand-in agent did not start: {launched.detail}"
        agent_pane = await displace(runner, base, graceful)
        agent_pid = (
            await runner.run(*base, "display-message", "-p", "-t", agent_pane, "#{pane_pid}")
        ).strip()

        # Capture, and everything fed by it. The session target would now read the pane that
        # swapped into the agent's home window, which prints nothing at all.
        assert _MARKER in await gateway.capture(graceful)
        ready = await terminal.confirm_ready(graceful, _PROFILE)
        assert ready.live, f"readiness read the wrong pane: {ready.detail}"
        await terminal.trust_state(graceful)  # classification over the agent's own screen

        # A keystroke, which is the write half. DEC-016 puts a bare Enter on this path.
        stopped = await terminal.graceful_stop(graceful, _PROFILE)
        assert stopped.preserved, f"the stop did not reach the agent: {stopped.detail}"
        assert await process_gone(agent_pid), "the agent process outlived its stop"

        # Force stop, on a second session, from displaced. This is the one that reported
        # success over a live agent before: `kill-session` cannot reach a pane whose window
        # is hosted elsewhere.
        forced = SessionId.new()
        assert (await terminal.launch(forced, _PROJECT, _PROFILE)).live
        forced_pane = await displace(runner, base, forced)
        forced_pid = (
            await runner.run(*base, "display-message", "-p", "-t", forced_pane, "#{pane_pid}")
        ).strip()

        result = await terminal.force_stop(forced)
        assert not result.live, f"force stop refused: {result.detail}"
        assert await process_gone(forced_pid), "force stop left the agent running"

        # And the console survived every one of them (DEC-006, from the other direction).
        assert await gateway.console_exists() is True
    finally:
        try:
            await runner.run(*base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)
