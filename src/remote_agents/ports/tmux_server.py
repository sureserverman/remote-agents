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

#: The dedicated server this project talks to in production.
SOCKET_NAME = "remote-agents"

#: The prefix every disposable server a live test creates must carry.
TEST_SOCKET_PREFIX = "remote-agents-test-"


def is_our_socket(socket_name: str) -> bool:
    """Whether a tmux server name is one this project's **gateway** may address."""
    return socket_name == SOCKET_NAME or socket_name.startswith(TEST_SOCKET_PREFIX)
