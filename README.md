# Remote Agents

Remote Agents will be a private, single-owner Telegram control plane for curated agent
sessions running in isolated tmux servers on this host.

Its approved scope is limited to project browsing, project creation, and managed session
lifecycle actions: launch, selected resume, list, inspect, Copy Attach, graceful stop,
cleanup, confirmed force stop, and confirmed Claude Remote Control state changes. It does not
provide remote shell access, prompt relay, raw agent arguments, arbitrary keystrokes, or
arbitrary paths. Its only approved registry mutation is the append-only add-project entry
described below; no existing registry entry is ever edited or removed.

## Development

```bash
uv sync --locked
uv run --locked pytest -q
```

## Production operation

Keep both runtime files outside the repository and owner-readable only:

```bash
install -d -m 700 ~/.config/remote-agents ~/.local/state/remote-agents
install -m 600 config/remote-agents.example.toml ~/.config/remote-agents/config.toml
${EDITOR:-vi} ~/.config/remote-agents/telegram.env
```

`telegram.env` contains these three values and nothing else:

```text
REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=replace-me
REMOTE_AGENTS_OWNER_USER_ID=replace-me
REMOTE_AGENTS_OWNER_CHAT_ID=replace-me
```

Install and start the user service:

```bash
install -m 600 systemd/remote-agents.service ~/.config/systemd/user/remote-agents.service
systemctl --user daemon-reload
systemctl --user enable --now remote-agents.service
systemctl --user is-active remote-agents.service
uv run --locked remote-agents doctor --json | python -m json.tool
```

The configured owner sees only `/start`, `/launch`, `/sessions`, and `/help` in Telegram's
command menu. `/start` opens a compact Home dashboard with active and preserved counts;
`/launch` opens the paginated project list and `/sessions` opens the paginated list of current
managed sessions. `/help` names the actions this deployment actually offers. Search, renaming, and
project creation use Telegram reply prompts: send `Skip`, `Cancel`, or `Back` instead of
leaving an input step stranded. Choosing an agent launches the session immediately — there is no
review step and no label to supply first — and a session is named afterwards, or never, with
`Rename` on its own detail screen. Ended records remain in local SQLite history but do not clutter the Telegram list.
Ending a session returns to the session list with the outcome as its lead line, rather than to a
screen of its own — so the next thing the owner wants is already on screen. Both project pickers,
and search results, put recently-used projects first, weighted so that recent launches outrank a
larger burst from long ago.

The chat holds one bot message. Every screen is that message being re-rendered, a command is
answered by redrawing it and deleting the command itself, and a reply prompt's input box is a
second message that goes away once it is answered or abandoned. A button does not expire: its
token is stored in SQLite and is valid for the message it was drawn on rather than for a clock,
so one drawn before a service restart still works after it. Replacing a screen prunes the tokens
it drew, so a press that lands after a redraw says the screen has moved on and shows Home.

Every screen closes with the navigation it is entitled to: `Back` to the screen that owns it,
`Refresh` on the two views whose answer goes stale on its own — Home's counts and the sessions
list — and `Home`. An action that makes you wait, such as a launch or a stop that polls a pane,
replaces the screen with what it is waiting for and drops the keyboard until it finishes, so a
press cannot be repeated into a second launch.

Inspect shows safely escaped terminal text inline when it fits, over a `Back` to the session it
came from. Oversized output is sent as an unforwardable UTF-8 `session-output.txt` attachment; it
is read-only captured output, never an input channel.

For safe stop behavior, choose Stop and close: the agent exits on its own terms and its pane is
removed in the same action, so the session ends in one step and its output is not kept. Clean up
remains for a session whose pane died on its own, which is preserved for inspection until you
close it. Force stop names the session and what will be lost, offers Cancel first, and is for a
live session that cannot exit gracefully. Each of them reports what the session actually did, and
a graceful stop that did not take effect says which of two unrelated things went wrong: the stop
was never sent, because no agent profile could be resolved on this host, or no clean exit was
seen before the wait ran out. One is fixed with `doctor --profiles`, the other is waited out or
forced, and both surfaces use the same words for them. The bot never relays arbitrary commands,
agent text, shell access, or approval responses.

Resume uses a server-resolved catalogue selection. It may show a bounded provider-generated title
or provider resume description (Claude's stored last prompt and Codex's thread preview when no
title is available); provider IDs and transcript output remain server-side. The bot does not scan,
identify, terminate, or adopt arbitrary local agent processes. Only provider-catalogued
conversations can be resumed into a new managed tmux pane.
Copy Attach is offered only for a currently trusted live managed pane. Claude Remote Control is
available only on a live managed Claude pane, requires a second confirmation, and uses the single
qualified enable/disable interaction; it never carries a prompt, transcript, or session URL.

The service also speaks first when a managed agent stops working: it has
finished, it hit a usage limit, one reply hit its output length limit, it is waiting for an answer,
or — for the profiles with no hook system — its pane has produced no output since a stated time,
which is said as the guess it is. It speaks only about a session that is still live, and only when
there is something to do about it: an agent reporting after the owner has already stopped its
session is telling them their own action back. **One session gets one message per pass**, listing
everything it has said since the last one, up to what one message can hold, and saying anything
repeated only once, beside the live view, with one button that opens the session it names. A
managed Claude session reports this itself through a global Claude Code hook, installed once with
`remote-agents install-agent-hooks` and removed with `--remove`. The
hook fires in every Claude session on the host — it starts a short-lived Python process each time —
but it writes nothing and exits 0 unless the environment carries the session identifier this
service injects into the panes it launches. Descendants of a managed pane inherit that identifier,
so a `claude` started from inside one is the exception and spools under its parent's session.

See [the operator runbook](docs/operator-runbook.md) for acceptance, recovery, and rollback, and
[agent activity notifications](docs/operator-runbook.md#agent-activity-notifications) for
installing, verifying and removing the hooks.
Do not put secrets in this repository.

## Local terminal surface

The same curated launches are available on this host without Telegram:

```bash
uv run --locked remote-agents tui
```

`remote-agents tui` carries the same session actions the bot carries, driven from this host instead
of from Telegram, and one the bot has no way to offer: it hands this terminal to a session's tmux
pane. The traffic is not all one way — the bot can rename a running session and the local surface
cannot, though it can name one at launch, which the bot no longer does. It reads the same private configuration the service reads, defaulting to
`~/.config/remote-agents/config.toml`, and it opens the same SQLite store, refusing a
`database_path` outside the private state directory exactly as `serve` does. It drives that store
itself, so none of what follows needs Telegram credentials or a running user service: launch,
resume, the session list, inspect, Copy Attach, all three stops, and Claude Remote Control are
available with the service stopped.

The wizard opens on the project list with the filter focused and reports how many projects are
available. Type to narrow the list one character at a time, press enter to move into it, then use
the arrows and enter to choose; registered projects are listed before unregistered ones and each
row names its group. The agent list names every curated profile and shows the blocking reason
beside one that cannot be launched here; choosing that one is refused rather than attempted. A
label is optional, bounded by the configured `max_label_length`, and an empty entry skips it.
Review names the project, agent, and label before anything is created, and it opens with Back
highlighted rather than Launch, so a stray enter mutates nothing; Back restores the agent choice
and Cancel returns to the project list. Escape is Back, Ctrl+R re-reads whatever the screen
you are on shows without leaving it, Ctrl+N adds a project, Ctrl+S opens the managed sessions,
Ctrl+O resumes a saved conversation, and Ctrl+Q quits.

The footer lists only the keys that do something where you are. Refresh appears only where
something can be re-read, Back is absent at the project list because there is nowhere behind
it, and Resume is absent entirely on a host that wired no conversation service. While a flow
holds work you would lose — a label or a project name being typed, or a review step holding
everything gathered so far — the three keys that leave the flow are greyed rather than
hidden, so a keystroke meant for somewhere else does not discard it. Ctrl+Q is deliberately
not among them: quit means leave, and an app that refuses to close until an entry is cleared
would be the worse answer. It does take unsaved work with it.

The surface has three places to say something and each one says a different kind of thing. The
header carries a breadcrumb — `Projects › infra/existing › claude › Review` — which is where
you are and what you chose to get there. Below it is a single line of status: what to do here,
or the result you still need, such as the attach command for a session that did not come up.
It is exactly one line high, so the list beneath it never moves as a message changes. Anything
that did not happen — a stop that raised, an agent that cannot be launched, a project the
catalogue no longer has — is a notification in the corner instead, because it is about the
action you just took rather than about the position you are standing on, which outlives it.

A launch that raises, or one whose session never reaches readiness, returns to Review, reports
the reason, and attaches to nothing. Where the session's pane may still exist, the attach
command that reaches it stays on the status line rather than expiring with the notification.

Add Project is Ctrl+N. The area is a choice between the existing directories the server enumerates
under the configured development root, further restricted to those the project identity rule also
accepts; a free-form area is never accepted. The name is typed and validated before anything is
created, and Review names the area and the name before the mutation. After a create the catalogue
is re-read, so the new project is selectable without leaving the app.

After a ready launch this process is replaced by the attach command for the session it just
started, `tmux -L remote-agents attach-session -t ra-<session>:`, and the store connection is
closed first, so the attached terminal holds no database handle. The project ships no tmux
configuration and sets no prefix, so detaching uses tmux's own binding: `Ctrl-b d` on a stock
tmux, or the same `d` under whatever prefix this host's `~/.tmux.conf` sets. Detaching leaves the
session running and managed; it stays listed, inspectable, and stoppable from either surface.
Started from inside an existing tmux client, the launch still happens but the attach is refused
rather than nested, and the command to reach the new session is printed instead. An exec that
cannot happen prints the same command and exits non-zero, so a started session is never lost.

Ctrl+S lists the managed sessions. The list is the shared store's rather than this process's, so a
session the bot launched, or one a previous run of this app started, is there too; each row names
the session, its state, and how long ago it started. Readiness is refreshed once as the list opens,
for the reason the bot refreshes it: a launch that failed here may have become ready since, and a
stale FAILED row sends the owner to fix something that already works. Ended sessions are left out,
because the record is kept for audit but there is nothing left to reach.

Selecting a row opens that session's detail, re-read from the store rather than carried over from
the list, because the store has a second writer and a session can be stopped elsewhere while the
list is on screen. The detail names the session and its state, and explains in one line what that
state means. It offers exactly the stops the shared policy allows from that state: Stop and close
only from RUNNING (which ends the session outright, cleaning up the pane it exited), Clean up only
from PRESERVED — now reached only by a pane that died on its own — and Force stop from RUNNING,
STOP_REQUESTED, PRESERVED, or FAILED. A starting session offers none, because the domain has no
stop transition out of STARTING and reconciliation is what resolves one that is stuck. An orphaned
session depends on how it got there: one that reconciliation adopted — a running agent found with
no record of it — offers Force stop and nothing else, while one whose pane evidence was merely
ambiguous offers none, as does any record predating the column that stores the difference.

Both surfaces spell those actions the same way, from one map beside the policy that decides which
of them to offer. The stops share a single row under the read-only actions, which each get a row
of their own: Telegram has no separator, so shape is the only thing distinguishing an action that
ends a session from one that reads it.

Copy attach is always offered and answers when it is chosen: a pane that is not live, or one whose
project or agent does not match, is explained rather than left out, so a dead pane cannot be
mistaken for a surface that forgot to draw the entry. Inspect output renders the captured text
through the same sanitizer the bot uses, in a scrollable pane rather than under Telegram's message
bound, and refuses output containing a NUL byte for the reason the bot refuses it: a pane emitting
NUL is not rendering text, and printing it can corrupt the terminal. Claude Remote Control appears
only on a running Claude pane. It and Force stop each move to a step of their own before anything
is issued, with Cancel first and resting under the cursor, so going through with either means
choosing a different row on purpose rather than repeating the keystroke that opened the detail.

Ctrl+O resumes a saved conversation. It asks for the project, then the agent, offering only those
whose provider reports itself resume-capable on this host; capability comes from the probe that
asks each provider, never from a version allowlist. Then it pages that agent's conversations for
that project, ten at a time. A row carries safe metadata only; the provider ID and the transcript
stay server-side exactly as they do in Telegram, and what the row holds is an opaque reference the
server resolves, so a stale one resolves to nothing rather than to a path. The confirmation opens
with Cancel under the cursor, so a stray enter starts nothing. A ready resume hands this terminal
to the new session's pane exactly as a launch does; one that never reaches readiness prints the
command that reaches the pane instead. Resume is Ctrl+O rather than Ctrl+E because the text input
already binds Ctrl+E to end-of-line.

## Creating a project

A project can be created from this host, with the command below or with Ctrl+N in the local
terminal surface, or from Telegram. Every surface runs the same validated use case and the same
append-only registry write:

```bash
uv run --locked remote-agents add-project --area infra --name new-thing
```

In Telegram, Add Project offers the area as a choice between the existing directories the server
enumerates under the configured development root; a free-form area is never accepted. The project
name is entered through a reply prompt and is validated before anything is created or written, and
Review names the area and the name before the mutation happens. Cancel returns Home without a
mutation.

Area and name must each be lowercase letters, digits, and single hyphens, 1 to 64 characters. The
project is created at exactly one area directory below the configured `dev_root`, so no other
location can be targeted, and an existing directory is never replaced.

The registry write appends one `{path, name, area, enabled, added}` entry and nothing else. The
existing bytes of the registry named by `registry_path` are kept as an exact prefix rather than
rewritten in bulk, which is what the portfolio tooling's drift detection expects of any writer.
The write is serialized by an exclusive lock that covers cooperating writers only, so a concurrent
hand edit is not protected. The extended document is re-parsed before it is published, and
publication is atomic; a registry that does not already read cleanly is not extended, nor is one
whose shape a block append cannot extend, such as an empty flow-style `projects: []` list, and a
symlinked registry is written through rather than replaced. A canonical path the registry already
holds is refused, including one held by a disabled entry. If the registry write fails, the created
directory is removed, so the registry never holds an entry for a directory that is not there. A
removal that itself fails is reported rather than hidden, leaving an unregistered directory behind.

A project created from Telegram is selectable there immediately, because the bot re-reads the
catalogue after the mutation. One created with the command line or the local terminal surface
lands in a separate process, so a running bot does not see it until it re-reads: press Refresh in
any paginated view, which returns Home, then open Launch again. No registry field
outside that closed schema is written, and neither surface can edit or remove an entry that
already exists.

## Curated profiles and recovery

This host enables only five fixed interactive profiles. Their installed versions are
operator-managed and reported for diagnosis, not used as a launch gate. Check the current
machine's non-secret profile record with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The command reports `AVAILABLE` when the executable is present. A version probe is informative
only: local updates remain launchable and each launch must still reach its profile's readiness
state. A missing executable is `BLOCKED`; one blocked profile does not affect the others.

The full doctor command reads only the private credential-file boundary, not its values. It
reports the core registry, SQLite store, fixed tmux command, active user service, Telegram
credential-file boundary, and every profile together; `healthy` is true only when all are ready.

See [the compatibility matrix and dedicated-socket recovery commands](docs/profile-compatibility.md).
