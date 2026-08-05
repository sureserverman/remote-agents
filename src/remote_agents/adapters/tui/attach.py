"""Hand the owner's terminal to the pane the local surface just started."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from remote_agents.adapters.tui.app import AttachRequest


def attach_to(
    request: AttachRequest | None,
    *,
    environment: Mapping[str, str] | None = None,
    exec_argv: Callable[[str, tuple[str, ...]], None] = os.execvp,
    report: Callable[[str], None] = print,
) -> int:
    """Replace this process with the attach command, or say how to reach the session.

    Nesting a tmux client inside a tmux client is refused rather than attempted, and an
    exec that cannot happen leaves the owner a command instead of a lost session.
    """
    if request is None:
        return 0
    values = os.environ if environment is None else environment
    if values.get("TMUX"):
        report(
            "Already inside tmux. Detach first, or switch to the new session with:\n"
            f"{request.command}"
        )
        return 0
    try:
        exec_argv(request.argv[0], request.argv)
    except OSError:
        report(f"Could not attach automatically. Attach with:\n{request.command}")
        return 1
    return 0
