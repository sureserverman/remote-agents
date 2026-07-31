# Database recovery

The SQLite database stores only managed-session metadata. A corrupt or unavailable database
blocks every mutation; it never grants a fallback path to launch, stop, clean up, or force-stop
an agent. Inspect managed tmux panes locally while the database is unavailable:

```bash
tmux -L remote-agents list-panes -a
```

Stop the user service before restoring. The command refuses to overwrite a healthy database,
preserves unreadable `sessions.sqlite3` evidence as `sessions.sqlite3.corrupt`, and restores only
from a readable current-schema backup.

```bash
systemctl --user stop remote-agents.service
uv run --locked remote-agents restore-database \
  --database "$HOME/.local/state/remote-agents/sessions.sqlite3"
systemctl --user start remote-agents.service
```

The default backup path is `sessions.sqlite3.bak`; add `--backup /absolute/path/to/backup.sqlite3`
only when restoring a separately retained verified backup. Do not delete either the `.corrupt` or
`.bak` file until the restored service has passed its health check.
