# Operator runbook

## Health and installation

Run these from the repository after installing the unit and private configuration described in
the README:

```bash
systemd-analyze --user verify systemd/remote-agents.service
systemctl --user daemon-reload
systemctl --user enable --now remote-agents.service
systemctl --user is-active remote-agents.service
systemctl --user is-enabled remote-agents.service
uv run --locked remote-agents doctor --json | python -m json.tool
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

`active` and `enabled` are required. Profile entries are `AVAILABLE` when their executable is
present; version reporting is informative and local updates remain launchable. A missing
executable is `BLOCKED` and must not be launched from Telegram. Each launch still has to reach
its agent-specific readiness state.
The full doctor reports the non-secret state of core, store, tmux, Telegram credential-file
boundary, service, and each profile. It must report `healthy: true` before normal operation.

Confirm Telegram's discoverable owner shell without printing the credential:

```bash
uv run --locked remote-agents telegram-ui-audit --json | python -m json.tool
```

The report must be healthy, with no default/global commands and exactly `/start`, `/launch`,
`/sessions`, and `/help` in the configured owner's chat scope. The owner chat's menu opens
commands; the bot description and short description are checked against the reviewed values.

## Telegram acceptance checklist

Begin with `/start`. Use only the configured private chat. The Home dashboard shows Active and
Preserved counts, then Launch and Sessions. `/launch`, `/sessions`, and `/help` offer the same
owner-only entry points from Telegram's command menu.

1. Open Launch and use Search to find a project by name. The reply prompt names the expected
   input; use Cancel or Back to leave it, and retry an empty or unmatched search.
2. Select an agent, add an optional label or Skip it, and confirm that Review names the project,
   agent, and label before Launch. Back restores the preceding choice; Cancel makes no mutation.
3. Launch two sessions for the same project/profile, applying an optional label to one. Their
   project, agent, mode, and sequence remain distinguishable.
4. Open Sessions, inspect one active session, and verify that inline output is bounded, escaped,
   and clean. For oversized output, verify the separate UTF-8 `session-output.txt` attachment.
5. Gracefully stop one session, inspect its preserved output, then choose Cleanup.
6. For a separate active session, use Force and confirm the second Force stop button.
7. Restart `remote-agents.service`, press an expired pre-restart button, and verify that it
   acknowledges expiry and replaces the view with a fresh Home. Verify the remaining managed
   session has the same identity.
8. Use `tmux -L remote-agents list-panes -a` only for local read-only confirmation. Never use
   the default tmux server for this service.
9. For a live managed Claude session only, open Details and confirm Enable or Disable Remote
   Control. If its state is unknown, do not retry remotely; inspect and recover locally. Never
   share the resulting Remote Control URL or a pane capture outside the owner workflow.
10. Open Resume for a project with prior Claude or Codex work. Its buttons show a bounded
    provider title or resume description when supplied, never a provider ID. The bot does not
    scan, control, or adopt arbitrary local agent processes; use only provider-catalogued
    conversations to create a new managed session.
11. Open Add Project. The area buttons must name only eligible existing directories under the
    configured `dev_root`, excluding hidden ones, `archive`, and `archives`. Enter a rejected name
    such as `New Thing` and confirm it is refused with no directory created, then enter a valid
    name and confirm that Review shows the area and the name before the mutation. Cancel at Review
    and verify nothing was created or appended. Repeat and confirm, then verify that the new
    project is selectable from Launch without restarting the service, and that a second create
    with the same area and name is refused.

For the auditable host-local profile trace, run:

```bash
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 \
  uv run --locked pytest -m live_acceptance tests/live/test_profiles_through_telegram.py -q
```

## Telegram credential denial and recovery drill

The test suite exercises a known-invalid credential against Telegram without reading or replacing
the production credential. Run it together with the configured-owner check:

```bash
set -a
. ~/.config/remote-agents/telegram.env
set +a
uv run --locked pytest -m live_telegram tests/live/test_telegram_owner.py -q
```

Rotate a production credential only when required by an incident. A revoked or replaced
credential must cause polling failure and a systemd restart attempt; it must not mutate tmux
sessions. Restore service only after the replacement token is present in
`~/.config/remote-agents/telegram.env` with mode `0600`:

```bash
systemctl --user restart remote-agents.service
systemctl --user is-active remote-agents.service
journalctl --user -u remote-agents.service -n 100 --no-pager
```

## Project creation and de-registration

Create a project from this host with:

```bash
uv run --locked remote-agents add-project --area infra --name new-thing
```

The command reads the same private configuration as `doctor`, so omitting `--config` targets the
production registry. The area must already exist directly under the configured `dev_root`, and it
must be a directory the workspace offers: hidden directories, `archive`, `archives`, and any name
outside the slug rule below are not eligible areas. The command creates `<dev_root>/<area>/<name>`
and appends one `{path, name, area, enabled, added}` entry to the registry named by
`paths.registry_path`, then prints the canonical path it recorded, which differs from the literal
`<dev_root>/<area>/<name>` when the development root traverses a symlink. A refusal exits
non-zero and prints a reason on standard error; refusals raised before the registry write name
their cause, while a failure inside the write is reported as `project could not be catalogued`
without its specific cause. Area and name must each be lowercase letters, digits, and single
hyphens, 1 to 64 characters; the check runs before any filesystem effect. A canonical path the
registry already holds is refused, including one recorded by a disabled entry.

The append keeps the registry's existing bytes as an exact prefix and publishes atomically under an
exclusive lock; a symlinked registry is written through rather than replaced. Before the extended
document replaces the registry it is loaded back through the ordinary reader and must both read
cleanly and contain the new entry, so a create either lands a readable registry or changes nothing.
A name or area that YAML would otherwise read back as a number, date, or boolean — `2026`, `no`,
`on` — is quoted for that reason; ordinary names stay unquoted. The lock serializes cooperating writers only, so do not hand
edit the registry while a create is in flight. The lock leaves a `.lock` file beside the registry;
it is created once and never removed.

Confirm the result rather than trusting the reply:

```bash
tail -n 6 ~/.claude/projects-registry.yaml
uv run --locked remote-agents doctor --json | python -m json.tool
```

The `tail` path above is the default; use whatever `paths.registry_path` names on this host. The
appended block must be the last five lines and `healthy` must still be true, with
`projects.registered` one higher than before.

A failed registry write is normally self-cleaning: the created directory is removed and the
failure is reported, so there is nothing to undo. The one case that needs an operator is a create
that could not clean up after itself, which logs `left an uncatalogued project directory behind
after a failed create`. That record goes wherever the process that
created the project logs: the `add-project` command writes it to standard error, while a create
started from Telegram writes it to the service journal. The record names no path; the directory
is the area and name that were chosen. Confirm it is empty
and remove it with `rmdir`; never use a recursive delete here. The project is absent from the
registry in that state, so no registry change is required.

Nothing in this tool removes or edits a registry entry, and hand-editing the registry is
discouraged by the file's own header because the portfolio tooling runs drift detection over it.
When a registration must be undone, copy the file first, then delete the five appended lines of
that entry's block and re-run the doctor command above to confirm `projects.registered` fell by
one. A malformed edit is not a partial failure: any schema or YAML error makes the whole registry
read as degraded, which drops **every** registered project from the catalogue at once and causes
further `add-project` calls to refuse to extend it. Restore the copy if the doctor check does not
report the expected count.

De-registration alone does not withdraw a project from the control plane. The catalogue merges the
registry with bounded discovery under the development root, so a project whose directory still
exists reappears as an unregistered entry and stays selectable for launch. The same is true of
setting `enabled` to `false`, which additionally leaves the path permanently claimed against a
future `add-project`. Removing the project directory is what actually withdraws it, and it should
be done only after inspecting the contents.

## Rollback and local recovery

To halt the control plane while preserving managed tmux panes for local recovery:

```bash
systemctl --user disable --now remote-agents.service
tmux -L remote-agents list-panes -a
```

Restore the last reviewed unit, run `systemctl --user daemon-reload`, then enable the service
again. Do not remove a managed tmux session until its ownership and output have been inspected.

Before changing bot profile metadata, retain a private rollback snapshot of the avatar,
descriptions, owner-scoped commands, and menu behavior. Restore that snapshot through the
Telegram profile controls if a rollback is required; do not replace the credential as part of a
usability rollback.

For a damaged session database, follow [database recovery](database-recovery.md). The restore
command refuses to replace a healthy database and preserves corrupt evidence.
