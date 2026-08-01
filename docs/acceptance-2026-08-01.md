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
