# Acceptance 2026-10-10 — Codex enable/launch race (BL-037 premise)

Verdict: RACE_REAL

A plain `codex` launch on a host with no daemon starts the **managed** backend. `codex
remote-control start --json`, which `_enable` runs when its probe finds no daemon, then stops
that backend: all 16 race-arm reps logged `shutdown_requested` on the backend the launch had
just started. Whether the pane dies depends on where its TUI is when the stop lands. A TUI
inside its daemon handshake exits: 6 of 6 plain launches with the stop at marker + 0.4 s, and 4
of 11 panes stopped just after their trust answer. A TUI past its handshake reconnects to the
replacement backend.

## Method

- `codex-cli 0.162.1` (the master measured 0.162.0; Preflight recorded the bump).
- Source: `openai/codex` at tag `rust-v0.162.1` (`092d3acd`), plus
  `codex-rs/app-server-daemon/src/lib.rs` at `rust-v0.151.0` for comparison.
- Drill: `docs/evidence/2026-10-10-codex-enable-race-drill.sh`; every capture is in
  `docs/evidence/2026-10-10-codex-enable-race-drill.log`.
  - Scratch `CODEX_HOME=/tmp/cxr` (short path, for the daemon socket). The owner's `~/.codex`
    and its daemon were never touched.
  - A private tmux server, `tmux -L cxdrill`.
  - Before each rep: `codex app-server daemon stop`, the saved preference set to off (the state
    *Turn on* is pressed from), and the probe confirms nothing is listening.
  - Each rep launches `codex` in a fresh `git init` repo, waits for the daemon socket, then runs
    the verb under test. It reads the pane 6 s later.
- The scratch home was logged in with a dummy API key (`codex login --with-api-key`). That puts
  the TUI at a real prompt. No model request is made, and the relay cannot enroll the host:
  `remote-control start` exits 1 with "the connection is errored" after it has already
  restarted the backend.

## Source: why the launch reaches the destructive branch

- `daemon_auto_start` is a **stable feature, on by default** (`codex features list`: `stable
  true`). With it on, the TUI calls `codex_app_server_daemon::start_with_features`
  (`tui/src/startup_orchestration.rs:509-575`) and attaches to the daemon socket with
  `allow_embedded_fallback: false`.
- `start` (`app-server-daemon/src/lib.rs:409`) spawns a managed pid backend when nothing
  answers, plus the `pid-update-loop` updater. Measured: the socket answered 0.8–2.0 s after
  the launch, and `ps` showed `codex app-server --listen unix:// … --managed-daemon` under the
  scratch home.
- `remote-control start` runs `ensure_remote_control_started` (`lib.rs:703`). With the updater
  running, `is_bootstrapped` is true, so it calls `set_remote_control_locked(Enabled)`. Without
  it, it calls `bootstrap_locked`. **Both stop a running managed backend**:
  - `bootstrap_locked` (`lib.rs:814`) calls `stop_with_grace` on any running backend.
  - `set_remote_control_locked` (`lib.rs:740`) calls `stop_with_grace` and then
    `start_managed_backend` whenever the saved preference **changes**. 0.151.0 does the same
    with `stop()` (`lib.rs:567-575` at that tag).
- Codex's operation lock serialises `start` against `bootstrap`, so neither interleaves inside
  the other. It does not stop a `start` that has finished from being followed by a stop.

So `docs/profile-compatibility.md`'s premise, that a launch "starts its own embedded server when
it does not [find a daemon]", is false at 0.162.1 with default features. The launch starts the
very backend the enable then stops.

## Drill results (authenticated TUI)

| arm | verb | TUI state when it ran | backend stopped | pane lost |
|---|---|---|---|---|
| race | `remote-control start --json` | trust dialog | 5/5 | 0/5 |
| race | `remote-control start --json` | prompt just drawn after the trust answer | 5/5 | 1/5 |
| race | `remote-control start --json` | the same, 0.75 s later (see below) | 6/6 | 0/6 |
| running | `app-server daemon enable-remote-control` | prompt just drawn after the trust answer | 3/3 | 2/3 |
| disable | `app-server daemon disable-remote-control` (preference on) | the same | 3/3 | 1/3 |
| control | none (4 s wait) | the same | 0/2 | 0/2 |

- **Every "prompt" row is a pane that had just answered its trust question.** The drill
  launches in a fresh git repo, so Codex asks; the script presses Enter and fires as soon as
  the prompt is drawn, about 1.17 s after the launch. These rows measure the second handshake,
  not a plain launch's. Plain launches are measured in their own section below.
- The 0.75 s row is not a timed window: in reps 1-5 the script's wait for the prompt ran out,
  and the verb fired about 11.8 s after the launch; rep 6 fired at 1.9 s. It shows a settled
  pane surviving, nothing more.
- Every pane lost here printed `Error: model/list failed during TUI bootstrap: … transport
  failed: WebSocket protocol error: Connection reset without closing handshake` and exited 1.
- Every surviving pane kept its codex pid and redrew its prompt or dialog: it reconnected.
- An unauthenticated first pass (the TUI on its sign-in screen) lost the pane in every rep whose
  preference flipped. The log keeps those result lines. It is superseded here because no
  managed launch sits on the sign-in screen.
- A settled TUI that has a conversation loses that conversation on a restart ("This
  conversation is unavailable"). That is the cost of *off* that `remote_control.py` already
  records. These reps had no conversation, so they cannot show it.

## Plain launches, timed against the stop

`docs/evidence/2026-10-10-codex-plain-launch-drill.py`: a non-git folder (no trust question),
`enable-remote-control` fired a fixed delay after the readiness marker `Codex` first appears.
The marker appeared at 0.32-0.34 s in every rep.

| fired at | reps | footer already drawn | pane lost |
|---|---|---|---|
| marker + 0.2 s (about 0.53 s) | 6 | 0 | 0 |
| marker + 0.4 s (about 0.73 s) | 6 | 0 | 6 |
| marker + 0.75 s (about 1.08 s) | 8 | 8 | 0 |

- At + 0.2 s the daemon had not yet answered (it answers at about 0.55 s), so the TUI had
  nothing to lose and reconnected.
- At + 0.4 s every pane died, each with a different handshake call failing:
  `account/read failed during TUI bootstrap`, `config/read failed while checking remote project
  trust`, or `failed to connect to remote app server`.
- At + 0.75 s, which is where the plan's original lock would have ended, the footer had always
  been drawn and nothing was lost.

## What this decides

- **BL-037's race is real.** A launch that lands between `_enable`'s probe and its
  `remote-control start` brings up a backend that the start then stops. The pane dies if the
  stop lands inside the TUI's daemon handshake (6 of 6 at marker + 0.4 s).
- **The daemon-scoped verbs are not the safe branch `_enable`'s docstring says.** On a real
  flip, `enable-remote-control` and `disable-remote-control` restart the backend, and a TUI
  still in its handshake dies (3 of 6). That `enable-remote-control` leaves a running backend
  alone when the preference is already on is read from source (`set_remote_control_locked`
  returns before its restart branch), not measured. So a launch racing *any* flip has the same
  window, not only the no-daemon branch.
- **For a plain launch, the plan's lock would have sufficed on this host; the footer is the
  sturdier end.** The kill window closed before marker + 0.75 s in 8 of 8 reps. The footer is
  drawn from the handshake itself, so waiting for it still covers the window on a loaded host,
  where a fixed 0.75 s may not. That is a judgement on top of the data, not a measurement of a
  loaded host.
- **A trust answer starts a second handshake that the plan's lock never covered.** The launch
  returns on the dialog and its lock is released; the answer comes later. 4 of 11 panes stopped
  just after their answer died (and 3 of 3 in the timing script below).
## The handshake window, timed

`docs/evidence/2026-10-10-codex-banner-gap.py` and
`docs/evidence/2026-10-10-codex-handshake-timing.py`, same scratch home. Seconds after spawn,
from the preference off and no daemon:

| what | rep 1 | rep 2 | rep 3 | rep 4 | rep 5 |
|---|---|---|---|---|---|
| readiness marker `Codex` and the prompt (non-git folder) | 0.33 | 0.33 | 0.33 | 0.33 | 0.32 |
| daemon socket answers | 0.56 | 0.55 | 0.55 | 0.55 | 0.55 |
| footer `<model> default · <cwd>` drawn | 1.06 | 1.19 | 1.26 | 1.12 | 1.15 |

- **The TUI draws its banner and prompt before the daemon is up.** The marker therefore says
  nothing about the handshake. The prompt alone stays on screen while the handshake runs.
- **The footer is the first thing drawn from the handshake.** It names the model, which comes
  from `model/list`. It was on screen in every pane that survived a restart, and absent from
  every pane that died.
- **A lock held from spawn to `_settle_launch` ends at marker + 0.75 s, about 1.08 s.** That is
  before the footer in 4 of 5 of these launches. This script polled the daemon socket with a
  `codex` command every 20 ms while it timed, which slows the TUI it measures: the plain-launch
  drill above, which does not poll, saw the footer by 1.08 s in 8 of 8.
- **Answering the trust dialog starts a second handshake.** In a git repo the TUI stops on the
  dialog at 0.76–0.83 s with no footer. With the dialog answered and `enable-remote-control`
  run at once, the pane died in 3 of 3 (`Error: config/read failed in TUI`). The race arms that
  restarted the backend *while the dialog was up* lost nothing (5 of 5). The pane dies after
  the answer.

So a guard that covers the sequence the surfaces teach holds until the footer is drawn after a
launch, after a resume and after a trust answer. For the trust answer the plan's lock was not
enough; for a plain launch the footer is the conservative end.

## Not covered

- **A trust question answered at the keyboard.** Only the bot's Trust button goes through
  `answer_trust` and its guard. An owner who presses Enter in the pane starts the second
  handshake with no guard held, and a toggle landing in the next second can still exit it.
- **A resume's handshake is not measured.** Creating a conversation to resume needs a model
  request, which these drills do not make. The resume is guarded by the same footer wait on
  the assumption that its handshake ends where a launch's does.
- **A `codex` started outside remote-agents** takes no guard and still races (BL-037's accepted
  residual).

