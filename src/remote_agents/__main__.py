"""Package command-line entry point with no application policy.

The one branch here is not policy but cost. `agent-event` is the command installed into the
operator's global agent settings, so it runs in every Claude session on the machine; routing
it before `bootstrap` is imported is what keeps a session this service did not start from
paying for the whole composition root to be told it has nothing to do. See
`remote_agents.agent_event`.
"""

import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["agent-event"]:
        from remote_agents.agent_event import run_agent_event

        raise SystemExit(run_agent_event(sys.argv[2:]))

    from remote_agents.bootstrap import main

    raise SystemExit(main())
