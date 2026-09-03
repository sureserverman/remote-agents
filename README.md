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

See [the architecture document](docs/architecture.md) for how the code behind both surfaces is
arranged: the layers, the dependency rule `tests/architecture/check_imports.py` enforces, and the
process model the terminal and the service share.

## Installing

`remote-agents` is distributed as a **pinned tag installed with `uv`** (DEC-057), on Ubuntu and
on macOS alike. Nothing here is packaged for a system package manager, and nothing runs as root.

The bootstrap needs three things it deliberately does not install: `curl` — implied, it is how
you fetch the script — `git`, which `uv` shells out to for a `git+https://` source, and `tmux`,
which onboarding requires. It does not answer `--yes` on your behalf, so a missing system
dependency stops it with the exact `apt-get` or `brew` command to run rather than escalating
privileges for you. On a bare image install `tmux` in the provisioning step before this one, or
an unattended run ends at exit 1 with nothing registered.

```bash
curl -fsSL https://raw.githubusercontent.com/sureserverman/remote-agents/main/scripts/install.sh \
  | bash
```

That fetches `uv` if the host has none — verifying a SHA-256 pinned against a *versioned*
installer URL before executing a byte of it, and using an already-present `uv` as-is — installs
this tool from the pinned tag, and then hands off to `remote-agents onboard --install-daemon`.
The one line therefore ends at a **running service**, not at an installed executable.

To pass the script an option you need bash's `-s --`. A piped `bash --no-onboard` is bash's own
option, and fails before the script runs at all:

```bash
curl -fsSL https://raw.githubusercontent.com/sureserverman/remote-agents/main/scripts/install.sh \
  | bash -s -- --no-onboard
```

### Installing without piping a fetched script into a shell

The bootstrap's own install step, run yourself. It is the same pinned tag; the script exists to
find `uv`, verify it, and sequence what follows, not to install anything different:

```bash
uv tool install --managed-python \
  "remote-agents @ git+https://github.com/sureserverman/remote-agents@v0.33.0"
remote-agents onboard --install-daemon
```

`uv tool install` puts the console script in `uv tool dir --bin` — usually `~/.local/bin`, which
a fresh login shell need not have on `PATH` and which is absent from macOS's default path
outright. `uv tool update-shell` fixes that. To check the executable resolves, run
`remote-agents --help`: there is no `--version` flag, and asking for one exits non-zero.

### Installing unattended

A piped run has no terminal to be asked at — `curl | bash` gives the script's own text to
stdin — so onboarding sees a non-tty and refuses to prompt. Supply the three Telegram values
through the environment; onboarding names them precisely if you run it once interactively:

```bash
REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=… REMOTE_AGENTS_OWNER_USER_ID=… \
  REMOTE_AGENTS_OWNER_CHAT_ID=… remote-agents onboard --install-daemon --yes
```

**The bot token is never a command-line argument.** On Linux `/proc/<pid>/cmdline` is
world-readable and argv lands in shell history, so there is no flag that takes the value: it
comes from the environment, or `--bot-token-file` names a path to read it from. Save the
installer and run it in a terminal instead, and onboarding prompts for all three.

### Upgrading

Re-run the bootstrap at a newer tag, then re-run onboarding:

```bash
curl -fsSL https://raw.githubusercontent.com/sureserverman/remote-agents/main/scripts/install.sh \
  | bash
remote-agents onboard --install-daemon
```

Or, from an install that already exists, in one command:

```bash
remote-agents upgrade            # --check to look without taking it
```

It finds the newest release tag, installs it, and re-registers the daemon so the running service
picks up the new code. `--version vX.Y.Z` names a tag explicitly, which is also how you roll back
off a bad release.

Asking `uv` to upgrade the tool is **not** the path, and this is the reason `remote-agents
upgrade` exists. `uv tool upgrade` re-resolves the requirement the tool was installed with; that
requirement is an exact git rev, so it resolves to itself and reports `Nothing to upgrade` having
done nothing — correct behaviour, exit 0, and indistinguishable from being up to date. The pin is
deliberate and stays: an install that moved whenever the default branch moved would be a
credential-holding daemon changing under a host with live agent sessions on it. What the pin cost
was a working upgrade verb, and that is supplied rather than the pin being given up.

`doctor` reports the same comparison passively, under `release`, so falling behind is something
you learn from the command you already run rather than by accident. It never affects `healthy`:
being a release behind is a diagnostic, not ill health — the rule DEC-002 already sets for the
agent CLIs' own versions.

### Uninstalling

**In this order — the daemon first, or nothing is left that can take it away:**

```bash
remote-agents onboard --remove      # unregister the daemon and delete what it installed
uv tool uninstall remote-agents     # then take the tool itself away
```

`uv tool uninstall` deletes the console script, so an operator who removes the tool first has
nothing left to run the uninstaller with, while the daemon stays registered naming an ExecStart
that no longer exists — which under `Restart=on-failure` is a service that keeps trying rather
than one that is gone. If that has already happened, re-run the bootstrap and then do it in the
order above.

`remote-agents onboard --remove` unregisters the daemon and deletes everything the install
caused to exist — the unit or the LaunchAgent, the `default.target.wants` symlink
`systemctl enable` writes, and on macOS the two log files launchd opens on the job's behalf.
Your config and your credential file are left alone. To see where the definition lives, ask:
`remote-agents onboard --print-daemon-path`.

## Production operation

Onboarding is where the host is actually configured, and the bootstrap ends by running it. Run
it again yourself at any time, on Ubuntu and on macOS alike:

```bash
remote-agents onboard --install-daemon
```

It probes the system dependencies and prints the exact command to install anything
missing — running that command only after you confirm it. It creates `~/dev`, the
projects tree the generated config names (`--dev-root` if yours is elsewhere), since
the config will not load without it. It generates
`~/.config/remote-agents/config.toml` from your own home rather than copying
`config/remote-agents.example.toml`, whose paths exist on no machine but the one it
was written on. It asks for the three Telegram values and writes them to
`~/.config/remote-agents/telegram.env` at mode 0600. And it writes and registers the
daemon your platform actually uses: a systemd user unit on Linux, a LaunchAgent on
macOS. Re-running it is safe — it keeps a config or a credential file you already
have, and rewrites the daemon only if the definition changed.

Its exit status answers for onboarding's own work, not for the whole host: a zero means the
config, the credentials and the daemon are as it left them, and `doctor` is what asks whether
everything else on this machine is happy (DEC-058).

One side effect of re-running is worth knowing: `--install-daemon` means "register
and start", so if the service is down it will be brought up, including when you
stopped it yourself. There is no way to ask a supervisor "is this registered?"
without either running the service or parsing output macOS documents as not being an
interface, so a stopped service and an absent one look the same from here. Stop it
again after onboarding, or use `--remove`.

**On macOS the service runs only while you are logged in at the screen.** The
LaunchAgent targets `gui/<uid>`, whose domain exists only after a console login — so a
Mac sitting at the login window after a reboot is one where this service is
legitimately absent rather than broken. That is an owner decision, recorded as DEC-054.

`systemd/remote-agents.service` is still in the repository as the behavioural reference
the generated unit is pinned against by
`tests/contract/supervisor/test_systemd_supervisor.py`. Do not install it by hand: it
carries a `%h` specifier and a hardcoded checkout path that the generated unit
deliberately does not, and a host running it is running a definition no version of this
tool would produce.

Check the result at any time:

```bash
remote-agents doctor --json | python -m json.tool
```

The configured owner sees `/launch`, `/resume`, `/sessions`, and `/help` in Telegram's command
menu, which names the same places the navigation bar does. `/start` stays registered because
Telegram requires it of every bot, and lands where `/sessions` lands: the paginated list of
current managed sessions, whose heading carries the total, active, and preserved counts.
`/launch` opens the paginated project list and `/resume` its resume counterpart.
There is no Home screen — every screen closes with a fixed `Sessions · Launch · Resume` row,
so the three destinations are one press away from wherever you are rather than one press away from a
dashboard in front of them. The row marks the flow you are standing in, and carries no
`Resume` at all on a host that wired no conversation service; `/resume` stays in the menu
there, because Telegram sets that menu once for the chat rather than per screen, and answers
that resuming is unavailable. `/help` names the actions this deployment actually offers. Search, renaming, and
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
it drew, so a press that lands after a redraw says the screen has moved on and shows the
sessions list.

Every screen closes with the navigation it is entitled to: `Back` to the screen that owns it,
on its own row, above the fixed `Sessions · Launch · Resume` bar. A screen reachable from
everywhere has no parent, so both project pickers carry no `Back` at all. There is no
`Refresh`: every screen re-derives what it shows on entry, so the view whose answer goes
stale on its own — the sessions list, and the counts in its heading — is current whenever you
arrive at it, and `Back` out of a session returns to the page of the list it was opened
from. An action that makes you wait, such as a launch or a stop that polls a pane,
replaces the screen with what it is waiting for and drops the keyboard until it finishes, so a
press cannot be repeated into a second launch.

A session's detail also reports what its agent has spent: the context window it is currently
carrying. **The rate-limit windows are not there**, and deliberately — they belong to the whole
agent rather than to any one session, so they are reported once per agent instead: under the
counts on the Sessions list in Telegram, and in the local terminal's own Plan limits pane. Both are read from the working
files the provider itself writes — nothing is asked of the agent, and nothing leaves the host.
The providers publish very different amounts, and the screen says which case it is in rather
than filling a gap: codex reports its context against a stated window and both its rate limits;
claude's transcript records what a turn used and never the ceiling it used it out of, so its
percentage is computed against a window you state as `limits.claude_context_window` and the line
says `declared` where codex's says nothing; cursor-agent reports neither and the row says so.
A rate-limit window belongs to the whole agent rather than to any session, so it is rendered
once per agent and never under a session, where the same figure read as that session's spend:
a block on the bot's Sessions screen, and a limits pane on the `tui` dashboard between the
sessions and notifications panes. The three-pane console does not carry that pane — its panes
are separate processes and the dashboard is not one of them. Claude's
rate limits are the one figure that is not the session's own — Claude Code hands them to a
status-line command and never writes them down, so they are read from the status-line cache when
one is fresh, and the line says where they came from. A rate-limit window whose reset has already
passed is dropped rather than shown, because the window it counted against has since reopened.

Every keyboard is widened to one floor, so screens do not alternate between a narrow box and a
full-width one as you move between them. The padding sits on the navigation bar and is invisible;
no button label changes.

Inspect shows safely escaped terminal text inline when it fits, over a `Back` to the session it
came from. Oversized output is sent as an unforwardable UTF-8 `session-output.txt` attachment; it
is read-only captured output, never an input channel.

For safe stop behavior, choose Stop and close: the agent exits on its own terms and its pane is
removed in the same action, so the session ends in one step and its output is not kept. Clean up
remains for a session whose pane died on its own, which is preserved for inspection until you
close it. Force stop names the session, says what will be lost, and explains the state it is in;
it offers the kill first with Cancel between that button and the navigation bar, so the row
nearest the habitual tap target is the harmless one. It is for a live session that cannot exit
gracefully. Each of them reports what the session actually did, and
a graceful stop that did not take effect says which of two unrelated things went wrong: the stop
was never sent, because no agent profile could be resolved on this host, or no clean exit was
seen before the wait ran out. One is fixed with `doctor --profiles`, the other is waited out or
forced, and both surfaces use the same words for them. The bot never relays arbitrary commands,
agent text, shell access, or approval responses.

Resume uses a server-resolved catalogue selection. It may show a bounded provider-generated title
or provider resume description (Claude's stored last prompt and Codex's thread preview when no
title is available); provider IDs and transcript output remain server-side. The bot does not scan,
identify, terminate, or adopt arbitrary local agent processes. Only provider-catalogued
conversations can be resumed into a new managed tmux pane. Choosing a conversation resumes it
on that press: the bot reviews neither a launch nor a resume, so nothing stands between the
choice and the session, and a second press of the same button is dropped rather than serviced
into a second session. The local terminal surface behaves the same way — it kept a resume
review for a while, and dropping it is what brought the two surfaces back into line. A
conversation
is attached to the session it starts and cannot be resumed again until that session has
**ended** — not merely stopped, so a preserved, failed, orphaned or stopping session still
holds it. Pressing it before then reports what it is attached to and what became of it, rather
than claiming a resume, and closing that session releases the conversation. The ended record keeps the
conversation it was resumed from, so the history still answers what was resumed.
Copy Attach is offered only for a currently trusted live managed pane. Claude Remote Control is
available only on a live managed Claude pane, offers the one direction its last observed state
leaves open — Enable for a session known inactive, Disable for one known active, and both only
while nothing has been observed for it — requires a second confirmation, and uses the single
qualified enable/disable interaction; it never carries a prompt, transcript, or session URL.

The service also speaks first when a managed agent stops working: it has
finished, it hit a usage limit, one reply hit its output length limit, or it is waiting for an
answer. Those four are the whole vocabulary. `opencode` and `cursor-agent` contribute none of
them — neither publishes a hook system, so nothing observes them at all — while `claude`,
`claude-remote` and `codex` each report for themselves. It speaks
only about a session that is still live, and only when
there is something to do about it: an agent reporting after the owner has already stopped its
session is telling them their own action back. **One session gets one message, not one per
report**: the first thing it says sends that message, and each later report sends a replacement
carrying the story so far and deletes the one it supersedes, up to what a message can hold and
saying anything repeated only once. A session that reports all night leaves one message in the
chat rather than ninety-six, and it is the newest thing there rather than buried where the
first report landed. Because a replacement notifies like any other message, the per-session
rate limit governs every send, so the taper still decides how often the phone is allowed to
buzz. The message stands beside the live view with one button that opens the session it names,
and it starts over from the next report only once it has left the chat — pressing `Open
session` deletes it. A
managed Claude session reports this itself through a global Claude Code hook, installed once with
`remote-agents install-agent-hooks` and removed with `--remove`. The
hook fires in every Claude session on the host — it starts a short-lived Python process each time —
but it writes nothing and exits 0 unless the environment carries the session identifier this
service injects into the panes it launches. Descendants of a managed pane inherit that identifier,
so a `claude` started from inside one is the exception and spools under its parent's session.
Codex can additionally install its own `Stop` and `PermissionRequest` hooks with
`remote-agents install-agent-hooks --provider codex`. Native code-mode escalations currently do
not call `PermissionRequest`; for those, the managed tmux pane's content-free `Action Required`
title produces one inferred `needs_answer` notification until it clears. Neither path exposes a
remote approval action or retains the command, prompt, path, or transcript. A completed Codex
turn does carry the agent's own last line, bounded as Claude's is; an approval carries no words at
all. Codex does not claim rate- or output-limit notifications.

See [the operator runbook](docs/operator-runbook.md) for acceptance, recovery, and rollback, and
[agent activity notifications](docs/operator-runbook.md#agent-activity-notifications) for
installing, verifying and removing the hooks.
Do not put secrets in this repository.

## Local terminal surface

The same curated launches are available on this host without Telegram. The front door is
the bare command:

```bash
uv run --locked remote-agents
```

With no arguments, `remote-agents` enters the **console**: a tmux session named
`ra-console` on the project's own server, whose single window is **three panes** — the
projects surface on the left at about 60% of the width, the running sessions top-right, and
the notifications feed under them. Each pane is its own process (`remote-agents pane
projects|sessions|feed`), because a terminal app owns a whole terminal and cannot span panes.
Run from inside the console it says so instead of nesting; run from inside somebody else's
tmux it prints the attach command instead.

Opening a session **exchanges** it into the left pane: the agent's own pane moves there and
the projects surface goes to live in that agent's window until it is swapped back, so the
sessions list and the feed stay on screen beside the agent you are working in. The whole
detail is one `d` away on the sessions pane, which stays visible the whole time, and each of
its actions also has a key of its own on the row — see *Keys on the sessions list* below.

`p` on the sessions pane swaps the projects surface back into the left slot, which is the
same exchange run backwards. It is a key inside our own process, so it works only while you
are focused on that pane; `F12` below does the same thing from anywhere, including from
inside a displayed agent.

**Killing the console while an agent is displayed destroys that agent's process**, because
its pane is physically in the console's window (DEC-040). With nothing displayed, killing the
console is safe and every managed session survives it.

If a pane's process dies, the console rebuilds exactly that one — including its own projects
surface — **on the next `remote-agents`**, not the moment it happens. Nothing watches the
panes; a pane that dies mid-session stays dead until you run the command again.

**Upgrading:** a console that was already running before this version keeps running whatever
it was. It is a tmux session, so it outlives the code that made it, and `remote-agents` will
attach to it rather than replace it. To get the three-pane console, kill it once —
`tmux -L remote-agents kill-session -t ra-console` with nothing displayed — and run
`remote-agents` again. Your managed sessions are not in that session and survive it.

### Keys the console takes

**One**, and it is worth knowing why. `F12` brings the projects surface back to the left
pane. It is a tmux *root* binding — no prefix — installed on this project's own tmux server
only, so your own tmux configuration is never touched, but on that server it is a key no
agent can ever receive. It earns that because the route back is the one thing that must not
require remembering configuration: an agent fills the pane you were working in, and that is
exactly when a console looks stuck.

Everything else uses tmux's own keys. **Moving between the three panes is `Ctrl-b o`** (or
the same `o` under whatever prefix this host's `~/.tmux.conf` sets) — the prefix reaches the
client before any key reaches a pane, so it works even while an agent is displayed. An
earlier design took a second root key for this; it was removed once that turned out to be
true.

Each pane offers only the flows it owns: the projects pane keeps *Add project* and *Resume*,
which both begin by choosing a project. The sessions and feed panes offer neither.

### Width

The agent's pane is permanently narrower than a whole window — about 60% of your terminal's
width. At 200 columns that is comfortable; at 100 it is tight, and worth knowing before you
size the terminal you keep the console in.

Every command with arguments is the CLI exactly as before — `serve`, `doctor`,
`add-project`, and the rest are unchanged.

The dashboard itself, in or out of the console, is:

```bash
uv run --locked remote-agents tui
```

`remote-agents tui` carries the same session actions the bot carries, driven from this host instead
of from Telegram, and one the bot has no way to offer: it hands this terminal to a session's tmux
pane. Naming works the same way on both now: a session is named after it exists, from its own
detail, and neither surface asks for a name at launch. It reads the same private configuration the service reads, defaulting to
`~/.config/remote-agents/config.toml`, and it opens the same SQLite store, refusing a
`database_path` outside the private state directory exactly as `serve` does. It drives that store
itself, so none of what follows needs Telegram credentials or a running user service: launch,
resume, the session list, inspect, Copy Attach, rename, all three stops, and Claude Remote
Control are available with the service stopped.

The dashboard rests on the project list with the filter focused and reports how many projects
are available. Type to narrow the list one character at a time, press enter to move into it,
then use the arrows and enter to choose; registered projects are listed before unregistered
ones and each row names its group. Choosing a project asks one question — launch a new session,
or reopen a saved conversation — with the cursor resting on Launch, and Resume offered only on
a host whose conversation service is wired. Launch opens the agent list, which names every
curated profile and shows the blocking reason beside one that cannot be launched here; choosing
a blocked one is refused rather than attempted. Choosing an agent launches it, with no name
asked for on the way and nothing asked afterwards. The agent list carries the project in its
breadcrumb and says what going through with it does: a ready launch hands this terminal to the
session's pane, or prints how to reach it. It opens with Back highlighted rather than an agent,
so a stray enter mutates nothing and reaching an agent is one arrow key — the same shape, and
the same cost, as choosing a conversation to resume. Escape is
Back, Ctrl+R re-reads whatever the screen
you are on shows without leaving it, Ctrl+N adds a project, Ctrl+S opens the managed sessions,
Ctrl+O resumes a saved conversation, and Ctrl+Q quits.

The footer lists only the keys that do something where you are. Refresh appears only where
something can be re-read, Back is absent at the project list because there is nowhere behind
it, and Resume is absent entirely on a host that wired no conversation service. While a flow
holds work you would lose — a project name or a session name being typed, or the add-project
review holding one already committed — the three keys that leave the flow are greyed rather
than hidden, so a keystroke meant for somewhere else does not discard it. The launch flow is
deliberately not among them: it holds one list choice and nothing typed, so re-picking costs
one keystroke and greying the keys would be friction with nothing behind it. Ctrl+Q is deliberately
not among them: quit means leave, and an app that refuses to close until an entry is cleared
would be the worse answer. It does take unsaved work with it.

The surface has three places to say something and each one says a different kind of thing. The
header carries a breadcrumb — `Projects › infra/existing` — which is where you are and what you
chose to get there. Below it is a single line of status: what to do here,
or the result you still need, such as the attach command for a session that did not come up.
It is exactly one *sentence*, and its region is a fixed height — two rows, or three on the
sessions positions, which carry a whole keymap there — so the list beneath it never moves as a
message changes. Anything
that did not happen — a stop that raised, an agent that cannot be launched, a project the
catalogue no longer has — is a notification in the corner instead, because it is about the
action you just took rather than about the position you are standing on, which outlives it.

A launch that raises, or one whose session never reaches readiness, leaves you on the agent
list with the cursor resting on nothing, reports the reason, and attaches to nothing. Where the session's pane may still exist, the attach
command that reaches it stays on the status line rather than expiring with the notification.

Add Project is Ctrl+N. The area is a choice between the existing directories the server enumerates
under the configured development root, further restricted to those the project identity rule also
accepts; a free-form area is never accepted. The name is typed and validated before anything is
created, and Review names the area and the name before the mutation. After a create the catalogue
is re-read, so the new project is selectable without leaving the app.

After a ready launch, where the surface goes depends on where it is hosted. From a bare shell
this process is replaced by the attach command for the session it just started,
`tmux -L remote-agents attach-session -t ra-<session>:`, exactly as before. Run inside the
console, the surface instead **exchanges** that session's pane into the console's left pane
and stays alive — nothing nests, nothing is switched, and the sessions list and feed stay
beside it. Either way the store is never held open across your work: the
surface's database connection exists only for the duration of a single store operation, so
however long it stays up beside running sessions, the terminal you type into holds no standing
database handle. The project ships no tmux configuration and sets no prefix, so detaching uses
tmux's own binding: `Ctrl-b d` on a stock tmux, or the same `d` under whatever prefix this
host's `~/.tmux.conf` sets. Detaching leaves the session running and managed; it stays listed,
inspectable, and stoppable from either surface. Started from inside somebody else's tmux
client, the launch still happens but the attach is refused rather than nested, and the command
to reach the new session is printed instead. An exec that cannot happen prints the same command
and exits non-zero, so a started session is never lost.

Ctrl+S lists the managed sessions. The list is the shared store's rather than this process's, so a
session the bot launched, or one a previous run of this app started, is there too; each row names
the session, its state, how long ago it started, and how full its context window is — a bar and a
percentage where the provider states a ceiling, and the bare token count where it does not, which
is the same rule the detail's own context line follows. Readiness is refreshed once as the list opens,
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
of them to offer. Claude Remote Control's two directions come from that same place, so which of
Enable and Disable a session offers is one answer both surfaces read rather than two that happen
to agree. The stops share a single row under the read-only actions, which each get a row
of their own: Telegram has no separator, so shape is the only thing distinguishing an action that
ends a session from one that reads it.

#### Codex Remote Control, for the whole machine

Beside `Plan limits` the terminal shows this computer's Codex Remote Control — `on`, `off`,
`connecting`, `no daemon`, `link broken` or `unreachable` — and `h` toggles it. Its subject is
the machine rather than a row, which is why it sits with the host facts and not on the sessions
list: turning it on enrols this host with OpenAI's relay so a paired phone can drive the Codex
sessions running here.

`P` mints a pairing code and shows it once, in a modal any key dismisses. Nothing stores it and
nothing can revoke it from here, so treat it like a password: anyone who types it into the
ChatGPT app gets control of this machine until it expires, and a phone that pairs keeps its
access afterwards. Turning Codex Remote Control off is what ends that.

**Codex sessions started after Remote Control is on are the ones your phone can see.** Only a
pane launched after the daemon is up lives in that daemon; one launched while the daemon is
down runs its own embedded server and stays invisible for its whole life, so restart it if you
need it reachable.

In Telegram the same fact appears under `Plan limits` in `/sessions`, and `/remote` opens the
toggle and the `Pair a phone` button. Both surfaces confirm the toggle and neither confirms the
reading.

The two keys are deliberately hidden from the footer, like `d`: the bar is shared with every
inherited binding, and these act on the host rather than on whatever row the cursor is on.

#### Keys on the sessions list

Every action the detail offers also has a single key on the highlighted row, on both the full
sessions screen and the console's sessions pane: `a` Copy attach, `i` Inspect output, `r`
Rename, `s` Stop and close, `c` Clean up, `f` Force stop, `m` Claude Remote Control. `d` opens
the detail itself, and on the console pane `p` returns the projects surface. They are bare
letters because these two positions have no filter to type into.

A key is a faster way to reach what a row already offers, and **where it acts depends on what
the key is for.** `a`, `i` and `r` open the session's detail and it performs them. `s`, `c` and
`f` end a session, and they act on the list you pressed them on — no detail is opened, the other
rows stay, and the outcome is said over the list. The confirmations are unchanged either way:
Force stop and both Remote Control directions still ask, and Stop and close and Clean up still
do not — on this surface and in Telegram alike. A key is offered only where the
policy offers the action, so `s` is absent on a preserved row and `c` on a running one, rather
than being present and inert.

Because two of those keys end a session without asking, **a background refresh that drops the
row you were on leaves the cursor on nothing at all** rather than falling back to the first
row. This list re-reads itself every ten seconds and restores your place by session rather
than by position; when the session you were on has gone there is no honest place to put the
cursor, and moving it silently onto a neighbour would put a live agent one keypress from an
unasked stop. One arrow press picks a row again.

Copy attach is always offered and answers when it is chosen: a pane that is not live, or one whose
project or agent does not match, is explained rather than left out, so a dead pane cannot be
mistaken for a surface that forgot to draw the entry. Inspect output renders the captured text
through the same sanitizer the bot uses, in a scrollable pane rather than under Telegram's message
bound, and refuses output containing a NUL byte for the reason the bot refuses it: a pane emitting
NUL is not rendering text, and printing it can corrupt the terminal. Claude Remote Control appears
only on a running Claude pane, offering the one direction its last observed state leaves open
exactly as Telegram does; the observation is stored with the session, so it holds across a restart
and across the other surface. It and Force stop each move to a step of their own before anything
is issued, with Cancel first and resting under the cursor, so going through with either means
choosing a different row on purpose rather than repeating the keystroke that raised it.

Ctrl+O resumes a saved conversation. It asks for the project, then the agent, offering only those
whose provider reports itself resume-capable on this host; capability comes from the probe that
asks each provider, never from a version allowlist. Then it pages that agent's conversations for
that project, ten at a time. A row carries safe metadata only; the provider ID and the transcript
stay server-side exactly as they do in Telegram, and what the row holds is an opaque reference the
server resolves, so a stale one resolves to nothing rather than to a path. Choosing a
conversation starts it — there is no confirmation step, matching the bot, which retired its own.
A ready resume hands this terminal
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
Review names the area and the name before the mutation happens. Cancel returns to the launch
project list — the screen Add Project is offered from — without a mutation.

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
lands in a separate process, so a running bot does not see it until it re-reads: press Launch
in the navigation bar, which re-reads the catalogue on entry. No registry field
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

A launched pane gets a small, fixed environment — `HOME`, `LANG`, `LC_ALL`, `PATH`, `TERM`,
`COLORTERM` and nothing else — because the fixed runner `exec`s the agent with exactly that
mapping rather than adding to what it inherited. `TERM` is guaranteed rather than merely passed
through: the daemon has no controlling terminal and so no `TERM` of its own, and an agent handed
no `TERM` renders monochrome, which is why a session launched from Telegram used to look
different from the identical session launched from the local surface. Absent or `dumb`, it
becomes `xterm-256color`. `COLORTERM` is passed on when the launching process has one and never
invented, since a truecolour claim is the terminal's to make.

See [the compatibility matrix and dedicated-socket recovery commands](docs/profile-compatibility.md).
