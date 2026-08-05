# Remote Agents

Remote Agents will be a private, single-owner Telegram control plane for curated agent
sessions running in isolated tmux servers on this host.

Its approved scope is limited to project browsing, project creation, and managed session
lifecycle actions: launch, selected resume, list, inspect, Copy Attach, graceful stop,
cleanup, confirmed force stop, and confirmed Claude Remote Control state changes. It does not
provide remote shell access, prompt relay, raw agent arguments, arbitrary keystrokes, or
arbitrary paths. Its only approved registry mutation is the append-only add-project entry
described below; no existing registry entry is ever edited or removed.

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
title is available); provider IDs and transcript output remain server-side. The bot does not scan,
identify, terminate, or adopt arbitrary local agent processes. Only provider-catalogued
conversations can be resumed into a new managed tmux pane.
Copy Attach is offered only for a currently trusted live managed pane. Claude Remote Control is
available only on a live managed Claude pane, requires a second confirmation, and uses the single
qualified enable/disable interaction; it never carries a prompt, transcript, or session URL.

See [the operator runbook](docs/operator-runbook.md) for acceptance, recovery, and rollback.
Do not put secrets in this repository.

## Creating a project

A project can be created from this host or from Telegram. Both surfaces run the same validated
use case and the same append-only registry write:

```bash
uv run --locked remote-agents add-project --area infra --name new-thing
```

In Telegram, Add Project offers the area as a choice between the existing directories the server
enumerates under the configured development root; a free-form area is never accepted. The project
name is entered through a reply prompt and is validated before anything is created or written, and
Review names the area and the name before the mutation happens. Cancel returns Home without a
mutation.

Area and name must each be lowercase letters, digits, and single hyphens, 1 to 64 characters. The
project is created at exactly one area directory below the configured `dev_root`, so no other
location can be targeted, and an existing directory is never replaced.

The registry write appends one `{path, name, area, enabled, added}` entry and nothing else. The
existing bytes of the registry named by `registry_path` are kept as an exact prefix rather than
rewritten in bulk, which is what the portfolio tooling's drift detection expects of any writer.
The write is serialized by an exclusive lock that covers cooperating writers only, so a concurrent
hand edit is not protected. The extended document is re-parsed before it is published, and
publication is atomic; a registry that does not already read cleanly is not extended, nor is one
whose shape a block append cannot extend, such as an empty flow-style `projects: []` list, and a
symlinked registry is written through rather than replaced. A canonical path the registry already
holds is refused, including one held by a disabled entry. If the registry write fails, the created
directory is removed, so the registry never holds an entry for a directory that is not there. A
removal that itself fails is reported rather than hidden, leaving an unregistered directory behind.

A project created from Telegram is selectable there immediately, because the bot re-reads the
catalogue after the mutation. One created with the command line lands in a separate process, so a
running bot does not see it until it re-reads: press Refresh in any paginated view, which
returns Home, then open Launch again. No registry field
outside that closed schema is written, and neither surface can edit or remove an entry that
already exists.

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
