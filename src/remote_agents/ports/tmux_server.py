"""Which tmux server this project owns, by name — the one fact two adapters must share.

A three-line module, and the reason it lives in `ports/` rather than in the tmux codec is a
rule the codec itself cannot satisfy. `hosting_mode` (the terminal adapter) classifies the
server a surface is running inside; `TmuxGateway` (the tmux adapter) refuses to talk to a
server that is not ours. Same question, two adapter families — and ARCH-02 lets an adapter
import `domain` and `ports` and nothing else, so this is the only shelf both can reach.

They disagreed until a live journey test tripped over it. The gateway has always accepted the
production socket **and** a `remote-agents-test-` name, which is how every live file gets a
disposable server; `hosting_mode` accepted only the production name. So a surface running
inside a *test* console classified itself as hosted by a foreign tmux, wired no console
capability, and exec-attached instead of exchanging panes — which meant no live test could
exercise the console-hosted surface at all, and the first one that tried is what found it.
"""

from __future__ import annotations

#: The dedicated server this project talks to in production.
SOCKET_NAME = "remote-agents"

#: The prefix every disposable server a live test creates must carry.
TEST_SOCKET_PREFIX = "remote-agents-test-"


def is_our_socket(socket_name: str) -> bool:
    """Whether a tmux server name is one this project owns."""
    return socket_name == SOCKET_NAME or socket_name.startswith(TEST_SOCKET_PREFIX)
