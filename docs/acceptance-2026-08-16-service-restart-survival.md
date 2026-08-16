# Acceptance: agent sessions survive a service restart

- **Date:** 2026-08-16
- **Release commit:** `e10269a81bbc169b79f92575c9333cfd9eac94a0` (2026-08-16 10:07:30 +0100)
- **Drill:** `docs/drill-service-restart-session-survival.md`
- **Plan:** `2026-08-13-backlog-closure-sub-05-durability-and-operator-safety-plan.md`, Stage 3
- **Closes:** BL-001 (session-survival half); BL-002 (automatable half)

> **Who did what.** The owner performed the redeploy — `systemctl --user restart
> remote-agents.service` at 10:38:02 — as an ordinary deploy of the merged branch, and reported
> it in conversation. Every reading below was then taken by the assistant from the live host,
> after the fact, and is machine output rather than transcription. **The drill was not run as a
> scripted exercise**: the restart the owner had already performed *was* step 3, and three
> agent sessions that happened to be running across it are the step-4 evidence. What that costs
> and what it buys is set out under *Deviations* before the readings, not after them.

## Outcome

**The property holds on this host.** `KillMode=process` (`systemd/remote-agents.service:14`) kept
every running agent pane alive through a full service restart. Until now that line was pinned
only by `tests/contract/systemd/test_remote_agents_unit.py`, which reads the unit *file*; nothing
had confirmed systemd's actual behaviour with real panes. It is confirmed.

## Deviations from the drill as written

Three, all of which weaken or strengthen the result in ways a reader should weigh:

1. **No session was launched for the drill.** The drill's step 2 says to start a session and
   leave it mid-task. Instead, three sessions that were already running were used. This means
   nobody controlled what the agents were doing at the moment of the restart, so "it was mid-task
   and resumed cleanly" is *not* claimed here.
2. **Correspondingly, the evidence is stronger on duration than the drill asks for.** Two of the
   three panes were 3 and 6 days old at the restart, so they have survived this restart *and*
   whatever earlier ones occurred in that window — a longer exposure than a purpose-built session
   minutes old would give.
3. **The credential-rotation half of BL-001 was not performed and is not claimed.** It was
   dropped by owner decision on 2026-08-16; the reasoning is recorded in the plan's
   *What changed about Stage 3* section and in the drill's own preamble.

## Step 1 — the unattended half

```
$ .venv/bin/python -m pytest tests/contract/systemd tests/integration -q
280 passed in 13.39s
```

`doctor` against the live deployed config — note the `config` block, which is BL-029's work
running against the real file for the first time:

```json
{
  "healthy": true,
  "config": {"detail": null, "invalid": [], "missing": [], "readable": true, "unknown": []},
  "components": {
    "core": {"reason": null, "status": "healthy"},
    "profiles": {"reason": null, "status": "healthy"},
    "service": {"reason": null, "status": "healthy"},
    "store": {"reason": null, "status": "healthy"},
    "telegram": {"reason": null, "status": "healthy"},
    "tmux": {"reason": null, "status": "healthy"}
  },
  "projects": {"catalogue": 96, "discovered": 91, "registered": 87}
}
```

- [x] **Unattended suite passes** — 280 passed, 13.39s.
- [x] **`doctor` reports `healthy: true`** — and `config.readable: true` with empty `missing`
      and `unknown`, i.e. the deployed config matches this build's schema. This is the check that
      did not exist when the same config class crash-looped the service through three restarts on
      2026-08-11.

## Step 3 — the restart

```
Aug 16 10:38:02  Stopping remote-agents.service ...
Aug 16 10:38:02  remote-agents.service: Consumed 2min 49.733s CPU time.
Aug 16 10:38:02  Started remote-agents.service - Remote Agents private Telegram control plane.
```

- [x] **Service came back `active`** — `ActiveState=active`, `NRestarts=0`, `MainPID=257059`.
- [x] **The new instance is running the merged code** — main PID started `Sun Aug 16 10:38:02`,
      against a release commit dated `10:07:30` the same morning. This check exists because the
      *previous* instance had been running since Aug 14 20:22:40, three sub-plans behind `main`,
      and reported `active` the entire time.

## Step 4 — the assertion the drill exists for

Pane processes, read after the restart, against a service process that started at 10:38:02:

```
  PID       STARTED                    ELAPSED       COMMAND
  2634469   Wed Aug 12 21:08:10 2026   3-13:33:20    node
  1916427   Sun Aug  9 22:06:53 2026   6-12:34:38    node
  1756409   Sun Aug 16 00:00:48 2026     10:40:43    claude
```

```
  ra-04c709b1-06be-4b7b-b3bc-a4423b524718  dead=0  pid=2634469  node
  ra-0734d69b-c353-403c-a730-e9ab802eeb7c  dead=0  pid=1916427  node
  ra-60533603-c95b-4b8c-988a-01d48862c8d2  dead=0  pid=1756409  claude
```

- [x] **Every pane pid is older than the service process** — by 6 days, 3 days and 10 hours
      respectively. Not restarted, not re-parented into a new pid: the same processes, never
      signalled. This is the negative half of the property and the half that matters, because a
      restart that "worked" is easy to over-read while what makes it safe is that the panes were
      not touched.
- [x] **No pane is dead** — `pane_dead=0` on all three.

The store's own account, read with `doctor --history` (the reader BL-030 added; before it, this
would have meant opening sqlite by hand):

```
04c709b1-06be-4b7b-b3bc-a4423b524718 · running
  2026-08-12T20:08:11.470271+00:00  ready
0734d69b-c353-403c-a730-e9ab802eeb7c · running
  2026-08-09T21:06:53.092265+00:00  ready
60533603-c95b-4b8c-988a-01d48862c8d2 · running
  2026-08-15T23:00:49.545590+00:00  ready
```

- [x] **No terminal event was appended to any session** — no `reconciled_terminal_missing`, no
      `startup_error`. Each history is still the single `ready` from its launch, and each record
      still reads `running`. Across the restart *and* the days of 60-second reconciliation passes
      before it, the reconciler wrote nothing spurious.
- [x] **No `InvalidTransition` since the restart** — `journalctl --user -u remote-agents.service
      --since "-15 min" | grep -c 'InvalidTransition'` → `0`. The five crashes in the journal all
      predate this build; the last was 2026-08-15 22:45.

## Step 5 — alive, and how far that was checked

```
  2634469 Ssl+  3-13:33:20 node
  1916427 Ssl+  6-12:34:38 node
  1756409 Ssl+    10:40:43 claude
```

- [x] **No pane process is a zombie or stopped** — all three are `S` (interruptible sleep) with
      `s` (session leader) and `+` (foreground group). A `Z` or `T` here would mean a pid that
      survived in name only.
- [ ] **The agent responds to input — NOT PERFORMED.** Two of the three sessions were attached by
      the owner at the time of reading and were deliberately not disturbed; the third was not
      attached to either. A live pid and a non-zombie state are **necessary and not sufficient**,
      exactly as the drill says, and nothing below the owner actually using the session can close
      that gap. Marked unperformed rather than folded into the confirmations above.
- [ ] **A normal stop after the restart — NOT PERFORMED.** Stopping a session the owner is
      working in was out of the question, so the drill's closing
      `graceful_stop_requested` / `pane_exited` / `cleanup_confirmed` sequence was not exercised
      here. It is covered for every profile by the trace audit below, from earlier sessions.

## BL-002 — the durable-trace audit, run against the production store

Read-only (`mode=ro`), opt-in, nothing driven:

```
$ REMOTE_AGENTS_LIVE_ACCEPTANCE=1 pytest tests/live/test_profiles_through_telegram.py -q
2 passed in 0.02s
```

- [x] **Every supported profile has a complete lifecycle trace** — `ready`,
      `graceful_stop_requested`, `pane_exited`, `cleanup_confirmed` present for all five, plus at
      least one `verified_force_stop`.
- [x] **The four auditable journey steps beyond launch-and-stop all left a trace** — a renamed
      session (step 3), a force stop (step 6), a resumed conversation (step 12), and durable
      callback rows (step 9).
- [x] **The opt-in gate is intact** — a bare `pytest tests/live/...` run *skips* both tests rather
      than failing, and reads no credential.

**One correction, recorded because the first run was wrong and looked plausible.** The step-9
check originally queried a table named `callback_state`; the table is `callback_states`. It
therefore reported *"the owner journey left no durable trace of: step 9 — a callback row
outliving a restart"* against a store that had 
the trace all along. A coverage gap is a believable
finding, which is what made it dangerous. The check now raises on a missing table instead of
answering "absent", so schema drift and journey coverage can no longer be confused, and every
table and column the file names was verified against the live schema before this run.

## What none of this can show

- **That an agent mid-task resumes cleanly.** No session was launched for the drill and none was
  driven, so what the agents were doing across the restart is unknown. Survival of the *process*
  is proven; continuity of the *work* is not.
- **That a revoked credential behaves correctly.** Not performed, by decision. The rotation
  procedure at `docs/operator-runbook.md:104-125` remains unexercised, and BL-001's original
  wording asked for it.
- **That the cross-process race is fixed.** It is not, and Stage 4 does not claim it: the local
  TUI drives its own `SessionService` in a separate process with its own `SessionLocks`, and no
  asyncio lock spans processes (DEC-005, DEC-030). The five journal crashes may have come from
  that path; execution reproduced the crash *class* but never pinned that exact variant.
- **That the property holds on any other host.** One machine, one systemd version, one restart.

## Known limitations

- Two of the three sessions were `node` (OpenCode or Cursor) rather than `claude`, so the sample
  is not one-per-profile.
- `NRestarts=0` means systemd has not had to auto-restart this unit; the restart measured here
  was operator-initiated. A crash-loop restart takes a different code path in systemd and is not
  covered.
- The reconciliation evidence is negative — nothing spurious was written — which is weaker than
  observing a repair happen correctly. The positive case is covered by unit and integration
  tests, not here.
