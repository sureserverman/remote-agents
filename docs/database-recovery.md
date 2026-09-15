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

**Read this first, before running anything.** Rolling back restores the surface's state *and*
reinstates the reason the bot was flood-banned: its own writes go back into the file the change
watcher fingerprints, so an open sessions page republishes its own change and redraws until
Telegram stops it. This is a way to recover state, not a place to stay. It is also only half an
undo — see "What this does not do" below.

Once the split is live, this host keeps **two** databases side by side in
`~/.local/state/remote-agents/`:

- `sessions.sqlite3` — session state, and the file the change watcher fingerprints
- `ui.sqlite3` — what the Telegram surface writes about itself: callback tokens, the live
  view's anchor, standing and trust notifications

**When to reach for this.** The surface has lost state it should have — an empty sessions list
that should not be, buttons that resolve to nothing, notifications that vanished — **and**
`ui.sqlite3` exists. A missing `ui.sqlite3` means either the split never ran, or it ran and the
file was lost afterwards; those are different problems and only the second is a data loss. Check
for a pre-split backup before concluding which:

```bash
ls ~/.local/state/remote-agents/sessions.sqlite3.pre-split-*.bak
```

"No such file or directory" means the split never ran on this host.

**Stop the service first.** Up to five writers share these files across four processes.

```bash
systemctl --user stop remote-agents.service
```

**Put the surface tables back in the domain store:**

```bash
cd ~/dev/infra/remote-agents
uv run --locked python -c "
from pathlib import Path
from remote_agents.adapters.sqlite.store_split import unsplit_stores
print(unsplit_stores(Path.home() / '.local/state/remote-agents/sessions.sqlite3').restored)
"
```

It prints a dict of **rows this run put back**, per table — `{}` means it found nothing to
restore, not that it failed. It is safe to run twice; a second run prints zeros because the rows
are already there.

**It refuses, loudly, in two cases**, rather than guessing:

- *"the domain store's columns … do not match the UI store's"* — a table of that name already
  exists in `sessions.sqlite3` with a different shape, so copying positionally would put values
  in the wrong columns. Rename or drop that table only if you know what wrote it.
- *"… is not something this restore knows how to recreate"* — the UI store holds a schema object
  this procedure does not handle. Do not work around it; capture the message.

**Start the service again:**

```bash
systemctl --user start remote-agents.service
```

**What this does not do.** It copies rows; it does not change where the code *writes*. Once the
split is live the surface stores still point at `ui.sqlite3`, so after a restart the bot writes
there again and the restored rows go stale. A durable rollback is a **code** rollback — deploy
the build from before the split — and this procedure is how you carry the rows back to it. It
also leaves `ui.sqlite3` in place; keep it until the restored service has passed its health
check, then it is yours to delete.

**If the surface tables were dropped before anything copied them.** Symptom: `ui.sqlite3` is
absent or empty, the surface has lost its state, and `sessions.sqlite3` is at schema version 14.
That means migration 14 ran on a store the split had not been run against — every command opens
the domain store through one function that splits first, so a build carrying that guard cannot
produce this, but a build from before it could. The rows are in the snapshot `open_database`
takes before any migration:

```bash
ls ~/.local/state/remote-agents/sessions.sqlite3.bak
```

Restore it with `--backup` as below, then start the service; the split runs on the next open and
carries the rows across properly. Do not simply re-run the rollback: it copies *from*
`ui.sqlite3`, which in this state has nothing in it.

**If that is not enough**, the split wrote a full snapshot of the domain store before it moved
anything. Restoring one needs `--backup`, because the default path is `sessions.sqlite3.bak` and
not the snapshot you just listed:

```bash
uv run --locked remote-agents restore-database \
  --database "$HOME/.local/state/remote-agents/sessions.sqlite3" \
  --backup "$HOME/.local/state/remote-agents/sessions.sqlite3.pre-split-<stamp>.bak"
```

**That command refuses a healthy database on purpose**, and the case that sends you here — the
surface lost state while session history is fine — is exactly a healthy one. It will say so and
stop. That refusal is correct: reaching for a pre-split snapshot then would trade intact session
history for surface rows you can get back with the restore above. Use it only when
`sessions.sqlite3` is itself damaged, and note the version rule the procedure at the top of this
file already carries: a snapshot predating a migration reads as *not ready* to a newer build.
