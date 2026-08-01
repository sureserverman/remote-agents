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

`active` and `enabled` are required. Profile entries must remain `QUALIFIED`; a changed or
missing executable is `BLOCKED` and must not be launched from Telegram.
The full doctor reports the non-secret state of core, store, tmux, Telegram credential-file
boundary, service, and each profile. It must report `healthy: true` before normal operation.

## Telegram acceptance checklist

Begin with `/start`. Use only the configured private chat.

1. Open Launch and use Search to find a project by name.
2. Launch two sessions for the same project/profile, applying an optional label to one. Their
   project, agent, mode, and sequence remain distinguishable.
3. Open Sessions, inspect one active session, and verify that the output is bounded and clean.
4. Gracefully stop one session, inspect its preserved output, then choose Cleanup.
5. For a separate active session, use Force and confirm the second Force stop button.
6. Restart `remote-agents.service`, open a fresh `/start`, and verify the remaining managed
   session has the same identity.
7. Use `tmux -L remote-agents list-panes -a` only for local read-only confirmation. Never use
   the default tmux server for this service.

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

For a damaged session database, follow [database recovery](database-recovery.md). The restore
command refuses to replace a healthy database and preserves corrupt evidence.
