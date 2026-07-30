"""Package command-line entry point with no application policy."""

from remote_agents.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
