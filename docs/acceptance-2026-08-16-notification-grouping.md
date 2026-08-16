# Acceptance: fewer notifications, and one message per session

Date: 2026-08-16
Release: 0.11.0 — branch `feat/notification-value-and-grouping`, commit `be980c8`
(the owner's run was against the service restarted from this branch on 2026-08-16)
Plan: `2026-08-16-notification-value-and-grouping-plan.md`, Task 3.4

> **Status: RUN AND ACCEPTED, 2026-08-16.** The machine-side half is recorded from its own
> output. The owner-side half was performed by the owner against the real Telegram client and
> the real service, and reported as a **single blanket confirmation** — "Yes, it works as
> intended" — against a seven-instruction sequence, **not** as four separate readings.
>
> That distinction is preserved rather than smoothed over, for the reason
> `docs/acceptance-2026-08-11.md` gives: what each step records is the owner's *coverage* of it,
> and the independent machine-side corroboration is listed separately. No per-step observation
> has been invented, and the four steps below therefore share one line rather than carrying four.
>
> The environment this branch was built in has no systemd user session and no way to reach
> Telegram, which was recorded as a Preflight failure before Stage 1 began. It does have real
> `tmux`, a real `claude`, the real hook installed into `~/.claude/settings.json`, and the
> operator's real config — so everything except the network call to Telegram could be driven
> with production code, and was.
>
> The split below is the point, and it is the one `docs/acceptance-2026-08-11.md` insists on:
> what a machine confirmed is listed as such, and what only an owner can witness is listed
> separately and attributed to them. This document was laid out by the session that wrote the
> code, which is exactly the party whose observations are worth least — so every line in Part 1
> is a transcript rather than a summary, and no line in Part 2 says more than the owner said.
>
> **What the machine half could not establish, and Part 2 exists for:** that Telegram accepts and
> renders these messages legibly on a phone, that the `Open session` button round-trips and
> consumes the notification, and that the service runs this build under systemd on the real
> host.

## What changed, in the owner's terms

The service used to send one message per observation, about any session, for six kinds of event.
It now sends **one message per session per delivery pass**, only about a session that is still
live, and only for events that carry something to do about them.

- Pressing **Stop no longer produces a notification**. The `ended` kind is retired; that message
  reported back an action the owner had just taken.
- A session with several things to say arrives as **one message with bullets**, newest last, up
  to five lines and then `and N earlier.`
- Repeated `finished` reports from a busy agent **space out**, to at most one an hour. The
  doubling backoff had never worked for any kind reporting less often than every four minutes;
  this release is the first in which it does. **Fewer of these is the intended outcome**, not a
  fault — see *The backoff correction* below.

---

# Part 1 — what the host confirms on its own

## The real hook still fires, against the agent as shipped

`tests/live/test_agent_activity_hooks.py` runs a real `claude` turn with the hook installed into
a settings file made for the test. It is the only place a real agent runs with a real hook, and
it is what catches upstream renaming a payload field.

```console
$ REMOTE_AGENTS_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/live/test_agent_activity_hooks.py -q
...                                                                      [100%]
3 passed in 12.61s
```

- **Observed 2026-08-16:** passed. A managed session spools its own `Stop`; a session the service
  did not start spools nothing; the payload field names still match the installed agent.

## The five behaviours, driven end to end with production code

A harness invoked the **installed hook binary** exactly as Claude Code invokes it — same command
from `~/.claude/settings.json`, same JSON on stdin, same `REMOTE_AGENTS_SESSION_ID` gate — against
a temporary spool, then ran the real `drain_activity` and the real `ActivityNotifier` against a
real SQLite session store, with only `send_apart` replaced by a list. The operator's database,
spool and tmux server were untouched; three live managed sessions were on that server at the time.

```text
STEP 1 -- a finished turn reaches the owner
  hook wrote 1 spool record(s)
  PASS: one notification
  message as Telegram would receive it:
    | <b>atlas · claude · regular · #1 · live</b>
    | The agent has finished its work.
    | Ran the suite.
  spool now holds 0 record(s) (drain deletes)

STEP 2 -- pressing Stop produces nothing
  hook still wrote 1 record(s) -- the hook is unchanged
  PASS: no notification, and the record was drained off disk
  spool now holds 0 record(s)

STEP 3 -- one session's several things arrive as one message
  hook wrote 3 spool records
  PASS: one message, three bullets
    | <b>atlas · claude · regular · #1 · live</b>
    | • The agent has finished its work. — Wrote the parser.
    | • The agent is waiting for an answer. — Overwrite config.toml?
    | • The agent stopped after reaching a usage limit.

STEP 4 -- the same thing said twice is shown once
  hook wrote 4 identical spool records
  PASS: one message, one line
    | <b>atlas · claude · regular · #1 · live</b>
    | The agent has finished its work.
    | Ran the suite.

STEP 5 -- a session that has ended is not spoken about
  PASS: no notification for a session in state 'ended'

TELEGRAM CONSTRAINTS -- what the wire would accept
  message 1:   91 UTF-16 units (limit 4096), HTML balanced: True, parse_mode=HTML
  message 2:  211 UTF-16 units (limit 4096), HTML balanced: True, parse_mode=HTML
  message 3:   91 UTF-16 units (limit 4096), HTML balanced: True, parse_mode=HTML
  live view moved to the bottom 3 time(s), once per delivering pass

RESULT: ALL CHECKS PASSED
```

Two readings in there are worth naming rather than leaving to be inferred.

**Step 2 asserts an absence, and the hook is deliberately unchanged.** The record was still
written by the hook and still deleted by the drain — what changed is that nothing is built from
it. A notification appearing there would mean the retirement had not deployed; a *record* still
appearing on disk is correct and is why `BL-001` exists.

**Step 4 renders in the ungrouped shape**, sentence then agent text on its own line, because four
identical reports collapse to one surviving observation and a lone observation reads as it always
did. Bullets appear only where there is more than one line to tell apart.

## The suite

```console
$ .venv/bin/python -m pytest -q
1948 passed, 35 skipped in 330.47s (0:05:30)
$ .venv/bin/ruff check src tests
All checks passed!
```

- **Observed 2026-08-16:** passed, from a 1902-test baseline at `15bcd71`.

## The backoff correction, measured

The doubling backoff had never engaged for any kind reporting less often than every four minutes:
the rate limit's memory was discarded on a horizon computed from the very repeat count it existed
to preserve, so a slow-reporting kind was always treated as first-time. Simulated over eight hours
of 30-second passes, one `Stop` per cadence:

| `Stop` cadence | before | after | taper intends |
|---|---|---|---|
| every 3 min | 12 | 12 | 12 |
| every 4 min | **120** | **12** | 12 |
| every 5 min | **96** | **12** | 12 |
| every 10 min | 48 | 11 | 12 |
| every 30 min | 16 | 9 | 12 |

Found by simulating overnight behaviour at a plan gate, not by reading the code. The unit test
covering that map advanced its clock by exactly the broken horizon and had been green throughout.

---

# Part 2 — what only the owner could establish

Four things, none of which any machine here can establish. All four are covered by one
confirmation rather than four readings — see Status.

## Step A — the service runs this build under systemd

```bash
cd ~/dev/infra/remote-agents
git checkout feat/notification-value-and-grouping
uv sync --locked
uv run --locked remote-agents doctor --json | python -m json.tool
systemctl --user restart remote-agents.service
systemctl --user show remote-agents.service -p MainPID -p ActiveState
ps -o pid,lstart,cmd -p "$(systemctl --user show -p MainPID --value remote-agents.service)"
```

No configuration keys changed in this release, so unlike the 2026-08-11 run there is no
config-schema drift to crash-loop on. `doctor` must still report `"healthy": true` first.

**The `ps` line is not decoration.** `docs/drill-service-restart-session-survival.md` records this
host reporting `active` continuously on 2026-08-16 while running code three sub-plans behind
`main`. If the process start time predates the restart, everything below reads the old build.

- **Expected:** `doctor` healthy, `ActiveState=active`, start time later than the restart.
- **Observed:** covered by the owner's blanket confirmation of 2026-08-16 (see Status). Not
  separately reported; no `ps` output was transcribed here, and none is claimed.

## Step B — a notification is legible on the phone

Launch a managed `claude` session from Telegram and let it complete one turn.

- **Expected:** the message from Step 1 above, rendered — the session's name in bold, the
  sentence, the agent's line, and a single `Open session` button. The menu ends up below it.
- **Observed:** covered by the owner's blanket confirmation of 2026-08-16.

## Step C — the `Open session` button round-trips

Press `Open session` on that notification.

- **Expected:** the session's detail screen renders into the live view, and **the notification is
  deleted from the chat** — it has been acted on. Pressing it does not make it the live view.
- **Observed:** covered by the owner's blanket confirmation of 2026-08-16.

## Step D — pressing Stop is silent

Press Stop on that session and let it end.

- **Expected:** the sessions list shows the outcome as its lead line, as always, and **nothing
  arrives in the chat**. The absence is the assertion.
- **Observed:** covered by the owner's blanket confirmation of 2026-08-16. This is the step the
  confirmation is weakest evidence for, and it is worth saying so: the assertion is an *absence*,
  and an absence is the one thing a reader can satisfy by not noticing. The machine half proves
  the mechanism — `SessionEnd` is drained and dropped, Part 1 Step 2 — so what the owner's
  confirmation adds here is that no *other* path sends a message on stop, which nothing else
  checks.

## Defects this run found

> A run that finds nothing is not evidence that there was nothing to find. Record anything that
> did not work first time, with what was done about it — the 2026-08-11 run found two defects no
> test did, and both are recorded in its own document rather than in a commit message nobody
> reads.

- **Machine half:** none. Every check passed first time.
- **Owner half:** none reported.

Two defects were found *while preparing* this run rather than by it, and both are recorded
because a document that only lists what the final pass saw understates what the exercise cost:

- **`uv sync --locked` failed outright.** The 0.11.0 bump edited `pyproject.toml` and
  `__init__.py` and left `uv.lock` at 0.10.0, so the command the README gives for development and
  the runbook gives before a redeploy stopped with `the lockfile needs to be updated`. Fixed in
  `be980c8`. Found by the owner asking which of the proposed redeploy commands were actually
  necessary — none of the automated checks cover the documented redeploy path.
- **Two of the three redeploy commands first given to the owner were unnecessary**, and one of
  those two was the broken one. `git checkout` named a branch already checked out, and
  `uv sync --locked` is not needed on this host at all: the venv install is editable, so the
  service picks up the code on restart, and no dependency changed in this release.

## Outcome

- **Machine half:** accepted 2026-08-16, recorded from its own output.
- **Owner half:** **accepted**, as a blanket confirmation of the seven-step sequence.
- **By:** the owner.
- **On:** 2026-08-16.

**Release 0.11.0 is accepted for this branch.** What it is not is a claim that four independent
observations were made — see Status.
