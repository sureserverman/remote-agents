"""Hand the owner's terminal to the pane the local surface just started.

**The handoff is an exec, not a suspend** (DEC-023). Textual's `App.suspend()` was the
alternative: it would have kept the surface process alive underneath the attached tmux client
and dropped the owner back on the session list when they detached. It is not adopted, because
a suspended surface is still a *running* surface — it holds the SQLite handle open for as long
as the owner stays attached. That is the whole objection. README.md:173-175 promises in writing
that "the attached terminal holds no database handle", and the concurrency story DEC-005
accepted rests on the same fact: the bot and this terminal are two writers, and the terminal
letting go of its handle while the owner is attached is what keeps that pair simple. So the
trade is a UX nicety against a documented guarantee. Declining costs the nicety — detaching
returns to the shell rather than to the session list, so re-entering the surface is a fresh
launch — and the guarantee is load-bearing in a way the nicety is not. DEC-005 stands
unamended; DEC-023 declines to override it and deliberately records no supersede.
"""

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
