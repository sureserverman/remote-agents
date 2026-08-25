"""The hook's entry point, deliberately reachable without the composition root.

`bootstrap` is where every adapter is wired together, so importing it costs the Telegram
service, httpx, sqlite3, rich and asyncio. That is the right price for starting the service
and the wrong one for this: the command installed into the operator's global settings file
fires on `Stop`, `StopFailure`, `Notification` and `SessionEnd` in *every* Claude session on
the machine, including every session this service did not start and will do nothing about.
Measured before the split, that was 678 modules and about a quarter of a second per event,
in front of an environment check whose whole purpose is to answer "not mine" immediately.

So the module lives beside the composition root rather than inside it, and `__main__` routes
to it before `bootstrap` is imported at all. `bootstrap` still owns the subcommand for a
caller that already has it loaded -- `main(["agent-event"])` keeps working -- and delegates
here, so there is one implementation and not two that must agree.
"""

from __future__ import annotations

import sys
from pathlib import Path

from remote_agents.ports.argv_text import NonEchoingArgumentParser


def spool_from_stdin(activity_directory: Path | None) -> int:
    """Read one hook payload from stdin and record it, reporting success whatever happens.

    Every import is deferred into the call for the reason the module docstring gives: a hook
    that decides it has nothing to do should pay for as little as possible on the way to that
    conclusion.
    """
    from remote_agents.adapters.agents.activity_spool import spool_agent_event
    from remote_agents.config import ConfigError
    from remote_agents.production import ProductionPaths

    try:
        resolved = activity_directory or ProductionPaths.for_home(Path.home()).activity_directory
    except (ConfigError, RuntimeError):
        return 0
    try:
        payload = sys.stdin.buffer
    except (AttributeError, ValueError):
        # `sys.stdin` is None when the process was started with no stdin at all, and a closed
        # one raises. Both were outside every guard here, so the attribute access itself
        # raised into the agent's session -- the one outcome this whole path is arranged to
        # prevent, arriving before the function that promises never to raise was even called.
        return 0
    return spool_agent_event(payload, activity_directory=resolved)


def run_agent_event(argv: list[str] | None = None) -> int:
    """Parse this subcommand's own arguments, so reaching it needs no other parser."""
    # `NonEchoingArgumentParser`, like every other parser in this project. This one is the
    # reason the architecture test exists: `__main__` routes `agent-event` here *without*
    # importing `bootstrap` -- deliberately, because the hook fires in every Claude session and
    # the composition root costs 678 modules -- so the defence `bootstrap` had did not reach the
    # shipped entry point, and `remote-agents agent-event --bot-token=<token>` printed the
    # credential through the exact message the leak was first filed for. The test driving it
    # drove `bootstrap.main`, which is not the path the console script takes.
    parser = NonEchoingArgumentParser(prog="remote-agents agent-event")
    parser.add_argument("--activity-dir", type=Path)
    arguments = parser.parse_args(argv)
    return spool_from_stdin(arguments.activity_dir)
