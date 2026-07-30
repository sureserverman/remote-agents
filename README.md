# Remote Agents

Remote Agents will be a private, single-owner Telegram control plane for curated agent
sessions running in isolated tmux servers on this host.

Its approved scope is limited to project browsing and managed session lifecycle actions:
launch, list, inspect, graceful stop, cleanup, and confirmed force stop. It does not
provide remote shell access, prompt relay, raw agent arguments, or registry mutation.

## Development

```bash
uv sync --locked
uv run --locked pytest -q
```

Configuration, service installation, Telegram credentials, and real tmux execution are
introduced in later implementation stages. Do not put secrets in this repository.
