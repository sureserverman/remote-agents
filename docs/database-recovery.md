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

## Migration 13 rewrites profile ids

Migration 13 (0.41.0) renames the retired `claude-remote` profile to `claude` in
`sessions.profile_id` and `sessions.resume_profile_id`. It is the one migration to date that
edits existing rows rather than only the schema, so the pre-migration backup `open_database`
takes is the way back — the usual restore above, no special case.

Rolling the *code* back on its own needs no restore. Both versions curate `claude` as a profile,
so a migrated row is valid to the older build; what an older build cannot see is the
`--remote-control` launch flag, and a session started with it keeps running regardless.

One case inside that migration is worth naming, because a migration that *fails* is a different
problem from one you want to undo. `sessions_resume_identity` is unique on
`(resume_profile_id, resume_source_id)` for rows that are not ended, and while both profile ids
existed one provider conversation could legitimately be resumed under each — two different keys,
so both rows were allowed. Renaming would make those keys identical, which SQLite refuses:

```
sqlite3.IntegrityError: UNIQUE constraint failed:
    sessions.resume_profile_id, sessions.resume_source_id
```

The migration rolls back whole, the schema stays at 12, and because migrations run at every
start the service then fails to start repeatedly rather than once. Migration 13 therefore breaks
the tie before renaming: of the two rows, the one under the retired id gives up its
`resume_profile_id`/`resume_source_id` — the session, its state and its history are untouched,
and the row under the id that still exists keeps the conversation. Nothing is deleted.

If you need the cleared binding back, it is in the pre-migration backup, and the restore above is
how to read it. Such a row is otherwise indistinguishable from a session that was never resumed;
no host has produced one to date.
