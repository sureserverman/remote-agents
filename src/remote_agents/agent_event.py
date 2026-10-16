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

`session-id` lives here too, because it asks the hook's question from the other side: a handoff
writer, run as an agent's tool command, asks which managed session it belongs to, and gets the
answer the hook drain would give for that agent (DEC-128).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from remote_agents.ports.argv_text import NonEchoingArgumentParser

if TYPE_CHECKING:
    from remote_agents.ports.provider_descriptor import HookLicence

#: `session-id`'s exit for a shell no managed session owns. The handoff writer reads it as
#: "unsupervised" and writes nothing; 2 stays argparse's usage error.
NOT_MANAGED = 3

HOOK_PROVIDERS = ("claude", "codex", "opencode", "cursor")
"""The `--provider` names `agent-event` and `install-agent-hooks` accept: one per hook installer.

Spelled here, not read off the registry, because this module must stay importable without the
adapters (see the module docstring). `bootstrap` asks this tuple for both of its parsers, and
tests hold it equal to the registry's installers and to the descriptors' `hooks` names, so none
of them can drift apart.
"""


def spool_from_stdin(activity_directory: Path | None, *, provider: str = "claude") -> int:
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

    # The one vertical this hook was installed for, imported only once the spool has decided
    # the session is ours, and inside its guard: a descriptor would cost every vertical and
    # its usage readers, and even one vertical is too much for the "not mine" path (BL-050).
    def licence() -> HookLicence | None:
        from remote_agents.adapters.agents.hook_licences import hook_licence

        return hook_licence(provider)

    return spool_agent_event(
        payload, activity_directory=resolved, licence=licence, hook_name=provider
    )


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
    parser.add_argument("--provider", choices=HOOK_PROVIDERS, default="claude")
    arguments = parser.parse_args(argv)
    return spool_from_stdin(arguments.activity_dir, provider=arguments.provider)


def resolve_session_id(
    environment: Mapping[str, str],
    cwd: str | None,
    activity_directory: Path | None,
    *,
    licence: Callable[[str], HookLicence | None],
) -> str | None:
    """The managed session the calling tool shell belongs to, by its provider's own rule.

    A shell carrying the thread variable of a provider that names its sessions from the payload
    is placed by that thread and its folder, against the roster the service publishes for the
    provider's hook -- `hook_identity.resolve`, the rule the hook drain applies. Its session
    variable is never read: the shell inherits the environment of whichever pane started the
    provider's daemon, measured naming another session (DEC-128). No roster, or none that places
    the thread, is nobody's -- where the hook, with no roster, keeps the variable: a handoff
    stamped with the wrong session is worse than none.

    Any other shell is named by its pane's variable, as its hooks are -- unless that variable
    names a session on such a provider's roster. That is a shell of the provider's daemon whose
    thread variable is missing, and the variable is the starter's, so it is nobody's.

    The folder is the shell's own, not its turn's: a writer that changes folder before asking
    is compared against the folder it moved to.
    """
    from remote_agents.adapters.agents.hook_identity import read_roster, resolve, roster_path
    from remote_agents.ports.provider_descriptor import HookIdentity
    from remote_agents.ports.session_identity import SESSION_ID_VARIABLE, safe_session_id

    inherited = safe_session_id(environment.get(SESSION_ID_VARIABLE))
    for hook_name in HOOK_PROVIDERS:
        found = licence(hook_name)
        if found is None or found.identity is not HookIdentity.PAYLOAD_THREAD:
            continue
        roster = (
            None
            if activity_directory is None
            else read_roster(roster_path(activity_directory, hook_name))
        )
        thread = None if found.thread_variable is None else environment.get(found.thread_variable)
        if thread:
            return resolve(thread, cwd, roster) if roster else None
        if roster and any(entry.session_id == inherited for entry in roster):
            return None
    return inherited


def run_session_id(argv: list[str] | None = None) -> int:
    """Print the calling shell's managed session id alone on one line; exit 3 when it has none.

    Read-only: it reads the environment, the folder and the roster file, and never the database.
    """
    from remote_agents.adapters.agents.hook_licences import hook_licence
    from remote_agents.config import ConfigError
    from remote_agents.production import ProductionPaths

    parser = NonEchoingArgumentParser(prog="remote-agents session-id")
    parser.add_argument("--activity-dir", type=Path)
    arguments = parser.parse_args(argv)
    activity_directory = arguments.activity_dir
    if activity_directory is None:
        try:
            activity_directory = ProductionPaths.for_home(Path.home()).activity_directory
        except (ConfigError, RuntimeError):
            activity_directory = None
    try:
        cwd: str | None = os.getcwd()
    except OSError:
        # A folder removed under the shell; the exact resume id can still place its thread.
        cwd = None
    session_id = resolve_session_id(os.environ, cwd, activity_directory, licence=hook_licence)
    if session_id is None:
        print("remote-agents: this shell belongs to no managed session", file=sys.stderr)
        return NOT_MANAGED
    print(session_id)
    return 0
