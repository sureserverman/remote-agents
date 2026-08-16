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
boundary, service, each profile, and — since BL-029 — the deployed **config** checked against
the schema this build requires. It must report `healthy: true` before normal operation. An
unreadable config reports `healthy: false` with `checked: false` and an empty `components`,
because the registry and database paths are read out of the config that would not load, so
nothing else was probed and nothing else is claimed.

`doctor` also reads back one session's recorded lifecycle history (BL-030). The
`session_events` table has been the durable audit trail since the first migration and had no
read path, so the only way to see it was to open sqlite by hand:

```bash
uv run --locked remote-agents doctor --history <session-id>
uv run --locked remote-agents doctor --history <session-id> --json | python -m json.tool
```

Events are listed in write order, not timestamp order — a graceful stop records its request,
the pane exit and the cleanup inside one operation, and the second-resolution timestamps tie.
The callback token stored on each row is never printed; the sanitized `error_code` is.

Confirm Telegram's discoverable owner shell without printing the credential:

```bash
uv run --locked remote-agents telegram-ui-audit --json | python -m json.tool
```

The report must be healthy, with no default/global commands and exactly `/start`, `/launch`,
`/sessions`, and `/help` in the configured owner's chat scope. The owner chat's menu opens
commands; the bot description and short description are checked against the reviewed values.

## Telegram acceptance checklist

Begin with `/start`. Use only the configured private chat. The Home dashboard shows Active and
Preserved counts, then Launch and Sessions. There is no Refresh: every route back to a screen
re-reads what it shows, so the counts and the session list are current on arrival. `/launch`,
`/sessions`, and
`/help` offer the same owner-only entry points from Telegram's command menu.

1. Open Launch and use Search to find a project by name. The reply prompt names the expected
   input; use Cancel or Back to leave it, and retry an empty or unmatched search.
2. Select an agent. The session starts on that press — there is no review screen and no label
   step in front of it — and the screen shows "Launching…" until the agent reports ready.
   Press the same agent a second time while it starts: the repeat is dropped, not serviced,
   and no second session appears.
3. Launch two sessions for the same project/profile, then open one and use `Rename` to name it.
   Send `Skip` to leave a session unnamed and `Cancel` to leave the step. Their project, agent,
   mode, and sequence remain distinguishable whether or not either carries a name, and a name
   set here also shows on the local surface, which reads the same store.
4. Open Sessions, inspect one active session, and verify that inline output is bounded, escaped,
   and clean, and that Back returns to that session with its actions intact. For oversized
   output, verify the separate UTF-8 `session-output.txt` attachment and that Telegram refuses
   to forward it.
5. Stop and close one session. Verify the screen shows what it is waiting for while the agent
   exits, then **lands on the session list** with "Stopped <session>" as its lead line above the
   remaining rows — not on a screen of its own, and with no Back button — and that the session
   reaches ENDED in that single action with its pane gone from
   `tmux -L remote-agents list-panes -a`. Stopping the *last* running session should still show
   the outcome, above an empty list.
6. For a separate active session, use Force stop and verify the confirmation names the session
   and offers Cancel before the kill. Cancel it once, then confirm it.
7. With more sessions than one page holds, page through Sessions with Previous and Next, open a
   row from a page other than the first, and verify Back returns to that page rather than to
   the top of the list. Home's Sessions button and `/sessions` still open the first page.
8. Open Launch. The project you have launched from most recently is first, ahead of registry
   order — including on the first Launch after a service restart, with nothing pressed
   beforehand. Confirm Resume and a search that matches both projects agree with it.
9. Restart `remote-agents.service`, press a button drawn before the restart, and verify that it
   still works: it renders the screen it names rather than reporting anything. Verify the
   remaining managed session has the same identity.
10. Use `tmux -L remote-agents list-panes -a` only for local read-only confirmation. Never use
   the default tmux server for this service.
11. For a live managed Claude session only, open Details and confirm Enable or Disable Remote
    Control. If its state is unknown, do not retry remotely; inspect and recover locally. Never
    share the resulting Remote Control URL or a pane capture outside the owner workflow.
12. Open Resume for a project with prior Claude or Codex work. Its buttons show a bounded
    provider title or resume description when supplied, never a provider ID. The bot does not
    scan, control, or adopt arbitrary local agent processes; use only provider-catalogued
    conversations to create a new managed session.
13. Open Add Project. The area buttons must name only eligible existing directories under the
    configured `dev_root`, excluding hidden ones, `archive`, and `archives`. Enter a rejected name
    such as `New Thing` and confirm it is refused with no directory created, then enter a valid
    name and confirm that Review shows the area and the name before the mutation. Cancel at Review
    and verify nothing was created or appended. Repeat and confirm, then verify that the new
    project is selectable from Launch without restarting the service, and that a second create
    with the same area and name is refused.

The bot keeps one message in the chat and edits it, so a restart normally leaves nothing behind.
One case does need the owner. If the service restarts between a reply prompt being sent and the
owner answering it, that input box is a second message and nothing is left running to remove it;
the Bot API cannot enumerate a chat, so it cannot be found afterwards either. Delete it by hand.
Messages the owner sends unprompted are left in place rather than deleted, which is deliberate:
only the command messages the bot answers are removed. Telegram also stops permitting an edit 48
hours after a message was sent; when it refuses, the live view is re-sent as a new message and the
old one deleted, so an old chat occasionally shows the view move rather than change in place.

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

## Agent activity notifications

> **Upgrading an existing host: edit the config before you restart the service.** This feature
> added two keys to `[limits]`, and `config.py` validates that table against an *exact* key set —
> unknown keys **and** missing ones are refused. So a config written before this release makes the
> new service exit 1 on startup, and `Restart=on-failure` turns that into a crash-loop:
>
> ```text
> remote_agents.config.ConfigError: limits has unknown or missing keys:
> ['activity_poll_seconds', 'activity_quiet_polls']
> ```
>
> Add both to `~/.config/remote-agents/config.toml` under `[limits]` first — the shipped defaults
> are in `config/remote-agents.example.toml`:
>
> ```toml
> activity_poll_seconds = 30
> activity_quiet_polls = 3
> ```
>
> They are deliberately required rather than defaulted, which is the same rule that rejects a
> typo'd key: this file is small, exact and hand-edited, and a silently defaulted knob is one the
> owner never learns they have. The cost is this upgrade step, and the error names both keys.
> Found by the acceptance run on 2026-08-11 rather than by any test, because every test builds its
> own config and so can never be out of date with the code.
>
> **`doctor` now catches this class before the restart does.** Run `remote-agents doctor --json`
> against the deployed config first: it no longer raises on a config it cannot load, and reports
> the drift under `config` — `missing` and `unknown` naming the keys, and `invalid` carrying the
> refusal for **any other reason `load_config` says no once the key sets agree**. That is wider
> than an out-of-range number: a section that is not a TOML table, a path that is relative or does
> not exist, and the refusal of a file carrying a token or secret all land there too — as does a
> file that is not valid UTF-8. Most name the setting at fault; the token refusal deliberately
> names nothing, and reads only `TOML must not contain tokens or secrets`. `healthy` is `false`
> for any of them. Naming the keys is the point: the fix above
> is four lines of TOML, and a report that said only "the config is wrong" would send you back
> here to work out which four.

The service sends unprompted messages when a managed agent stops working, one message per
session per delivery pass, beside the live view rather than inside it. Two sources feed them and
only one has to be installed. A managed `claude` or `claude-remote` session reports through Claude Code's own
hooks; every other curated profile — `codex`, `opencode`, `cursor-agent` — has no hook system, so
it is watched by capturing its pane. The hooks are not installed by the unit, by `serve`, or by
`doctor`. Install them once per host:

```bash
uv run --locked remote-agents install-agent-hooks
```

With no arguments this writes to `~/.claude/settings.json` — the owner's global agent
configuration, which this project does not own — and adds one matcherless group to each of
`Stop`, `StopFailure`, `Notification` and `SessionEnd`, each holding one command:

```json
{
  "hooks": [
    {
      "type": "command",
      "command": "/path/to/.venv/bin/python -m remote_agents agent-event"
    }
  ]
}
```

The command names the interpreter that performed the install rather than the `remote-agents`
console script, because a hook runs with whatever environment the agent happened to have and a
virtualenv's `bin` need not be on its `PATH`; a hook that fails to resolve is worse than no hook,
because nothing reports it. Re-run the install after moving or rebuilding that virtualenv. A group
is recognised by the parsed words after the interpreter, so a stale entry is replaced rather than
joined by a second one.

**It merges, and it only ever adds those four groups.** Every other key, every other event, and
every other group under those four events is copied across untouched — including a `SessionEnd`
hook of your own, which is the case this host's real settings file presents. **It is idempotent:**
a second run reports `agent hooks already current in <path>` and writes nothing. If an entry
already runs this subcommand in a form the installer does not recognise — a wrapper, a hand-edit,
a future version — the summary says so and names the events, because it will not touch that entry
and the hook then runs twice for them.

**It refuses rather than damages.** A settings file that is not valid JSON is left exactly as it
was found and the command exits 1:

```text
~/.claude/settings.json is not valid JSON (Expecting property name enclosed in double quotes:
line 2 column 1 (char 19)); it has been left untouched
```

The same refusal, always without writing, covers a `hooks` key that is not a JSON object, one of
those four events whose value is not a JSON array, a file whose exact formatting cannot be
reproduced (so a later `--remove` would reformat the rest of it), an empty `"hooks": {}` block
that removal could not tell apart from no `hooks` key at all, and a file that changed on disk
while the command was preparing its edit. Each prints its reason on standard error and exits 1.

Three flags exist and no others. `--settings <path>` names the file to operate on, `--activity-dir
<path>` names the spool the installed command will write to, and `--remove` takes the hooks out
again. Both path flags default to the real ones and exist so that the live drill can drive a real
agent end to end without going near either. `--activity-dir` must be absolute, and it is refused
when another user can write to any existing ancestor of it without the sticky bit. Note that
`serve` always drains `~/.local/state/remote-agents/activity`, so an `--activity-dir` pointing
anywhere else in production means the hooks spool where the service does not read.

### Verifying the install

Confirm the four groups are present:

```bash
python3 -m json.tool ~/.claude/settings.json | grep -c 'remote_agents agent-event'
```

That must report `4`. Then confirm the guard, which is what makes a global hook safe to install —
the hook writes nothing and exits 0 when `REMOTE_AGENTS_SESSION_ID` is absent from its
environment, so a Claude session started outside a managed pane notifies nobody:

```bash
mkdir -p /tmp/ra-hook-check
printf '{"hook_event_name":"Stop","last_assistant_message":"drill"}' \
  | env -u REMOTE_AGENTS_SESSION_ID \
    uv run --locked remote-agents agent-event --activity-dir /tmp/ra-hook-check
ls -A /tmp/ra-hook-check
printf '{"hook_event_name":"Stop","last_assistant_message":"drill"}' \
  | REMOTE_AGENTS_SESSION_ID=drill \
    uv run --locked remote-agents agent-event --activity-dir /tmp/ra-hook-check
ls -A /tmp/ra-hook-check
```

The first listing must be empty and the second must hold exactly one `0600` file named
`drill-<timestamp>.json`. Both invocations exit 0: a hook that fails must never break the agent it
is attached to, so every path through it — a malformed payload, an oversized one, an unwritable
spool, no stdin at all — reports success and costs at most one record. Remove the drill directory
afterwards; it is not the spool the service reads.

The variable itself is injected only into the panes this service launches or resumes, as part of
the curated environment (`HOME`, `LANG`, `LC_ALL`, `PATH`, `TERM`) those panes are given. It is
launch-time context for the hook and never an authority on which session is which; the store and
the tmux inventory remain the authority, exactly as they are for a stop (DEC-006).

**It is inherited by descendants of a managed pane, and the hook cannot tell that apart.** The
variable goes into the managed agent's own process environment — the fixed runner `execvpe`s the
agent with it — so every process started beneath that agent carries it too: a `claude` you start
from a shell inside a managed session, and a `claude` the managed agent runs through its own Bash
tool. Those sessions spool under the **parent's** session id, so their `Stop` or `SessionEnd`
reaches you as a notification naming a managed session that has not finished or ended. Nothing here
is configurable, so this is knowledge rather than a setting: an unexplained "finished" for a session
you can see still working usually means a nested `claude`. A *sibling* tmux pane does not inherit it
— the variable never enters the tmux server's environment, only the launched process's — so an
unrelated pane on the same server is silent, as is any `claude` started outside the managed tree.

### Removing the hooks

```bash
uv run --locked remote-agents install-agent-hooks --remove
```

This deletes only the groups this installer wrote and restores the file to its pre-install content
**byte for byte** — the file's own indentation, separators and trailing newline are recovered from
its original bytes rather than re-picked by a JSON writer, and the install refuses outright when it
cannot promise that. A group you have hand-edited to run this command beside one of your own is
left alone, because failing to remove a hook is recoverable and deleting somebody else's is not. On
a host that was never installed to, `--remove` reports `no agent hooks in <path>` or `no settings
file at <path>` and exits 0.

### When notifications stop arriving and nothing complains

The failure mode to know about is a symlink standing anywhere on the spool path. The hook opens its
directory through a check that refuses to write *through* a link, and it cannot say so: it is
running inside the agent's process, where raising would disrupt the session the owner is working
in. So it returns 0, writes nothing, and does that on every event for as long as the link is there.
The service is the half that can complain, and does — `serve` calls `ensure_directories`, which
raises `production paths cannot traverse symlinks` and refuses to start. If notifications have gone
quiet with a healthy service, check the path itself before anything else:

```bash
namei -l ~/.local/state/remote-agents/activity
ls -A ~/.local/state/remote-agents/activity | wc -l
```

Every component must be a real directory, and the leaf `drwx------`. A spool that stays empty while
managed Claude sessions finish work is this fault; a spool that grows without shrinking is the
opposite one, and points at the service rather than at the hook — check
`journalctl --user -u remote-agents.service` for `activity watch pass failed`.

The other way notifications go missing is a restart. Whenever Telegram is unreachable, refusing
sends, or simply behind — a burst of more than ten messages in one pass is throttled and the
remainder queued, which is ordinary and clears itself within seconds — the undelivered ones are
held **in memory only**, and a pass that delivered nothing while holding some says so: `holding N
undelivered notification(s) in memory; a restart now loses them`. It used to fire on *any*
non-empty queue, which meant it also fired on the ordinary throttled pass just described — in
simulation, on eight of forty passes with Telegram working perfectly — so the line an operator
was meant to spot an outage by was one they learned to scroll past. It now names the case worth
looking at: nothing got through, and a restart right now would drop those N. It still reports the
queue rather than the cause, so it does not by itself mean Telegram is failing. The queue itself holds at most 200, matching what one drain may
hand over; past that, eviction falls on the session holding the most of the queue rather than on
whatever arrived first, so a chatty session can no longer push out a quiet session's only report.
There is nothing behind that queue, and that
is DEC-026 rather than an omission: a durable queue was weighed against a schema migration and a
second spool to drain and bound forever, and declined, because the session itself is the
authoritative record of what an agent did. What a restart during an outage costs the owner is
being told, not the fact. So if that warning is in the journal and you must restart anyway, read
the sessions afterwards rather than waiting on the notifications — they are not coming.

### What each notification means

Each message names the session by its display identity and carries a single `Open session`
button that renders that session's detail into the live view. There is no other button: a
notification is not a screen, so it may not carry navigation. It is sent apart from the live
view, so navigating the live view (Back, Home, a session detail) leaves it alone — the anchor's
pruning does not own it.

A session gets one message per delivery pass rather than one message per observation, so several
things it has to report in one pass ride together instead of arriving as separate messages that
each push the live view down again. A lone observation keeps the old shape: the sentence, then
the agent's text on its own line. Two or more take `• ` bullets, newest first, each folding the
agent's text onto the same line after an em-dash; the message holds at most five lines, and
anything past that is summarized in a trailing `and N earlier.` line rather than growing the
message without bound. Two observations of the same kind carrying the same text collapse to one
line bearing the later timestamp; the same kind with different text is not the same report, so
both survive. An observation the message could not fit is not lost — it stays queued, and on the
next pass it claims a line ahead of what the message is already showing, because there is no
second message for it to go out in.

**A session owns one message, and later news is re-rendered into it.** The first observation
creates the message; everything after it edits that message in place, however many passes later
it arrives. This is what makes the count bounded: three turns half an hour apart used to be
three messages, each pushing the menu further up, and are now one message amended twice.
Two consequences are worth knowing. An edit does not re-notify your phone, so news after the
first arrives quietly — the message is current, but nothing buzzes. And an edited message stays
where it was sent, so a session that has been quiet for a while keeps its place in the chat
rather than jumping to the bottom.

The message is replaced by a fresh one only when the old one is no longer there: you pressed
`Open session`, which deletes it, or Telegram will no longer accept edits to it — messages
become uneditable after 48 hours. The per-`(session, kind)` window still governs whether a
*new* message may be created; it has no say over amending one that already exists, because a
line added to a message already on the screen costs nothing to scroll past.

Two things follow from Telegram ordering a chat purely by send time, and both were added after
the first real run showed what their absence feels like. **Pressing `Open session` deletes that
notification**: it has been acted on, and left in place it becomes one of a growing pile of
alerts already dealt with, each still offering the button just pressed. And **every pass that
sends a new notification moves the live view to the bottom of the chat**, re-sending it below
whatever arrived, because a new notification always lands *below* the menu and editing the anchor
in place cannot move it back. Without that, the menu drifts upward until reaching it means
scrolling past the notifications. A pass that only *amends* messages already in the chat does not
move the menu: nothing was added, so nothing got in front of it, and moving it anyway would
delete and re-send your screen to answer an update you could not see. The move re-binds the
screen's callback tokens to the new message, so no button on it dies; if the move fails, the
notifications are still delivered and the menu simply stays where it was.

| Kind | Sentence the owner sees | Source | Reported or inferred |
|---|---|---|---|
| `completed` | "The agent has finished its work." | Claude's `Stop` hook | reported |
| `limit_reached` | "The agent stopped after reaching a usage limit." | Claude's `StopFailure` hook, `error: rate_limit` | reported |
| `output_limit` | "The agent stopped at its output length limit for one reply." | Claude's `StopFailure` hook, `error: max_output_tokens` | reported |
| `needs_answer` | "The agent is waiting for an answer." | Claude's `Notification` hook, `notification_type: permission_prompt` or `agent_needs_input` | reported |
| `quiet` | "No output since 14:05 UTC." | this service watching the pane | inferred |

The first four are the agent reporting on itself, and each carries at most one bounded, escaped
line of what it last said. Everything else those hook fields can carry — every other value of
`error`, every other `notification_type` — is dropped rather than mapped to the nearest neighbour:
reporting the wrong reason an agent stopped is worse than reporting nothing.

**A notification has to be worth acting on, and two kinds that once appeared here were not.** The
bar is no longer only "does this say why the agent stopped" but "is there anything for the owner
to do about it", and both are now checked at the mapping, so a record that fails the second is
drained, deleted and dropped exactly like one that fails the first:

- **`ended`**, from `SessionEnd`. It fired whenever a session ended, including one the owner had
  just ended themselves — the graceful stop types `/exit` into the pane, so pressing Stop in
  Telegram reliably produced a message reporting that press back. Every `reason` mapped to the
  same sentence, so it could not even distinguish the owner's exit from any other.
- **the inferred `needs_answer`**, from `notification_type: idle_prompt`. A sixty-second timer
  upstream, with recorded false positives and false negatives. When it was wrong it interrupted
  the owner for nothing; when it was right, `permission_prompt` or `agent_needs_input` had usually
  said so already, and said it as a fact rather than a guess.

The hook still fires for both, and `install-agent-hooks` still registers `SessionEnd` — the
records are still written and still drained off disk. What changed is that nothing is built from
them, which is deliberate: a kind that is never produced cannot then be rendered, rate-limited, or
delivered by mistake somewhere downstream.

`limit_reached` and `output_limit` are separate because the next move differs: a rate limit is
waited out or paid around and the work is untouched, while a reply that hit its output ceiling is
simply continued from. They were one kind under the `limit_reached` sentence until this was
corrected, which named the alarming one for the routine event.

The field names above are the ones the installed agent actually sets — `StopFailure` carries
`error`, `SessionEnd` carries `reason`, `Notification` carries `notification_type`. Two of the
three were wrong in the shipped code (`error_type` and `end_reason`), which made `limit_reached`
unreachable: a managed session stopping on a rate limit spooled a record whose reason was null and
the drain dropped it, silently. `tests/live/test_agent_activity_hooks.py` now reads the installed
bundle rather than a fixture, so the same drift fails a test instead of losing notifications.

**Notifications are only sent about a session that is still live** — `starting` or `running`.
A record that has reached `stop_requested`, `preserved`, `failed`, `ended` or `orphaned` is one
the owner has already dealt with, so an agent's report about it tells them their own action back.
That check is made when the message is *sent*, not when the record was drained, which is the case
that matters: an activity can wait in the retry queue across passes while Telegram is refusing
sends, and the owner can press Stop while it waits. Declining to speak is logged as
`not notifying about a session that is no longer running`, separately from the rarer
`dropping an activity this service will not speak about`, which means a session this service can
no longer identify at all.

`quiet` appends one further sentence, "This is a guess, not something it reported." It is this
service's own heuristic, and the only signal available for the three
profiles with no hook system: the pane's captured output is digested each poll, and a digest that
has not changed for the configured number of polls is reported once. It says what was observed —
that no output has appeared since a given minute — and never that the agent finished, because the
service did not see that. It carries no agent text at all, since nothing said anything; the last
line of an idle screen rendered under a session's name reads exactly like a parting statement. A
change must be seen before an absence of change means anything, so a restart does not report every
idle pane on the host, and the report fires once per quiet spell, re-arming only when the pane
changes again. The time in the sentence is the moment the threshold was crossed, so the true
silence began `activity_quiet_polls × activity_poll_seconds` earlier — the sentence is true and
understated, which is the right direction for a heuristic to be wrong in.

Both knobs live under `[limits]` in `~/.config/remote-agents/config.toml`. The shipped sample sets
`activity_poll_seconds = 30` (bounded 5–600) and `activity_quiet_polls = 3` (bounded 2–20), so a
pane goes quiet 90 seconds after its last change by default.

Separately, and not configurable, each kind of a session's news is rate-limited on its own — a
`Stop` hook fires per turn rather than per task, so an agent working through a long instruction
reports "finished" repeatedly and each report is true. The first message of a kind is always
prompt; the window then **doubles for each consecutive repeat of that same kind**, from 2 minutes
to 4, 8, 16, 32, and no further than one message every 64 minutes. Only the second and later
copies are made rarer, and the cap keeps the signal alive rather than muting it: an agent that has
been waiting all night is still waiting, and a window that kept doubling would amount to never
mentioning it again. The window decides only whether a message is sent at all, never which lines
a sent message carries: if anything in a session's group is due, the whole group goes out,
because a second line in a message the owner is already receiving costs nothing.

The limit is keyed by session *and* kind, and so is the backoff. An agent that finishes and then
needs an answer has said two different things and both arrive. A **different** kind for the same
session also resets that session's repeat counts to zero, because a different kind means something
changed and the count is a claim that nothing has: an agent that finishes, is asked something, and
finishes again is not repeating itself, and its second "finished" arrives at the base window rather
than an hour later. The other kinds keep their timestamps, so a genuine burst is still collapsed.
A repeat is counted only when a message was actually sent; a suppressed one does not advance the
backoff.

The doubling described above only started working correctly in this release. Before it, the rate
limit's memory was discarded after 4 minutes, so any kind reporting less often than that was
always treated as first-time and never backed off: measured, a `Stop` every 5 minutes produced 96
messages over 8 hours where the taper intends 12. Operators upgrading will notice fewer repeated
notifications from a busy agent, and that is intended, not a fault.

One further bound sits above all of that, and it is the only one about the chat rather than about
a session's news: **at most ten messages are sent per poll.** The per-session limit cannot
provide this, because twenty sessions stopping at once are twenty separate keys and none of them
suppresses another — past Telegram's per-chat rate, at which point its refusals would come back as
a growing backlog. Nothing is dropped: the remainder stays queued and the next poll takes it, so a
burst arrives spread over a minute or two instead of being refused.

## Local terminal visual baselines

Every position the terminal wizard can be in has a committed SVG baseline under
`tests/unit/adapters/tui/snapshots/`, captured through Textual's own `App.export_screenshot()`
and compared byte-for-byte by `tests/unit/adapters/tui/test_tui_snapshots.py`. The rest of that
directory asserts behaviour — that a key issues a command, that a rendered row decodes to an
action — so the baselines are the only thing asserting what the owner actually *sees*. A change
that drops a state explanation, reorders a confirm so the destructive row rests under the cursor,
or renders into a pane nobody displays passes every other test in the suite and fails here.

When a change to the surface is meant to alter what is on screen, regenerate it and then
**review the SVG diff** before committing:

```bash
REMOTE_AGENTS_SNAPSHOT_UPDATE=1 uv run --locked pytest tests/unit/adapters/tui/test_tui_snapshots.py -q
git diff -- tests/unit/adapters/tui/snapshots/
```

Read the diff rather than accepting it. An unreviewed regeneration turns the baselines from a net
into a rubber stamp, and it is most tempting precisely when a change was *expected* to move
them — which is when a second, unintended change rides along unnoticed. To read one as rendered
text rather than as markup, strip the SVG's text nodes:

```bash
python3 -c "import re,html,sys;print(html.unescape(''.join(re.findall(r'<text[^>]*>(.*?)</text>',open(sys.argv[1]).read(),re.S))))" \
  tests/unit/adapters/tui/snapshots/SESSION_DETAIL.svg
```

A missing baseline fails rather than being written silently, so a newly added position must be
generated deliberately and looked at once. Five things are pinned to keep the captures
reproducible — three environment dependencies (the terminal size, the theme, and colour via
`NO_COLOR` / `FORCE_COLOR`) and two wall-clock ones (the age column and the input cursor's
blink timer, the two that would flake on a merely busy machine). The count matters when you
are diagnosing a failure: an unpinned theme or colour setting fails *all sixteen* baselines at
once, so a mass failure points at the environment rather than at your change. See the test
module's docstring for the full rationale rather than duplicating it
here; it is the copy that sits next to the code and will be updated with it.

## Local terminal acceptance checklist

`remote-agents tui` launches one curated agent on this host and then hands the terminal to its
tmux pane. It needs no Telegram credentials and no running service, but accept it with the
service active, because the point of the check is that both surfaces work over one store:

```bash
uv run --locked remote-agents tui
```

1. Confirm the wizard opens on the project list with the filter focused. Type to narrow it, press
   enter to move into the list, and choose a project with the arrows and enter.
2. Confirm the agent list names each curated profile and the blocking reason beside any that is
   unavailable, and that selecting a blocked one is refused rather than launched.
3. Skip the label with an empty enter, and confirm Review names the project, agent, and label with
   Back highlighted rather than Launch. Cancel returns to the project list with no mutation; Back
   restores the agent choice.
4. Launch, and confirm this terminal is replaced by the attach and the pane holds the chosen
   agent. Detach with tmux's own binding — `Ctrl-b d` on a stock tmux, or `d` under whatever
   prefix `~/.tmux.conf` sets on this host, since this project ships no tmux configuration and
   sets no prefix. The session must survive the detach.
5. In Telegram, open Sessions and confirm the session started from the terminal is listed,
   inspectable, and stoppable exactly like one the bot started. Launch one from Telegram and
   confirm it is equally a managed session for the terminal's store; neither surface owns a
   session the other cannot manage.
6. Run `remote-agents tui` from inside a tmux client and launch. The launch must still happen, but
   the attach must be refused rather than nested, printing the command that reaches the new
   session. Nothing is launched twice.
7. Press Ctrl+N, confirm the offered areas are the eligible existing directories under the
   configured `dev_root`, enter a rejected name such as `New Thing` and confirm nothing is
   created, then create a valid one and confirm it becomes selectable without leaving the app.
   Escape is Back, Ctrl+Q quits, and Ctrl+R re-reads the screen you are on rather than
   returning to the project list — confirm on the sessions view that it re-lists in place.
   Confirm the footer drops Refresh on a screen with nothing to re-read, and that typing a
   project name greys Ctrl+N, Ctrl+S and Ctrl+O rather than discarding what you typed.

## Terminal and service on one database

The terminal and the service are separate processes writing one SQLite file. The terminal refuses
any `database_path` outside the private state directory exactly as `serve` does, so sharing the
store is not a configuration accident; two consequences of it must be understood before a second
surface is used.

Duplicate-command protection is durable and does hold across processes: every launch claims an
idempotency key with a unique insert into the database, so a key one process has claimed is
refused in the other. The per-process `SessionLocks` do not hold across processes. Each
`SessionService` constructs its own, so they serialize concurrent mutations only inside the
process that owns them, and the service's per-session serialization does not extend to the
terminal. On a single-owner host this costs nothing: one person driving one surface at a time
never produces the concurrency those locks exist to arbitrate. What is left unserialized has
widened, though, because the terminal is no longer launch-only: it gracefully stops, cleans up,
force stops, and toggles Remote Control on sessions it never started, and each of those writes
changes an existing record rather than creating a fresh identity. If two people used the bot and
the terminal at the same moment, only SQLite would be arbitrating between them, and a writer that
cannot take the file lock within one second fails rather than waiting,
which surfaces as a reported error rather than a damaged record. Do not hand the bot to a second
person while working in the terminal, and do not drive one session from both surfaces at the same
moment.

Each process also holds its own catalogue and its own profile probe, both taken when it starts. A
project created in the terminal is invisible to a running service until it re-reads — opening
Launch or Resume re-reads the catalogue, as does `/launch`, so no Refresh press is needed for
this — and one created from Telegram or the command line is invisible in
a running terminal until it re-reads the catalogue — press Ctrl+R on the project list, the
resume project list, or use Add Project, which re-reads on the way out. Ctrl+R re-reads only
what the screen it is pressed on shows, and the catalogue is what those two show; on the
sessions view it re-runs the readiness pass and the session list instead. No screen's Refresh
re-probes the agents: one installed after the terminal started stays reported as unavailable
there until the terminal is restarted, and the
service's profile list is a snapshot of its own start in the same way. When the two surfaces
disagree about which projects or agents exist, neither is wrong; refresh or restart the older
process, and treat `doctor --profiles`, which probes when it is run, as the current answer.

## Local recovery without Telegram

When the service is down, its credential has been revoked, or Telegram is unreachable, every
post-launch action still exists on this host. `remote-agents tui` reads the same private
configuration and the same session database as `serve`, and it is the one of the two that does
not require the Telegram environment file, so it starts where `serve` would refuse to:

```bash
uv run --locked remote-agents tui
```

1. Press Ctrl+S, which is available from any screen. Sessions lists every managed session the
   shared store holds, including ones the bot launched and ones an earlier terminal run started.
   ENDED records are filtered because nothing is left to reach or stop. Readiness is refreshed
   once as the list opens, so a launch recorded as FAILED whose pane has since become ready is
   listed as RUNNING rather than sending an operator to repair something that already works.
   Escape returns to the project list.
2. Select a row for its detail: the session's identity, its state, and one line explaining what
   that state means. The record is re-read from the store every time detail opens, because the
   store has a second writer and the session may have been stopped elsewhere while the list was
   on screen.
3. Copy attach prints the command that reaches the pane, or states that there is none because the
   pane is not live or the pane found for that session belongs to a different project or agent.
   Inspect output renders the session's captured output, sanitized and bounded, in a scrollable
   pane; escape returns to detail. Output containing NUL is refused rather than printed, because
   a pane emitting it is not rendering text.
4. The stops offered are exactly the ones the shared policy allows from the session's current
   state: graceful only from RUNNING, cleanup only from PRESERVED, and force from RUNNING,
   STOP_REQUESTED, PRESERVED, or FAILED. Both surfaces label them from one map beside that
   policy — "Stop and close", "Clean up", "Force stop" — so an action is named the same wherever
   it is offered. Stop and close is the whole stop: once the pane exits it is cleaned up in the
   same action and the session reaches ENDED, so its output is not left to read. Clean up is
   therefore the answer to a pane that died on its own, which reconciliation preserves for
   inspection until the owner closes it. Claude Remote Control is offered only for a RUNNING
   Claude session. The record is read again and the policy re-checked at the moment the action is
   issued, so an action that has become illegal since the list was drawn is explained rather than
   attempted.
5. Force stop is confirmed a second time, on a screen of its own that names the session and
   states that the kill is immediate, cannot be undone, and loses whatever the agent has not
   saved. Cancel is the first entry and the highlighted one, so a stray or repeated enter aborts;
   confirming means moving to the other row on purpose. No screen rests the cursor on an entry
   that mutates, and a stop that fails leaves the cursor on Back rather than on the button
   that just failed, so a second enter is never a blind retry of a kill.
6. ORPHANED is two situations, and what is offered depends on which. It never meant the pane is
   gone — that ends the session. It means the record and this host's panes could not be
   reconciled, and reconciliation knows two ways that happens, so the record now remembers which
   applied.
   - **A running agent was found with no record of it**, and was taken back into the list. This
     is usually a live agent the database lost. It offers **Force stop**, which is the action its
     pane actually supports, and the screen says so in as many words. The lifecycle permits
     exactly this one transition out of ORPHANED and nothing else — there is no way to simply
     dismiss the row, because clearing it without acting would hide a working agent rather than
     stop it.
   - **The pane was found but was neither live nor preserved.** The evidence supports no action,
     so none is offered. Every record written before the provenance column existed reads this
     way too, because how it got there cannot be worked out after the fact.

   Either way, inspect it with `tmux -L remote-agents list-panes -a` and resolve it at the tmux
   level, only after its ownership and output have been established. Forcing the first kind kills
   a pane this tool never properly owned — that is intended, and it is still a kill.

Ctrl+O opens Resume, which starts a new managed session continuing a saved conversation, and it
is offered only for profiles that report themselves resume-capable on this host. When a session
cannot be salvaged, force stopping it and resuming its conversation into a fresh session is the
local recovery route that keeps the prior work.

A stop issued from the terminal reaches a session the service launched, and one issued from
Telegram reaches a session the terminal launched, because the profile a graceful stop needs is
resolved from the curated launch factories rather than from the launching process's memory of it.
Those factories hold only the profiles this host reports available. If no factory is curated here
for that session's profile, the graceful stop fails closed: nothing is sent to the pane, and the
session is not recorded as stopped, rather than claiming a stop that never happened.

Both surfaces now say so, and say which of two things went wrong, because the two have nothing
to do with each other. "The stop was never sent" is this case — no profile could be resolved on
this host, so no exit sequence was signalled, and `doctor --profiles` is where the answer is.
"The agent did not exit in time" is the other one — the sequence was sent and no clean exit was
seen before the wait ran out, which is about the agent rather than this host's configuration. The
wording is deliberately "no clean exit was seen" rather than "the agent was still running": a pane
destroyed mid-wait lands here too, and claiming it is still running would be more than the
observation supports. Until that distinction was added, the detail simply re-rendered the session as
RUNNING and the bot asserted the timeout wording for both, so this paragraph was the only place
an operator could learn the first case existed. It is no longer load-bearing in that way, which
is the point; check `doctor --profiles` when the surface tells you the stop was never sent.
Force and cleanup resolve no profile, because they remove the managed tmux session itself, so
force remains available when a graceful stop cannot be resolved.

The two-writer caveat above still applies, and it now governs stops as well as launches.
`SessionLocks` serializes the mutations issued through `SessionService`, and only inside the
process holding them — reconciliation's own writes do not take it at all — so a stop from the
terminal and a stop from the service are not serialized against each other; across the two
processes the only arbitration is SQLite's one-second busy timeout and the
durable idempotency keys. That is sound for a single owner on one host and would not be for
concurrent operators. Drive a given session from one surface at a time, and let the other
surface's next list read the result.

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
registry already holds is refused, including one recorded by a disabled entry. Ctrl+N in
`remote-agents tui` runs the same use case and the same append-only write from the terminal,
under the same area eligibility and the same slug rule.

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

`remote-agents tui` keeps working while the service is disabled, because it needs neither the unit
nor the Telegram credentials, so a curated launch and the local control plane described under
[local recovery without Telegram](#local-recovery-without-telegram) both remain possible during a
rollback. It attaches only to the session it has just started and never adopts an existing one;
reach a pane that outlived its launch with `tmux -L remote-agents attach-session -t ra-<session>:`.

Before changing bot profile metadata, retain a private rollback snapshot of the avatar,
descriptions, owner-scoped commands, and menu behavior. Restore that snapshot through the
Telegram profile controls if a rollback is required; do not replace the credential as part of a
usability rollback.

For a damaged session database, follow [database recovery](database-recovery.md). The restore
command refuses to replace a healthy database and preserves corrupt evidence.
