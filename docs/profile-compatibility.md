# Profile compatibility and local recovery

Get the current machine-readable view with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The profile record contains no credentials, terminal output, project paths, prompts, or
environment values. `AVAILABLE` means the executable is present; its version is diagnostic
information only, so a local update does not disable Telegram launches. Every launch still has
to reach its agent-specific readiness state. Authentication is never bypassed and stays a
local operator action.

**Workspace trust is not.** A launch that lands on an agent's folder-trust dialog is recorded
`untrusted` on the first capture that shows it, rather than spending the startup budget and
failing (DEC-016). The owner answers from the bot, which is where the pane is *not* visible;
the local surface shows the word and hands over the dialog itself, because the console has the
agent's pane on screen (DEC-047). Saying *yes* is confined to the agents whose dialog this
project can read — `claude` and the declared Codex folder dialog — and is a keypress into that
exact dialog. A new or unrecognised dialog holds rather than receiving guessed keys. Saying
*no* is available for every agent, because it ends a session that never started. It takes
effect without a confirmation step (DEC-078) — not uniquely, since Stop and close and Clean up
are unconfirmed too (DEC-018), but it is the only ending offered from a state whose stop policy
otherwise carries force alone.

| Profile | Fixed launch argv | Availability/auth/trust | Resume catalogue / selection | Readiness evidence | Fixed graceful exit |
| --- | --- | --- | --- | --- | --- |
| `claude` | `claude`, or `claude --remote-control ra-<uuid>` when the stored Remote Control default reads *on* | executable must be present; local auth/trust stays local | documented UUID filenames/project directories plus a bounded generated title or stored resume description; enabled when the catalogue is available | `Claude Code`; a workspace-trust dialog answers the launch as `untrusted` | `/exit`, Enter |
| `codex` | `codex` plus the selected curated permission flags below | executable must be present; local auth/trust stays local | feature-probed app-server `thread/list` with a bounded provider title or preview when supplied | `/exit` command selection and submit | `/exit`, Enter, Enter |
| `opencode` | `opencode` | executable must be present; local auth/trust stays local | `session list --format json`; enabled only after its JSON contract succeeds | `Ask anything...` interactive UI | Ctrl-C |
| `cursor-agent` | `cursor-agent` | executable must be present; local auth/trust stays local | disabled: `ls` is interactive and has no structured safe identifier catalogue | `/quit` command selection and submit | `/quit`, Enter, Enter |

**Claude's** Remote Control — the *pane* action — is available only for a live managed
`claude` pane. Enable and Disable each require confirmation and use the qualified in-pane
interaction; a stale or unclassifiable capture fails closed. No other profile has a Remote
Control of that kind.

The second curated spelling, `claude-remote`, is **retired as of 0.41.0**. It was
`claude --remote-control ra-<uuid>`: one launch flag modelled as a separate agent, which put
the choice in every picker and made it a property of whichever session the owner happened to
start. The flag is now chosen once per launch from a host-wide setting — Claude's own
`remoteControlAtStartup`, edited from either surface's Settings screen — and stored sessions
that named the retired id were rewritten by migration 13.

That is a statement about the pane action and not about the words "Remote Control". Codex
publishes one too, with a different subject — the machine rather than a pane — and it is
described in its own section below. Reading the sentence above as "only Claude can be remote
controlled" was the misreading this paragraph now exists to prevent.

The profile arguments are defined in the closed catalogue. Telegram can select a named
permission preset; it does not accept an executable, path, raw argument or arbitrary keystroke
as launch configuration. Full access maps only to the reviewed variant below.

## Codex permission presets

Both surfaces use one closed catalogue and one persisted remote-agents default. The absent-key
default is `approve_for_me`; the setting does not edit `~/.codex/config.toml`.

| Persisted value | Label | Permission arguments, fresh and selected resume |
| --- | --- | --- |
| `approve_for_me` | Approve for me | `--approve-for-me` alone |
| `ask` | Ask me | `--sandbox workspace-write --ask-for-approval on-request -c approvals_reviewer="user"` |
| `full_access` | Full access | `--dangerously-bypass-approvals-and-sandbox` |
| `provider_default` | Use Codex configuration | None |

Fresh arguments follow `codex`; resume arguments follow `codex resume <provider-thread-id>`.
The preset adds no model/provider selection. Approve for me is not combined with sandbox,
approval or bypass switches. Ask explicitly resets the reviewer to the user, even if the
provider configuration normally requests automatic review. Full access is an explicit choice.
These mappings follow the [official CLI reference](https://learn.chatgpt.com/docs/cli/reference)
and [approval configuration](https://learn.chatgpt.com/docs/agent-approvals-security).

For each new process, precedence is:

| Priority | Source |
| --- | --- |
| 1 | Explicit per-launch/resume override |
| 2 | Inherited source session's immutable preset, for restart/rollover |
| 3 | Recorded managed conversation preset, for manual resume |
| 4 | Current persisted default |

An inherited legacy source without a preset falls back to the current default. An already
running conversation is reused only when its pane and project/profile ownership are verified;
unavailable or unreadable pane evidence holds without changing its preset. Unknown stored metadata is shown
as unknown; it is never displayed as a fabricated default. Invalid values or an override for
a provider without this capability refuse before a new pane. Stale buttons re-check capability.

The stored value describes the requested launch configuration. Native in-pane edits are not
tracked, and a managed policy may constrain or refuse a requested mode. An older CLI that
lacks a requested option is unsupported for that mode; there is no substitute argv or silent
fallback. Existing sessions are preserved when configuration cannot be read or saved.

**Native qualification: prepared, not run at this sub-plan.** Local composition, argument,
persistence and frontend checks do not prove effective native policy. The final combined gate
runs `tests/live/test_codex_permission_presets.py` serially with `-m live_acceptance` and
`REMOTE_AGENTS_LIVE_ACCEPTANCE=1`. Four cases each launch and resume one disposable native
session, observe actual `turn_context` approval policy, sandbox and reviewer, and check the
saved snapshot after the default changes. The probe asks only for a text reply and rejects
tool use. Collection alone is not native acceptance.

The drill requires Linux bwrap, Codex, tmux and existing file authentication. Its private home
uses a read-only bind of that auth file, not a credential copy or writable symlink. Normal
profiles and hook trust are unchanged; writable state is confined to owned fixture directories.
It selects no model/provider. Missing auth/CLI/support and managed-policy disagreement remain
an open native claim, reported as BLOCKED or failed evidence. Optional
`REMOTE_AGENTS_CODEX_PRESET_REPORT` records only version, preset and observed policy tuples
after successful cleanup. Live fixture ownership/auth handling is audited again before the
master's final native run.

## Continuation routing and qualification

The v2 destination is explicit execution metadata, independent of the folder containing the
plan records. The reader resolves exactly one registered project or recorded worktree and
stores the source/destination identities, roots, branch, plan/task locator and preset before
launch. Registry changes, removed roots, branch mismatches and ambiguous folder ownership
refuse; later writer reports cannot redirect the frozen request channel or target.

The same folder authority serves manual and restart resume. Ended outside-folder bindings
remain discoverable under their recorded project, deduplicated with the provider catalogue.
A removed worktree cannot be revived as an outside folder. Automatic legacy recovery without
an immutable registered-root snapshot holds; explicit current catalogue selection retains
historical session IDs and supplies its current project context. Existing presets remain
unchanged on a hold. Ambiguous server loss and surviving-turn restart hardening are still
separate open work (BL-136/137/138).

For Codex writer attribution, only an exact independent managed conversation binding can
establish identity. Current hook marks can reject a conflicting binding but cannot establish
one through their folder/time heuristic. A reported destination root is not identity proof.
Fresh unbound conversations, missing/stale evidence, ambiguity and unmanaged shells refuse;
the daemon starter's inherited session is never a fallback. The native first cross-repo
fresh-conversation claim remains open until that independent binding is demonstrated.

A same-root stage adoption resumes its existing owned run at the actual target task and
checks that run's latest saved handoff receipt. Different repositories retain independent
owned runs and source-channel outcomes; copying a predecessor's state or reusing a terminal
completed run is not destination acceptance.

**Evidence scope.** The five-project source matrix runs the built Claude and Codex writers
through A→B→B stage 2→C→D worktree→E with real application composition and SQLite, using a
synthetic terminal. It covers frozen presets, owner/automatic requests, root refusals and
restart/crash boundaries. Stage 2 separately observed a genuine completed Codex conversation
resumed through the public terminal and a named hold after its owned pane became unavailable.
That observation does not qualify every outside-folder, fresh-identity or restart case.
The final native four-preset/identity/destination-adoption/restart tests and independent
Stage 1/Telegram demonstrations remain separate required gates. Collection is not acceptance.

**Fixture safety.** The prepared drills confine writable state to owned directories and
mount existing credentials read-only over empty placeholders. Each uses private HOME and,
for Codex, a private CODEX_HOME with the qualified local plugin. They do not copy normal
provider/model choices or hook-trust state, and they never grant hook trust automatically.
Missing authentication, unsupported provider behavior or required owner hook review is
BLOCKED. Process cleanup verifies the exact private home, UID/start time and pidfd ownership;
unreadable ownership or failed cleanup retains the home and reports the failure.

**Later reader-first installation.** This is an operator procedure, not an action performed
by source qualification:

1. Preserve database and pending handoff evidence; pause new automatic launches before changing
   an installed service. Keep unresolved transactions available for inspection.
2. Install a compatible reader accepting v1/v2 before enabling a writer that emits cross-root v2.
   Apply only additive migrations; preserve folder, root, permission and routing metadata.
3. Install the corresponding qualified Claude/Codex writer builds and start fresh managed
   sessions to advertise capabilities. Existing sessions keep their recorded capability;
   a newer installed reader does not retroactively grant them v2.
4. Resolve provider authentication/folder trust and any explicit hook review locally, then run
   the separate native and user-route acceptance gates. An unverified install is not accepted.
5. Before rollback, drain or explicitly cancel open v2 transactions. Keep their immutable
   metadata and predecessor panes; do not destructively roll back the database schema.

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

**What "off" actually costs you, measured.** This paragraph has been wrong twice, in opposite
directions, so here is what was observed on a standalone install rather than what was reasoned
from source. Turning Remote Control off **does** restart the daemon: the app-server process is
replaced and the `--remote-control` flag is gone from its argv. An attached `codex` pane is
**not** killed — same pid, still at a usable prompt afterwards. What it loses is the
conversation: the TUI reconnects to the replacement daemon and reports `Reconnected. No input
was resent.` and then `This conversation is unavailable; no operation was sent.`, and that state
does not heal. So "off" costs you in-flight work in attached panes, not the panes themselves.
Finish or checkpoint anything you care about before pressing it.

**"On" restarts the daemon too, and a pane still starting does not survive either.** Measured
on codex-cli 0.162.1 (`docs/acceptance-2026-10-codex-enable-race.md`): `remote-control start`,
`app-server daemon enable-remote-control` and `disable-remote-control` all stop and restart the
running daemon whenever they change the preference. A pane past its daemon handshake reconnects
as above. A pane still inside it exits with `transport failed`. That is its first second or so
after a launch or resume, and the second after its trust question is answered. remote-agents
holds one lock across its own Codex launches, resumes and Trust-button answers until the
handshake is drawn, and across every toggle. A toggle waits up to 30 s for a starting session.
A launch waits 2 s for a toggle, then says "launch again in a moment" with nothing started. Two
things take no lock and can still be caught: a `codex` started outside remote-agents, and a
trust question answered by pressing Enter in the pane, in the second after the answer.

There is no gentler verb to switch to. An earlier version of this section proposed the daemon's
own `remoteControl/disable` RPC; no such client request exists in the app-server protocol (the
schema defines `RemoteControlEnableParams`/`RemoteControlDisableParams` as orphan types that no
method references), and the stdio transport it assumed does not work at all. See BL-040.

**Where the reading comes from, and what it cannot see.** Not from the daemon: it was asked
over `codex app-server proxy` for a `remoteControl/status/read` method until 2026-09-03, and
that method does not exist in the protocol while the transport never answered `initialize` on
any host this was run against, so the reading was always a fallback or an error (BL-040). The
CLI offers no read-only status verb either. The reading is therefore two local facts:

| `$CODEX_HOME/app-server-daemon/settings.json` | daemon running? | reading |
| --- | --- | --- |
| `remoteControlEnabled: false` | not consulted | **off** |
| `remoteControlEnabled: true` | yes | **on** |
| `remoteControlEnabled: true` | no | **no daemon** — on, but nothing is serving it |
| unreadable, absent, or not a boolean | — | **unreachable** — never "off" |

The preference is Codex's own file, rewritten on every toggle, so it stays right when *you*
flip Remote Control from a terminal — which is the thing a value this project remembered for
itself could never do. It is an internal file rather than a documented interface (DEC-063), so
it fails closed: anything unreadable is "no reading", and a future Codex that moves the file
degrades to honest silence instead of a confident "off" on an enrolled machine.

Two things this cannot see, and no surface claims otherwise. **Whether the relay link is
healthy** — a daemon can be up and enrolled and unable to reach OpenAI, and that reads as
**on**; the `link broken` reading now comes only from the enable command's own output, at the
moment of enabling. And **the machine's name**, which the settings file does not carry, so only
`Pair a phone` and a fresh enable can show it.

The reason "on" is a choice rather than a constant is the same reason `stop` is banned.
`codex remote-control start` reaches a function that, on a host which has not been
bootstrapped, tears down a running managed backend before starting its own. Probing first and
using the daemon-scoped verb when a daemon exists keeps that branch away from a machine with
live panes. It does not make the daemon-scoped verb harmless: as measured above, it restarts
the daemon whenever it changes the preference.

**Every Codex session lives in the daemon now.** `daemon_auto_start` is a stable Codex
feature, on by default, so a plain `codex` launch attaches to the daemon socket when it answers
and **starts the daemon itself** when it does not (measured on 0.162.1: the socket answered
about 0.55 s after the launch). Older releases started an embedded server instead, and a
session launched before Remote Control was on stayed invisible to the phone. Whether a session
that reconnected across an enable is then visible to the phone has not been measured. If one
is not, restart it.

**The pairing code is shown once.** `Pair a phone` mints a short-lived manual code, and both
surfaces display it exactly once: the terminal in a modal that any key dismisses and that no
snapshot ever captures, the bot in a single unforwardable message carrying one client-side `Copy code` button -- which sends nothing to the bot, so it cannot re-send the secret -- and which takes itself out of the chat at your next interaction with the bot.
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
