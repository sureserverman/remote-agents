# Remote Agents

Remote Agents will be a private, single-owner Telegram control plane for curated agent
sessions running in isolated tmux servers on this host.

Its approved scope is limited to project browsing and managed session lifecycle actions:
launch, selected resume, local-session safe handoff, list, inspect, Copy Attach, graceful stop,
cleanup, confirmed force stop, and confirmed Claude Remote Control state changes. It does not
provide remote shell access, prompt relay, raw agent arguments, arbitrary keystrokes, or registry
mutation.

## Development

```bash
uv sync --locked
uv run --locked pytest -q
```

## Production operation

Keep both runtime files outside the repository and owner-readable only:

```bash
install -d -m 700 ~/.config/remote-agents ~/.local/state/remote-agents
install -m 600 config/remote-agents.example.toml ~/.config/remote-agents/config.toml
${EDITOR:-vi} ~/.config/remote-agents/telegram.env
```

`telegram.env` contains these three values and nothing else:

```text
REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=replace-me
REMOTE_AGENTS_OWNER_USER_ID=replace-me
REMOTE_AGENTS_OWNER_CHAT_ID=replace-me
```

Install and start the user service:

```bash
install -m 600 systemd/remote-agents.service ~/.config/systemd/user/remote-agents.service
systemctl --user daemon-reload
systemctl --user enable --now remote-agents.service
systemctl --user is-active remote-agents.service
uv run --locked remote-agents doctor --json | python -m json.tool
```

The configured owner sees only `/start`, `/launch`, `/sessions`, and `/help` in Telegram's
command menu. `/start` opens a compact Home dashboard with active and preserved counts;
`/launch` opens the paginated project list and `/sessions` opens current managed sessions.
Search and optional-label entry use Telegram reply prompts: send `Skip`, `Cancel`, or `Back`
instead of leaving an input step stranded. Review shows the project, agent, and label before
creating a session. Back restores the preceding choice and Cancel returns Home without a
mutation. Ended records remain in local SQLite history but do not clutter the Telegram list.
After a service restart or an expired button, Telegram replaces the old view with a fresh Home.

Inspect shows safely escaped terminal text inline when it fits. Oversized output is sent as a
UTF-8 `session-output.txt` attachment; it is read-only captured output, never an input channel.

For safe stop behavior, choose Graceful, inspect preserved output, then Cleanup. Force stop
requires a second confirmation and is for a live session that cannot exit gracefully. The bot
never relays arbitrary commands, agent text, shell access, or approval responses.

Resume uses a server-resolved catalogue selection. It may show a bounded provider-generated title
or provider resume description (Claude's stored last prompt and Codex's thread preview when no
title is available); provider IDs and transcript output remain server-side. Local Sessions never
moves an external process: exit it locally, then confirm a new managed resume after liveness is
rechecked. A verified-source Local Session may instead offer the fixed **Terminate and Resume**
handoff: it warns that the current unsaved turn may be lost, requires a second confirmation, sends
only `SIGTERM` after exact same-UID identity validation, waits for exit, and resumes through managed
tmux. An uncorrelated eligible row requires an explicit same-profile saved-conversation selection;
Cursor and incomplete identity evidence remain read-only. There is no `SIGKILL`, process-group
signal, terminate-only action, key injection, or raw PID/signal input.
Copy Attach is offered only for a currently trusted live managed pane. Claude Remote Control is
available only on a live managed Claude pane, requires a second confirmation, and uses the single
qualified enable/disable interaction; it never carries a prompt, transcript, or session URL.

See [the operator runbook](docs/operator-runbook.md) for acceptance, recovery, and rollback.
Do not put secrets in this repository.

## Curated profiles and recovery

This host enables only five fixed interactive profiles. Their installed versions are
operator-managed and reported for diagnosis, not used as a launch gate. Check the current
machine's non-secret profile record with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The command reports `AVAILABLE` when the executable is present. A version probe is informative
only: local updates remain launchable and each launch must still reach its profile's readiness
state. A missing executable is `BLOCKED`; one blocked profile does not affect the others.

The full doctor command reads only the private credential-file boundary, not its values. It
reports the core registry, SQLite store, fixed tmux command, active user service, Telegram
credential-file boundary, and every profile together; `healthy` is true only when all are ready.

See [the compatibility matrix and dedicated-socket recovery commands](docs/profile-compatibility.md).
