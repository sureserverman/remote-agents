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
