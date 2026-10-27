# Stage 2 premises — 2026-10-26-session-defaults-and-project-rollover-sub-01-codex-permissions-plan

Goal: Telegram and terminal Settings and launch/resume flows expose the same persisted choices.
Risk: MEDIUM — stale callbacks and two independent surface processes.

Outcomes:
- Works: Telegram and terminal Settings and launch/resume flows expose the same persisted choices.
- Invalid or unavailable input → a named refusal with existing sessions preserved.
- Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: Both installed surface libraries can render the new control using the locked application environment.
Checked: `.venv/bin/python` reports installed library versions; Task 2.1's real Textual owner journeys and Telegram handler journeys execute in that environment.
Result: Textual 8.2.8 and python-telegram-bot 22.8; the first 17 shared UI journeys passed in 9.15s. Updated SETTINGS and PERMISSIONS SVGs were rendered with librsvg and inspected. This is local synthetic evidence, not a live Telegram or Codex claim.
Verdict: holds for local rendering and application wiring.

## Premise 2
Assumption: Reconciliation is available as part of the complete fixture runtime for interruption observations.
Checked: Stage 1's actual TUI launch/interruption and shipped offline serve startup, independently observed by planning:goal-evaluator.
Result: The complete runtime preserved the original session without a second launch. A TUI-only fixture omitted the documented reconciler and was retained as a failed attempt. The service's Telegram initialization remains unavailable in the offline namespace.
Verdict: holds for local recovery; native provider and Telegram acceptance remain for the final combined gate.
