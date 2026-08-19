"""Hand the owner's terminal to the pane the local surface just started.

**From a bare shell the handoff is still an exec** — the surface process becomes the
attached client, exactly the shape DEC-023 originally pinned. What that decision recorded
as an open question — the database handle a longer-lived surface would hold — is answered
by the per-operation connection lease (see `bootstrap.py`'s tui branch and the README's
reworded guarantee), and with it the *refusal* half of the old contract narrows: a client
already on **our own** tmux server is not nesting when it reaches a session, it is
switching, so that path issues `switch-client` instead of printing a scolding. A client on
somebody else's tmux is still refused and handed the command, because nesting a client
inside a foreign client remains the thing this module exists to prevent, and an exec that
cannot happen still leaves the owner a command instead of a lost session.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from enum import Enum

from remote_agents.adapters.tui.app import AttachRequest
from remote_agents.domain.models import SessionId


class HostingMode(Enum):
    """Where this surface's controlling terminal actually lives."""

    BARE = "bare"
    CONSOLE = "console"
    FOREIGN = "foreign"


def hosting_mode(environment: Mapping[str, str]) -> HostingMode:
    """Classify the hosting from `$TMUX`, by socket rather than by mere presence.

    tmux sets `TMUX` to `socket_path,server_pid,session_id`; the socket's basename is the
    server's `-L` name, and only our own name means switching is possible. Anything set but
    unreadable is classified as foreign, because the safe answer to "whose client is this?"
    when the evidence is garbled is "not ours".
    """
    value = environment.get("TMUX")
    if not value:
        return HostingMode.BARE
    socket_path = value.split(",", 1)[0]
    if os.path.basename(socket_path) == "remote-agents" and os.path.sep in socket_path:
        return HostingMode.CONSOLE
    return HostingMode.FOREIGN


def attach_to(
    request: AttachRequest | None,
    *,
    switch_argv: Callable[[SessionId], tuple[str, ...]],
    environment: Mapping[str, str] | None = None,
    exec_argv: Callable[[str, tuple[str, ...]], None] = os.execvp,
    report: Callable[[str], None] = print,
) -> int:
    """Reach the session by the route the hosting allows, or say how to reach it.

    Bare shell: exec the attach command (the process becomes the client). Our own server:
    exec the codec-built `switch-client` — the client moves, nothing nests. Foreign tmux:
    refused, command printed, session never lost. An exec that cannot happen prints the
    same command and exits non-zero.

    `switch_argv` is **injected rather than imported**, and required rather than defaulted.
    This module used to import the tmux codec directly, which is a driving adapter reaching
    across into another adapter family — forbidden by ARCH-02, and undetected because
    nothing ran the checker against `src/`. The composition root is the one place allowed to
    know both sides, so it supplies the builder; the argv is still codec-built, which is what
    DEC-001 actually requires. No default, because a default would have to be that same
    import wearing a keyword.
    """
    if request is None:
        return 0
    values = os.environ if environment is None else environment
    mode = hosting_mode(values)
    # The CONSOLE branch below is defense in depth, not a production route: under console
    # hosting the composition wires `open_in_console` unconditionally (bootstrap
    # `local_context`), so the app opens by switching and never exits with a request — an
    # AttachRequest can only arrive here from the other two hostings, or if that wiring
    # invariant is ever broken. If it fires, the exec'd one-shot switch still moves the
    # client; what it costs is this process, which is why the invariant lives where the
    # capability is wired rather than being re-checked here.
    if mode is HostingMode.FOREIGN:
        report(
            "Already inside another tmux. Detach first, or reach the session with:\n"
            f"{request.command}"
        )
        return 0
    argv = (
        switch_argv(SessionId.parse(request.session_id))
        if mode is HostingMode.CONSOLE
        else request.argv
    )
    try:
        exec_argv(argv[0], argv)
    except OSError:
        report(f"Could not attach automatically. Attach with:\n{request.command}")
        return 1
    return 0
