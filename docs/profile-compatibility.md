# Profile compatibility and local recovery

Get the current machine-readable view with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The profile record contains no credentials, terminal output, project paths, prompts, or
environment values. `AVAILABLE` means the executable is present; its version is diagnostic
information only, so a local update does not disable Telegram launches. Every launch still has
to reach its agent-specific readiness state. Authentication and workspace trust are never
bypassed; a trust dialog remains a local operator action.

| Profile | Fixed launch argv | Availability/auth/trust | Resume catalogue / selection | Readiness evidence | Fixed graceful exit |
| --- | --- | --- | --- | --- | --- |
| `claude` | `claude` | executable must be present; local auth/trust stays local | documented UUID filenames/project directories plus a bounded generated title or stored resume description; enabled when the catalogue is available | `Claude Code`, rejecting workspace-trust dialog | `/exit`, Enter |
| `claude-remote` | `claude --remote-control ra-<uuid>` | executable must be present; local auth/trust stays local | not a resume profile | `Claude Code`, rejecting workspace-trust dialog | `/exit`, Enter |
| `codex` | `codex` | executable must be present; local auth/trust stays local | feature-probed app-server `thread/list` with a bounded provider title or preview when supplied | `/exit` command selection and submit | `/exit`, Enter, Enter |
| `opencode` | `opencode` | executable must be present; local auth/trust stays local | `session list --format json`; enabled only after its JSON contract succeeds | `Ask anything...` interactive UI | Ctrl-C |
| `cursor-agent` | `cursor-agent` | executable must be present; local auth/trust stays local | disabled: `ls` is interactive and has no structured safe identifier catalogue | `/quit` command selection and submit | `/quit`, Enter, Enter |

**Claude's** Remote Control — the *pane* action — is available only for a live managed
`claude` pane. Enable and Disable each require confirmation and use the qualified in-pane
interaction; a stale or unclassifiable capture fails closed. No other profile has a Remote
Control of that kind, `claude-remote` included.

That is a statement about the pane action and not about the words "Remote Control". Codex
publishes one too, with a different subject — the machine rather than a pane — and it is
described in its own section below. Reading the sentence above as "only Claude can be remote
controlled" was the misreading this paragraph now exists to prevent.

The profile arguments are defined in the closed catalogue; Telegram does not provide an
executable, path, raw argument, prompt, keystroke, bypass, or auto-approval flag.

## Local recovery

Use the dedicated managed socket only. Never run the equivalent command without
`-L remote-agents`, and replace the placeholder with the complete generated session ID.

```bash
# Read-only inventory of managed sessions. `#{pane_id}` is the address the commands below
# use: a session name resolves to whichever pane occupies that window, which stops being the
# agent's the moment the console is showing it.
tmux -L remote-agents list-panes -a -F '#{pane_id} #{session_name} #{pane_dead} #{@remote_agents_id}'

# Inspect one exact managed pane; its UUID must match the stored session record.
tmux -L remote-agents capture-pane -p -t %<pane-id>

# Remove one exact pane only after verifying its ownership metadata and preserved output.
tmux -L remote-agents kill-pane -t %<pane-id>
```

**Address the pane, not the session.** These commands named `ra-<uuid>:` until 2026-08-19.
That is a *window* target, and tmux resolves it to whatever pane is in that window now — so
with the console displaying an agent, `capture-pane` reads the wrong screen and, worse,
`kill-session` does not reach the agent at all: a window linked into another session is not
closed by killing the session it came from, so the command exits 0, the session name
disappears, and the agent keeps running. Take the pane id from the inventory above and use it.

If SQLite is unavailable or a profile reports `BLOCKED`, do not issue a mutation from the
service. Preserve the database files and use only the read-only inventory/capture commands
until the local cause is repaired.

The bot does not inspect, terminate, or adopt arbitrary local agent processes. Resume is limited
to provider-catalogued conversations started in a new managed tmux pane.

## Codex Remote Control

**It is a property of this machine, not of a pane.** Claude's Remote Control is a fixed action
on one live owned pane. Codex's is a machine-wide setting on the shared `codex app-server`
daemon: turning it on enrols this host with OpenAI's relay so a paired phone can drive the
agent sessions running here. There is one reading and one toggle for the whole computer, and
both surfaces show it beside `Plan limits` rather than on a session row, because it is not a
fact about any session.

**Which command each direction runs, and the one that is never run.** Turning it *on* picks
its verb from the state the host is actually in: `codex app-server daemon
enable-remote-control` when a daemon is already listening, and `codex remote-control start
--json` only when nothing is. Turning it *off* is always `codex app-server daemon
disable-remote-control`, which flips the persisted preference and the running daemon in one
step. `codex remote-control stop` is **never** issued: it tears the daemon down and leaves it
down.

**A correction worth reading before you rely on "off".** This document previously said the
disable verb leaves the daemon up and therefore spares attached panes. It does not. Reading
`set_remote_control_locked` upstream: when the preference actually changes and a managed
backend is running, it stops that backend and starts a new one. The daemon returns; panes
attached to the old process disconnect, and a Codex TUI exits on disconnect. The difference
from `stop` is that the daemon comes back, not that your sessions survive.

The genuinely non-destructive "off" is the daemon's own `remoteControl/disable` RPC — what the
CLI itself calls when the preference is already at the target — which flips it in-process with
no restart. This service does not use it yet; see BL-038. Until then, treat turning Remote
Control off as something that may cost you attached Codex sessions, and stop them yourself
first if that matters.

The reason "on" is a choice rather than a constant is the same reason `stop` is banned.
`codex remote-control start` reaches a function that, on a host which has not been
bootstrapped, tears down a running managed backend before starting its own. Probing first and
using the daemon-scoped verb when a daemon exists keeps that branch away from a machine with
live panes.

**Only sessions started after the daemon is up are visible to the phone.** Every `codex` TUI
is app-server backed, and a plain `codex` launch attaches to the daemon socket when it answers
and starts its own embedded server when it does not. A managed pane launched **after** Remote
Control is on therefore lives in the daemon and the phone can see it; one launched **before**
is embedded, cannot be moved, and stays invisible for its whole life. Restart such a session if
you need it reachable. Neither surface can prevent a launch racing an enable, so this is a rule
to know rather than a guard to rely on.

**The pairing code is shown once.** `Pair a phone` mints a short-lived manual code, and both
surfaces display it exactly once: the terminal in a modal that any key dismisses and that no
snapshot ever captures, the bot in a single unforwardable message with no buttons under it.
Nothing stores it, nothing logs it, and nothing here can revoke one — anyone who types it into
the ChatGPT app gets control of this machine until it expires, and a phone that pairs keeps its
access afterwards. Turning Codex Remote Control off is what ends that access. If you lose a
code, mint another; the old one expires on its own.

**Starting the daemon needs OpenAI's standalone Codex install, not the npm package.** The
verbs that *start* one — `codex remote-control start` and `codex app-server daemon start` —
refuse without it (`Error: managed standalone Codex install not found at
~/.codex/packages/standalone/current/codex`), because that fixed path is where the daemon
starts and updates app-server from. The verbs that only flip the persisted preference,
`app-server daemon enable-remote-control` and `disable-remote-control`, work on either
distribution and answer with JSON.

So on a host with the npm package: the reading is `no daemon`, because none is running and
none can be started; pressing *off* succeeds and changes the stored preference; pressing *on*
reads `unreachable`, which is the honest answer for an install that cannot bring a daemon up.
The preference can be set, but nothing will ever serve it, so no phone can reach the machine.
`codex --version` and `codex remote-control --help` both succeed regardless, which is why the
absence shows up only when the toggle is pressed.

Install the standalone distribution if you want this feature:
`curl -fsSL https://chatgpt.com/codex/install.sh | sh`. Note it may place a `codex` on your
`PATH` ahead of an npm one; check `command -v codex` afterwards if the npm build is the one
you want launching sessions.

**Six readings, and three of them are not "off".** `on`, `off`, `connecting`, `no daemon`,
`link broken` and `unreachable`. `no daemon` means nothing is listening, so nothing can say
whether this host is enrolled — the preference outlives the daemon that serves it, which is why
it is not reported as off. `link broken` means the daemon answered and said its own connection
to the relay is broken. `unreachable` means `codex` did not answer at all, which on a host
without it installed is every path at once.
