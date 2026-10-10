# Acceptance 2026-10-10 — Codex enable/launch race (BL-037 premise)

Verdict: RACE_REAL

A plain `codex` launch on a host with no daemon starts the **managed** backend. `codex
remote-control start --json`, which `_enable` runs when its probe finds no daemon, then stops
that backend: 10 of 10 drill reps logged `shutdown_requested` on the backend the launch had just
started. Whether the pane dies depends on where its TUI is when the stop lands. One rep in ten
died. Every rep that died was still in its startup handshake (`model/list failed during TUI
bootstrap`). A TUI past that point reconnects to the replacement backend.

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
| race | `remote-control start --json` | prompt just drawn | 5/5 | 1/5 |
| race | `remote-control start --json` | prompt + 0.75 s | 6/6 | 0/6 |
| running | `app-server daemon enable-remote-control` | prompt just drawn | 3/3 | 2/3 |
| disable | `app-server daemon disable-remote-control` (preference on) | prompt just drawn | 3/3 | 1/3 |
| control | none (4 s wait) | prompt just drawn | 0/2 | 0/2 |

- Every lost pane printed `Error: model/list failed during TUI bootstrap: … transport failed:
  WebSocket protocol error: Connection reset without closing handshake` and exited 1.
- Every surviving pane kept its codex pid and redrew its prompt or dialog: it reconnected.
- An unauthenticated first pass (the TUI on its sign-in screen) lost the pane in every rep whose
  preference flipped. The log keeps those result lines. It is superseded here because no
  managed launch sits on the sign-in screen.
- A settled TUI that has a conversation loses that conversation on a restart ("This
  conversation is unavailable"). That is the cost of *off* that `remote_control.py` already
  records. These reps had no conversation, so they cannot show it.

## What this decides

- **BL-037's race is real.** A launch that lands between `_enable`'s probe and its
  `remote-control start` brings up a backend that the start then stops. The pane dies if the
  stop lands inside the TUI's startup handshake.
- **The daemon-scoped verbs are not the safe branch `_enable`'s docstring says.**
  `enable-remote-control` "starts nothing and stops nothing" only when the preference is
  already on. On a real flip, it and `disable-remote-control` restart the backend, and a TUI
  still in its handshake dies (3 of 6). So a launch racing *any* flip has the same window, not
  only the no-daemon branch.
- **The window is the TUI's daemon handshake, not its first frame.** See the next section: the
  lock Task 2.2 planned, held until `_settle_launch` returns, does not cover it.

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
  before the footer in 4 of 5 launches.
- **Answering the trust dialog starts a second handshake.** In a git repo the TUI stops on the
  dialog at 0.76–0.83 s with no footer. With the dialog answered and `enable-remote-control`
  run at once, the pane died in 3 of 3 (`Error: config/read failed in TUI`). The race arms that
  restarted the backend *while the dialog was up* lost nothing (5 of 5). The pane dies after
  the answer.

So a guard that covers the sequence the surfaces teach must hold until the footer is drawn,
after a launch, after a resume and after a trust answer. Holding it until `_settle_launch`
returns is not enough.
