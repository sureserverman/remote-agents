# Acceptance — F10 closes the console, and the bot reports an early limits reset

Date: 2026-09-18
Plan: `2026-09-18-console-close-and-early-limit-reset-plan.md`
Decisions recorded: **DEC-096** (F10 under console hosting closes the whole console),
**DEC-097** (one account-level notification kind, amending DEC-031)
Closes: **BL-097**

Released as **v0.44.0**, then **v0.44.1** and **v0.44.2** — see *Three releases, and why* below.
Every figure here was read off the owner's own host after the deploy; nothing in this document
is copied from a test run.

---

## What was accepted

### 1. `console close` removes the console and leaves every agent running

Driven three times on the live host, once per release, each time with **an agent displayed** —
the hazard case, since under DEC-040 a displayed agent's pane lives in the console window and
would die with the session.

| release | displayed agent's pane pid | `has-session ra-console` after | that agent's process | managed sessions |
|---|---|---|---|---|
| v0.44.0 | 85215 (`codex`) | `can't find session: ra-console` | **alive** | 4 → 4 |
| v0.44.1 | 3713435 (`claude`) | `can't find session: ra-console` | **alive** | 4 → 4 |
| v0.44.2 | 3618420 (`claude`) | `can't find session: ra-console` | **alive** | 4 → 4 |

`remote-agents console close` exited 0 on all three. The managed session set was byte-identical
before and after all three closes and all three service restarts:

```
ra-65d2a031-7f8c-40a9-b1ce-6e9e228d0ef0
ra-cf3e6f9c-94c9-4978-bf60-2134a68c3141
ra-f336a47f-426f-4cb1-b846-677e6fe67ff2
ra-f9394608-a6c5-457b-b406-89ea871b4b64
```

All four agent processes — 85215, 3995041, 3618420, 3713435 — were alive at the end.

**Re-entry rebuilt every surface pane.** Before the deploy the four surface panes had started at
06:08:13–14; after the final upgrade at 22:36:34 they started at **22:36:42**, all four, marked
`surface`, `sessions`, `limits`, `feed`.

**The deploy step this retires.** The surface-side half of a deploy used to be "respawn the four
surface panes by process". It was `console close` + re-entry here, three times.

### 2. The limits watch runs on the real host and its first reading is not a false alarm

Service restarted 22:36:34. The interval is 300 s. At **22:41:36**, one interval later:

```
INFO:...limit_reset_notifications:limits watch: baseline taken for claude (2 window(s)); nothing to report
INFO:...limit_reset_notifications:limits watch: baseline taken for codex (2 window(s)); nothing to report
INFO:...limit_reset_notifications:limits watch: baseline taken for opencode (0 window(s)); nothing to report
INFO:...limit_reset_notifications:limits watch: baseline taken for cursor-agent (0 window(s)); nothing to report
```

Counted over the same window: **0** watch failures, **0** messages sent, and **0** `HTTP Request`
lines — the last confirming that lifting only the `remote_agents` logger keeps httpx's
per-request chatter out of the journal.

`opencode` and `cursor-agent` publish no windows and are logged at 0. The line is emitted per
provider read rather than per provider *with a limits capability*, which is marginally wider
than the plan's wording and is honest about what it found.

---

## A real early reset was NOT observed, and this is what stands in for it

**No provider wiped its meters early during this acceptance.** That event cannot be produced on
demand — it is Anthropic's or OpenAI's to cause — so the feature's central claim is **not**
demonstrated live here, and this document does not pretend otherwise.

What is demonstrated instead, in descending order of directness:

1. **The live baseline above.** The loop runs on the owner's host, reads all four providers, and
   reports nothing from the first reading. The half of the feature that runs every five minutes
   forever is proven in production; the half that fires rarely is not.
2. **The service-level test** — `test_the_limits_watch_reports_one_early_reset_and_then_stops_talking`
   drives the real `_serve_with_reconciliation` with a fake backend whose figures drop between
   two ticks, and asserts exactly one message reaches a recording bot and that a further tick
   adds none.
3. **The detector's own table** — `tests/unit/application/test_limit_resets.py`, 24 test
   functions including a 144-pairing sweep asserting the rule never raises on any pairing of
   twelve readings.

**Two accepted costs, restated because they are real:**

- An early reset in the minutes around a service restart is **missed**. The baseline is in
  memory and does not survive the process. Missed, never invented — every guard in the rule
  fails towards silence, because a false "your limits were reset" is the one an owner would act
  on.
- With `claude_limits_source = status line` — which is what this host runs, confirmed by
  `doctor` at acceptance time — an early Claude reset is noticed only when the next Claude
  session reports, because that is when the figure moves at all. Codex has no equivalent gap:
  its figures are asked of `codex app-server` every pass.
- **A provider that has been unreadable for hours looks like a provider with nothing to say.**
  The baseline line fires once, when a provider's baseline is *first* taken, and a failed read
  never touches the baseline — so a provider that goes unreadable and later recovers logs
  nothing on either transition. The startup lines still prove the watch is alive as a whole,
  which is what the three releases above were about; what they do not distinguish is "alive,
  but this one provider has answered nothing since 04:00". Raised by Stage 3's own Tier-2
  review, and named here rather than fixed: closing it means logging per failed read, and the
  failure this feature is arranged against is a false alarm, not a missed line.

---

## Three releases, and why

This was meant to be one release. It became three, and both extra tags were caused by the same
gap, found by the same gate check.

- **v0.44.0** — the plan's release. Its live gate asked the journal to show the limits watch
  taking its baseline. The watch logged only on failure, so a healthy pass was silent: on a real
  host "the loop ran and found nothing" and "the loop was never created" produced identical
  journals. The check could not be satisfied.
- **v0.44.1** — added the baseline line. The check *still* failed. The line was emitted and
  dropped: nothing in `src/` configured logging, so the root logger sat at its default WARNING
  and every `_LOG.info` in this codebase was unreachable on the deployed service — 18 of them,
  plus 35 `_LOG.debug`. `application/console.py` had said so in prose for months; what was
  missing was anybody needing it to be false.
- **v0.44.2** — `_serve` now attaches a handler, holds root at WARNING and lifts `remote_agents`
  to INFO. Verified in a fresh process before shipping, which is the step v0.44.1 skipped.

The tags were not moved. A pushed tag is not deleted (DEC-057), so each fix is its own release.

**What this cost, and what it bought.** Two extra releases and two extra restarts of a live
service, to make one line visible. It bought the only thing that distinguishes a working watch
from an absent one on a host nobody is running tests against — and the same ambiguity had
already been caught once by a surviving mutant in the unit tests, fixed there, and left standing
in production. The live gate is what found the production half.

---

## Also found by this plan's own gates, and fixed

- **A wired capability could take the surface down.** `action_quit` awaited the detached-closer
  spawn unguarded; `EMFILE`, `ENOMEM` or a moved executable would have crashed the pane — BL-097
  again, through a new door, and worse, since the original was a key doing the wrong thing
  rather than a surface dying. Swept by AST across all eight capabilities wired onto
  `TuiContext`; one instance.
- **DEC-049 was cited and not implemented.** `LimitResetNotifier` struck on every refusal rather
  than only when another send in the same pass had proved the channel live. With a 300 s
  cadence, a provider-keyed `_abandoned` set and nothing clearing it, one ordinary Telegram
  outage would have disabled early-reset notifications for that provider for the life of the
  process.
- **Two module comments asserted opposite orderings of the same pair of constants**, both five
  minutes, both false. The grace was never what silences a scheduled rollover — that is already
  false at grace zero, because the published instant has passed by the time the drop is visible.

---

## Open, and deliberately not fixed here

- Two real-tmux tests skip under `pytest -n auto` and run in a serial command. The proximate
  cause is known — `gateway.py` counts `"error connecting to"` as an absent server — but why the
  socket becomes unreachable inside an xdist worker is **not** diagnosed, so nothing was changed
  to make them green.
- Nothing runs `ruff`: not CI, not the plan's declared test commands.
- On re-entry the console displays whichever session the selection option names, rather than the
  projects surface. Pre-existing entry behaviour, observed three times here, unrelated to this
  change.
- `ProviderDescriptor` carries no display name, so the presenter is handed one instead.
- The comment above `_ACTIVITY_POLL_SECONDS` describes a shutdown grace, not a poll period. The
  runbook's per-notification opener and README's "those four are the whole vocabulary" are now
  narrower than what the service sends.

---

## Close-out still owed by the owner

Press **F10** on the real console once with an agent displayed and once from a pane, and confirm
both return the shell with every agent still listed by the bot. This session proved the verb and
the forwarded key on a disposable server and proved `console close` on the live one; only the
owner's hands prove the owner's terminal.
