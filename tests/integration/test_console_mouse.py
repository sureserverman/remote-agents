"""The console turns its own server's mouse on, against a real tmux server (BL-098).

**Why this has to be an integration test and not a codec assertion.** The codec test next
door proves the argv is right. It cannot prove tmux accepts it, that `-g` reaches the option
a later `show-options -g` reads back, or -- the part that actually matters -- that the write
lands on **this project's socket and no other**. Those are claims about tmux, so a real
server answers them.

The defect: with tmux's default `mouse off`, tmux enables terminal mouse reporting only on
behalf of the **active** pane's application. The console's resting state is an agent
displayed in the left slot, and an agent that does not ask for mouse (codex reports
`mouse_any_flag=0`) means tmux never asks the terminal to report mouse at all -- so clicking
a surface pane does nothing, and not because the click went somewhere wrong. Measured on the
owner's host 2026-09-17: all three surface panes report `mouse_any_flag=1` and the active
agent pane `0`.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner


def _socket() -> str:
    """Unique per run: a socket named for the process collides with a server a prior run left."""
    return f"remote-agents-test-{uuid4().hex}"


async def _kill(socket: str) -> None:
    """Kill the server and unlink its socket file, which tmux does not do for us.

    Both halves matter. Skipping the unlink is how `/tmp/tmux-<uid>/` accumulated 108 dead
    `remote-agents-fkey-*` sockets from the live suite, cleared by hand on 2026-09-17.
    """
    try:
        await AsyncTmuxRunner().run("tmux", "-L", socket, "kill-server")
    except RuntimeError:
        pass
    (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)


async def test_the_console_server_reads_mouse_on_and_an_untouched_server_does_not(
    tmp_path: Path,
) -> None:
    """Two servers, one built by us and one not: the difference is the whole claim.

    **Both servers are started with `-f /dev/null`, and that is what makes the claim
    unconditional.** tmux reads its configuration once, at *server* start, so a throwaway
    session opened with `-f /dev/null` fixes the whole server to tmux's compiled defaults --
    and `create_console`'s own `new-session` then joins that existing server rather than
    starting a configured one. Nothing in `~/.tmux.conf`, or in any file tmux would otherwise
    read, can reach either side of the comparison.

    *This replaced a weaker version worth recording, because its docstring claimed more than
    it did.* It said the control ruled out the owner's `~/.tmux.conf` supplying `mouse on`.
    It could not: that file's guard is `#{m:*/remote-agents,#{socket_path}}`, a **suffix**
    match, and these sockets are named `remote-agents-test-<uuid>`, so the guard matched
    neither server. The test was therefore never exposed to the confound it named -- and
    would have gone red for an unrelated reason the day somebody loosened that guard to a
    substring, coupling the suite to a file outside the repo. `-f /dev/null` makes the
    control mean what the docstring says: with no configuration anywhere, `on` on our socket
    and `off` on the other is this write and nothing else.
    """
    ours, theirs = _socket(), _socket()
    runner = AsyncTmuxRunner()
    try:
        # The server, fixed to compiled defaults before the gateway touches it. `create_console`
        # issues a plain `new-session`, which joins this server rather than starting one.
        await runner.run(
            "tmux", "-L", ours, "-f", "/dev/null", "new-session", "-d", "-s", "pin", "sleep", "30"
        )
        gateway = TmuxGateway(ours, runner, intent_directory=tmp_path / "intents")
        await gateway.create_console(("sh", "-c", "sleep 30"), tmp_path)
        await gateway.write_console_server_option("mouse", "on")

        assert (
            await runner.run("tmux", "-L", ours, "show-options", "-g", "-v", "mouse")
        ).strip() == "on", "the console's own server did not take the option"

        await runner.run(
            "tmux", "-L", theirs, "-f", "/dev/null", "new-session", "-d", "-s", "t", "sleep", "30"
        )
        assert (
            await runner.run("tmux", "-L", theirs, "show-options", "-g", "-v", "mouse")
        ).strip() == "off", (
            "a server this project never built, started with no configuration at all, came "
            "up with mouse on -- so the write is not scoped to our socket"
        )
    finally:
        await _kill(ours)
        await _kill(theirs)
