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

Telegram configuration and service installation are introduced in later implementation stages.
Do not put secrets in this repository.

## Curated profiles and recovery

This host enables only the five fixed, version-qualified interactive profiles. Check the
current machine's non-secret compatibility record with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The command returns `QUALIFIED` only when the installed version matches a recorded live
launch/readiness/graceful-exit/cleanup qualification. A missing executable, failed probe, or
version change is reported as `BLOCKED`; one blocked profile does not affect the others.

See [the compatibility matrix and dedicated-socket recovery commands](docs/profile-compatibility.md).
