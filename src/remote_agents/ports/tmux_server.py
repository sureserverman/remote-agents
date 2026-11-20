"""Which tmux server this project owns, by name.

`TmuxGateway` refuses to talk to a server that is not ours, and it has always accepted two:
the production socket, and a `remote-agents-test-` name so a live test can have a disposable
one. That is this module's whole subject.

**What it deliberately does not decide is whether a *surface* is hosted by the console.**
`hosting_mode` asks a different question with the same words, and answering it from here was a
real defect rather than a tidy-up. A surface that calls itself console-hosted goes on to build
a `ConsoleComposer`, and the composition root hardcodes that composer's server to the
production socket — so a surface running inside a *disposable* console classified itself as
CONSOLE and then drove the owner's real one: splitting panes into their live console window,
installing a root binding on their server, and running the start-time repair against it. That
happened, on the machine this was written on, and it was found by the final gate's evaluator
reading the artifact rather than by any test.

The rule that follows: **`hosting_mode` stays strict** — CONSOLE means the production socket
and nothing else — until the composer's server stops being hardcoded. Two questions, two
answers, and the narrower one is the one attached to a live tmux server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The dedicated server this project talks to in production.
SOCKET_NAME = "remote-agents"

#: The prefix every disposable server a live test creates must carry.
TEST_SOCKET_PREFIX = "remote-agents-test-"


def is_our_socket(socket_name: str) -> bool:
    """Whether a tmux server name is one this project's **gateway** may address."""
    return socket_name == SOCKET_NAME or socket_name.startswith(TEST_SOCKET_PREFIX)


def hosting_socket(environment: Mapping[str, str]) -> str | None:
    """The `-L` name of the tmux server this process runs inside, when that server is ours.

    tmux sets `TMUX` to `socket_path,server_pid,session_id` in every pane and every
    `run-shell` child, and the socket's basename is the server's `-L` name. Anything absent,
    garbled, or naming somebody else's server is None: a surface outside our servers builds on
    the production one, exactly as it always has.
    """
    value = environment.get("TMUX")
    if not value:
        return None
    socket_path = value.split(",", 1)[0]
    name = os.path.basename(socket_path)
    if os.path.sep in socket_path and is_our_socket(name):
        return name
    return None
