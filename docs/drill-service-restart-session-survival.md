# Drill: does a service restart take running agent sessions with it?

**Closes the automatable and owner-run halves of BL-001.** Read this once before starting; it
takes about ten minutes and does **not** touch your Telegram credential.

## What this proves, and what it does not

`systemd/remote-agents.service:14` sets `KillMode=process`. That line is what stops systemd
killing the whole control group — your running agent panes along with the service — when the
unit restarts. Today it is pinned only by
`tests/contract/systemd/test_remote_agents_unit.py`, which reads the unit **file**. Nothing has
ever confirmed systemd behaves that way on this host, with real panes, in a real restart.

That is the gap. If `KillMode` were wrong, or a future edit dropped it, a token expiring at 3am
would silently take every in-flight agent session and the work inside it.

**What this drill does not cover.** BL-001 as originally written revoked and replaced the live
Telegram credential. That half was dropped by owner decision on 2026-08-16: it breaks a working
credential to add only "does a bad token trigger the restart", which
`tests/live/test_telegram_owner.py`'s known-invalid-token probe largely covers already. The
session-survival property — the part that can lose work — needs no credential at all. If you
later want the rotation half, `docs/operator-runbook.md:104-125` is the written procedure.

## Before you start

The service must be running the code you are testing. Check:

```bash
systemctl --user show remote-agents.service -p MainPID -p ActiveState
ps -o pid,lstart,cmd -p "$(systemctl --user show -p MainPID --value remote-agents.service)"
```

If the start time predates your last deploy, **restart it first and start this drill over** —
a drill against stale code rehearses something nobody ships. This is not hypothetical: on
2026-08-16 the service had been running since 2026-08-14 20:22, three sub-plans behind `main`,
and reported `active` the whole time.

> `systemctl --user` needs a session bus. If it answers
> `Failed to connect to bus: No medium found`, prefix these commands with
> `XDG_RUNTIME_DIR=/run/user/$(id -u)`.

## Step 1 — the unattended half

Run this first. It needs no window and no live sessions, and if it fails the rest is moot.

```bash
uv run --locked python -m pytest tests/contract/systemd tests/integration -q
uv run --locked remote-agents doctor --json | python -m json.tool
```

- **Expected:** the suite passes, and `doctor` reports `"healthy": true`.
- **A wrong reading:** `healthy: false` with a `config` block naming keys means the deployed
  config has drifted from this build's schema — fix that before restarting anything, or the
  restart below will crash-loop instead of testing what you meant. That is BL-029's whole
  subject; see `docs/operator-runbook.md`.

## Step 2 — start a real agent session and leave it working

From Telegram or the local TUI, launch a session in any project. Give it something slow enough
that you will still be mid-task in two minutes — a long file read, a build.

Record what you see:

```bash
tmux -L remote-agents list-sessions
uv run --locked remote-agents doctor --history <session-id>
```

- **Expected:** one `ra-<session>` line from tmux, and a history ending in `ready`.
- **Write down the session id and the pane's pid.** You compare against them in step 4.

```bash
tmux -L remote-agents list-panes -a -F '#{session_name} #{pane_pid}'
```

## Step 3 — restart the service under it

```bash
systemctl --user restart remote-agents.service
systemctl --user is-active remote-agents.service
```

- **Expected:** `active`, within a second or two.
- **A wrong reading:** anything else. Capture `journalctl --user -u remote-agents.service -n 50`
  before doing anything else — a service that will not come back is a bigger finding than the
  one this drill went looking for.

## Step 4 — the assertion this drill exists for

```bash
tmux -L remote-agents list-panes -a -F '#{session_name} #{pane_pid}'
```

- **Expected:** the **same session name and the same pane pid** as step 2. Not a new pid — the
  same one. The agent process was never signalled.
- **This is the negative half, and it is the one that matters.** A restart that "worked" is
  easy to over-read; what makes rotation and restart safe is that the panes were *not* touched.
  A changed pid means `KillMode=process` is not doing what the unit file claims, and every
  future restart is a work-loss event.

Then confirm the service still owns the session rather than merely leaving it alive:

```bash
uv run --locked remote-agents doctor --history <session-id>
```

- **Expected:** the same history as step 2, with **no new terminal event** appended. No
  `reconciled_terminal_missing`, no `startup_error`.
- **A wrong reading:** a new terminal event means reconciliation decided the session had gone
  while it was in fact fine. That is the class Stage 4 fixed (DEC-030); report it with the
  history output rather than working around it.

## Step 5 — check the agent is still usable, not just alive

Attach and confirm the session responds:

```bash
tmux -L remote-agents attach-session -t ra-<session>:
```

- **Expected:** your agent, mid-task, exactly as you left it. Detach with your tmux prefix + `d`.
- **A wrong reading:** a live pane whose agent is dead or wedged. A pid surviving is necessary,
  not sufficient — this step is what tells the two apart, and `doctor --history` cannot.

Finally, stop the session normally from either surface and confirm it ends cleanly:

```bash
uv run --locked remote-agents doctor --history <session-id>
```

- **Expected:** `graceful_stop_requested`, `pane_exited`, `cleanup_confirmed`.

## Recording the result

Write it up as `docs/acceptance-<ISO date>-service-restart-survival.md`, following the structure
of `docs/acceptance-2026-08-11-agent-activity.md`: a provenance block (date, release commit,
this drill), a status blockquote saying **who performed which steps and how each confirmation
arrived**, the step-1 output in fenced blocks, and a per-step checkbox carrying **a real reading
each** — the actual pids, not "as expected".

The standard is the one that document set when it corrected a transcribed byte count by
reproducing the drill. A step you did not perform is marked unperformed, not folded into a
blanket confirmation.
