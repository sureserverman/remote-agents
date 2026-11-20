"""Which tmux server this project owns, by name.

`TmuxGateway` refuses to talk to a server that is not ours, and it has always accepted two:
the production socket, and a `remote-agents-test-` name so a live test can have a disposable
one. That is this module's whole subject.

**The same rule now decides console hosting** (`hosting_socket`, DEC-144). It once could not:
the composition root built every surface's `ConsoleComposer` on the production socket, so a
surface inside a *disposable* console that called itself console-hosted drove the owner's real
one — splitting panes into their live console window, installing a root binding on their
server, running the start-time repair against it (BL-041). That happened on the machine this
was written on, and was found by a final gate's evaluator reading the artifact rather than by
any test. The composer is built on the socket the surface runs inside now, so "hosted by one
of ours" and "the server this surface drives" are the same server by construction.
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
