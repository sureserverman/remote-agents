# Acceptance record: 2026-08-01

The private production service was enabled and active after `systemctl --user daemon-reload` and
`systemctl --user enable --now remote-agents.service`. The reviewed unit passed
`systemd-analyze --user verify`.

The owner-driven Telegram trace covered all currently qualified profiles: Claude, Claude Remote,
Codex, OpenCode, and Cursor Agent. Each recorded readiness, graceful stop, preserved-pane exit,
and cleanup. Confirmed force-stop traces were also recorded. The production acceptance audit
passed:

```bash
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 \
  uv run --locked pytest -m live_acceptance tests/live/test_profiles_through_telegram.py -q
```

The dedicated managed tmux server was empty after the final cleanup. Service restarts retained
the dedicated tmux server while polling restarted, as required by `KillMode=process`. No default
tmux-server session was targeted.

The full production doctor reported healthy core, SQLite store, dedicated tmux command, private
Telegram credential-file boundary, active user service, and all five qualified profiles. The
credential-denial drill uses a known-invalid credential and does not replace the production
credential; incident rotation and rollback are documented in the operator runbook.

## Usability refresh follow-up: 2026-08-02

The owner-scoped Telegram shell was refreshed with `/start`, `/launch`, `/sessions`, and `/help`;
the command menu is scoped to the configured private chat and the default/global command list is
empty. The bot description, short description, and reviewed circular avatar were updated after a
private rollback snapshot was recorded. The read-only `telegram-ui-audit` command verifies those
metadata values without emitting the credential.

The executable fake journey now covers the status Home, search and optional-label recovery,
Review and outcome, lifecycle detail, escaped inline inspection, UTF-8 attachment inspection,
Back/Cancel, and expired-view recovery. A separate real mobile owner journey remains the final
visual qualification step; this record does not claim that it has been witnessed yet.
