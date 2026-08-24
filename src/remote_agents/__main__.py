"""Package command-line entry point with no application policy.

The one branch here is not policy but cost. `agent-event` is the command installed into the
operator's global agent settings, so it runs in every Claude session on the machine; routing
it before `bootstrap` is imported is what keeps a session this service did not start from
paying for the whole composition root to be told it has nothing to do. See
`remote_agents.agent_event`.

`main` is defined at module scope, and has to be: `[project.scripts]` resolves
`remote_agents.__main__:main`, so the generated `remote-agents` shim imports this module and
reads that attribute. Deferring the imports *inside* it is what makes the branch cheap;
deferring the definition itself broke the console script, and with it the generated unit and
every documented command, while `python -m remote_agents` kept working because it takes the
`__name__` branch below.
"""

import sys


def main(argv: list[str] | None = None) -> int:
    """Route to the hook's own entry point, or to the composition root for everything else."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["agent-event"]:
        from remote_agents.agent_event import run_agent_event

        return run_agent_event(arguments[1:])

    from remote_agents.bootstrap import main as run_service

    return run_service(argv)


if __name__ == "__main__":
    raise SystemExit(main())
