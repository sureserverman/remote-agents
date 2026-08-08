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
Preserved counts, then Launch and Sessions, and closes with Refresh. `/launch`, `/sessions`, and
`/help` offer the same owner-only entry points from Telegram's command menu.

1. Open Launch and use Search to find a project by name. The reply prompt names the expected
   input; use Cancel or Back to leave it, and retry an empty or unmatched search.
2. Select an agent, add an optional label or Skip it, and confirm that Review names the project,
   agent, and label before Launch. Back restores the preceding choice; Cancel makes no mutation.
3. Launch two sessions for the same project/profile, applying an optional label to one. Their
   project, agent, mode, and sequence remain distinguishable.
4. Open Sessions, inspect one active session, and verify that inline output is bounded, escaped,
   and clean, and that Back returns to that session with its actions intact. For oversized
   output, verify the separate UTF-8 `session-output.txt` attachment and that Telegram refuses
   to forward it.
5. Stop and close one session. Verify the screen shows what it is waiting for while the agent
   exits, then names the session, says its output is gone, and that it reaches ENDED in that
   single action with its pane gone from `tmux -L remote-agents list-panes -a`.
6. For a separate active session, use Force stop and verify the confirmation names the session
   and offers Cancel before the kill. Cancel it once, then confirm it.
7. With more sessions than one page holds, page through Sessions with Previous and Next, and
   verify Refresh redraws the page you are on rather than returning Home.
8. Restart `remote-agents.service`, press an expired pre-restart button, and verify that it
   alerts that the view expired and replaces it with a fresh Home. Verify the remaining managed
   session has the same identity.
9. Use `tmux -L remote-agents list-panes -a` only for local read-only confirmation. Never use
   the default tmux server for this service.
10. For a live managed Claude session only, open Details and confirm Enable or Disable Remote
    Control. If its state is unknown, do not retry remotely; inspect and recover locally. Never
    share the resulting Remote Control URL or a pane capture outside the owner workflow.
11. Open Resume for a project with prior Claude or Codex work. Its buttons show a bounded
    provider title or resume description when supplied, never a provider ID. The bot does not
    scan, control, or adopt arbitrary local agent processes; use only provider-catalogued
    conversations to create a new managed session.
12. Open Add Project. The area buttons must name only eligible existing directories under the
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
generated deliberately and looked at once. Three things are pinned to keep the captures
reproducible — the terminal size (an environment dependency), and the age column and the input
cursor's blink timer (both wall-clock ones, and so the two that would flake on a merely busy
machine). See the test module's docstring for the full rationale rather than duplicating it
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
   Escape is Back, Ctrl+R re-reads the catalogue, and Ctrl+Q quits.

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
project created in the terminal is invisible to a running service until it re-reads — press
Refresh in any paginated view — and one created from Telegram or the command line is invisible in
a running terminal until Ctrl+R. Ctrl+R re-reads the catalogue only: an agent installed after the
terminal started stays reported as unavailable there until the terminal is restarted, and the
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
   confirming means moving to the other row on purpose. No screen rests the cursor on a
   destructive entry, and a stop that fails leaves the cursor on Back rather than on the button
   that just failed, so a second enter is never a blind retry of a kill.
6. ORPHANED offers no stop at all. It does not mean the pane is gone — that ends the session — it
   means the record and this host's panes could not be reconciled, so the session is quarantined
   for local attention and the lifecycle permits no transition out of it. Inspect it with
   `tmux -L remote-agents list-panes -a` and resolve it at the tmux level, only after its
   ownership and output have been established.

Ctrl+O opens Resume, which starts a new managed session continuing a saved conversation, and it
is offered only for profiles that report themselves resume-capable on this host. When a session
cannot be salvaged, force stopping it and resuming its conversation into a fresh session is the
local recovery route that keeps the prior work.

A stop issued from the terminal reaches a session the service launched, and one issued from
Telegram reaches a session the terminal launched, because the profile a graceful stop needs is
resolved from the curated launch factories rather than from the launching process's memory of it.
Those factories hold only the profiles this host reports available. If no factory is curated here
for that session's profile, the graceful stop fails closed: nothing is sent to the pane, and the
session is not recorded as stopped — detail still shows it RUNNING rather than claiming a stop
that never happened. Check `doctor --profiles` before concluding that a session refuses to stop.
Force and cleanup resolve no profile, because they remove the managed tmux session itself, so
force remains available when a graceful stop cannot be resolved.

The two-writer caveat above still applies, and it now governs destructive actions rather than
launches alone. `SessionLocks` serializes per-session mutations only inside the process holding
them, so a stop from the terminal and a stop from the service are not serialized against each
other; across the two processes the only arbitration is SQLite's one-second busy timeout and the
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
