# Acceptance: fewer notifications, and one message per session

Date: 2026-08-16
Release: 0.11.0 — branch `feat/notification-value-and-grouping`, commit `__________` (fill in the
commit actually deployed: `git rev-parse --short HEAD` on the host, after the restart)
Plan: `2026-08-16-notification-value-and-grouping-plan.md`, Task 3.4

> **Status: NOT RUN.** This document is a blank instrument, laid out by the session that wrote the
> code and deliberately carrying no observations. Every "Observed:" line below is empty and stays
> empty until somebody drives the real service and writes what happened.
>
> That is the rule `docs/acceptance-2026-08-11.md` was written to enforce, and it is worth
> restating because this file was created by the party with the most to gain from it reading
> well: an acceptance document that can be satisfied by saying "yes" is not an acceptance
> document, and one pre-filled by the session that wrote the code is worse. If a step was not
> performed, its line says so. If a step was performed and the result was wrong, its line says
> that — a run that finds nothing is not evidence that there was nothing to find.
>
> The environment this branch was built in had no systemd user session and no Telegram
> credentials, so none of the below could be performed there. That was recorded as a Preflight
> failure before Stage 1 began rather than discovered at the end, and nothing weaker was
> substituted for it.

## What changed, in the owner's terms

The service used to send one message per observation, about any session, for six kinds of event.
It now sends **one message per session per delivery pass**, only about a session that is still
live, and only for events that carry something to do about them.

Concretely, three differences the owner should be able to see:

- Pressing **Stop no longer produces a notification**. The `ended` kind is retired; that message
  reported back an action the owner had just taken.
- A session with several things to say arrives as **one message with bullets**, newest last, up
  to five lines and then `and N earlier.`
- Repeated `finished` reports from a busy agent **space out**, to at most one an hour. The
  doubling backoff had never worked for any kind reporting less often than every four minutes;
  this release is the first in which it does. Fewer of these is the intended outcome, not a fault.

## Preconditions

```bash
cd ~/dev/infra/remote-agents
git checkout feat/notification-value-and-grouping
uv sync --locked
uv run --locked remote-agents doctor --json | python -m json.tool
systemctl --user restart remote-agents.service
systemctl --user show remote-agents.service -p MainPID -p ActiveState
ps -o pid,lstart,cmd -p "$(systemctl --user show -p MainPID --value remote-agents.service)"
```

No configuration keys changed in this release, so — unlike the 2026-08-11 run — there is no
config-schema drift to crash-loop on. `doctor` must still report `"healthy": true` first.

**The `ps` line is not decoration.** `docs/drill-service-restart-session-survival.md` records a
case on 2026-08-16 where the unit reported `active` continuously while running code three
sub-plans behind `main`. If the process start time predates the restart above, the restart did not
take and everything below is a reading of the old build.

- **Expected:** `doctor` healthy, `ActiveState=active`, and a process start time later than the
  restart.
- **Observed:**

## Step 1 — a finished turn still reaches the owner

Launch a managed `claude` session from Telegram and let it complete one turn.

- **Expected:** a notification naming the session, reading "The agent has finished its work.",
  carrying at most one bounded line of what the agent last said, and a single `Open session`
  button. The live view ends up below it.
- **Observed:**

## Step 2 — pressing Stop produces nothing

Press Stop on that session and let it end.

- **Expected:** **no notification at all.** The sessions list shows the outcome as its lead line,
  as it always has. Nothing arrives in the chat about the session having ended.
- **Why the absence is the assertion:** `SessionEnd` still fires and its record is still spooled
  and drained off disk — what changed is that nothing is built from it. A message here means the
  retirement did not deploy.
- **Observed:**

## Step 3 — one session's several things arrive as one message

Get a single session to produce two or more observations inside one 30-second poll. The
reliable way is a turn that ends and immediately asks for permission, so `Stop` and
`Notification` spool together.

- **Expected:** **one** message, headed by the session's name, with two `•` lines — not two
  messages. Each line carries its own sentence and, after an em-dash, what the agent said.
- **Observed:**

## Step 4 — the same thing said twice is shown once

Let an agent report the identical thing twice inside one pass (a `Stop` hook fires per turn, so a
long instruction does this on its own).

- **Expected:** one line, bearing the **later** of the two times.
- **Observed:**

## Step 5 — a session that has ended is not spoken about

While a session is stopping, or just after it has ended, let a spooled record for it arrive.

- **Expected:** no notification. The journal shows `not notifying about a session that is no
  longer running`, naming the state.
- **How to see it:** `journalctl --user -u remote-agents.service -n 100 --no-pager`
- **Observed:**

## What the host confirms on its own

Independent of anything the owner reads in the chat:

```bash
journalctl --user -u remote-agents.service --since "1 hour ago" --no-pager \
  | grep -E 'not notifying|will not speak about|holding [0-9]+ undelivered'
ls -la ~/.local/state/remote-agents/activity/ 2>/dev/null
```

- **Expected:** the spool directory is empty or nearly so between passes — every record is
  deleted as it is drained, whether or not anything was built from it. `holding N undelivered`
  should be absent on a healthy host; it now fires only when a pass delivered nothing at all.
- **Observed:**

## Defects this run found

> A run that finds nothing is not evidence that there was nothing to find. Record anything that
> did not work first time here, with what was done about it — the 2026-08-11 run found two
> defects that no test did, and both are recorded in its own document rather than in a commit
> message nobody reads.

- **Observed:**

## Outcome

- **Accepted / rejected:**
- **By:**
- **On:**
