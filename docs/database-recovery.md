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

## Undoing the store split

Since the store split, this host keeps **two** databases side by side in
`~/.local/state/remote-agents/`:

- `sessions.sqlite3` — session state, the file the change watcher fingerprints
- `ui.sqlite3` — what the Telegram surface writes about itself: callback tokens, the live
  view's anchor, standing and trust notifications

They are separate because the watcher's signal is a file's metadata. While the bot's callback
tokens shared `sessions.sqlite3`, minting a keyboard was indistinguishable from another process
launching a session, so an open sessions page republished its own change and redrew about
thirty times a minute until Telegram flood-banned the bot.

**When to reach for a rollback.** Only when the surface has lost state it should have — an
empty sessions list that should not be, buttons that resolve to nothing, notifications that
vanished — *and* `ui.sqlite3` exists. If `ui.sqlite3` is missing entirely, the split never ran
and there is nothing to undo.

**Stop the service first.** Up to five writers share these files across four processes.

    systemctl --user stop remote-agents

**Put the surface tables back in the domain store:**

    python3 - <<'PY'
    from pathlib import Path
    from remote_agents.adapters.sqlite.store_split import unsplit_stores
    print(unsplit_stores(Path.home() / ".local/state/remote-agents/sessions.sqlite3").restored)
    PY

It prints a row count per table it restored, and is safe to run twice — run it again if you are
not sure it worked. It recreates the tables from `ui.sqlite3`'s own schema, so it works whether
or not the domain store still has them.

**If that is not enough**, the split wrote a full snapshot of the domain store before it moved
anything:

    ls ~/.local/state/remote-agents/sessions.sqlite3.pre-split-*.bak

Restore one with the ordinary restore procedure above. Note the version rule that procedure
already carries: a backup predating a migration will read as *not ready* to a newer build.

**After a rollback the bot works and the flood-ban cause returns.** Rolling back puts the
surface's writes back in the watched file, which is what made the redraw republish its own
change. Treat it as a way to recover state, not a place to stay.
