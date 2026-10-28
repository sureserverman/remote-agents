# Stage 2 premises — 2026-10-26-session-defaults-and-project-rollover-sub-03-routing-and-recovery-plan

Goal: A destination session remains discoverable and resumes in its own folder and preset after ending or interruption.
Risk: HIGH — conversation ownership and duplicate work on restart.

Outcomes:
- Works: A destination session remains discoverable and resumes in its own folder and preset after ending or interruption.
- Invalid or unavailable input → a named refusal with existing sessions preserved.
- Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: Existing session_folders and worktree rows survive ending/reopening, and the
locked host runtime can exercise both resume surfaces without a real provider.
Checked: Before implementation, scoped baseline (38 passed in 4.05s), source/schema inspection
of migration21 and session/worktree stores; new requirement suite then demonstrated the
missing behavior (7 failed,43 passed in5.00s; /tmp/routing-task21-red.log).
Result: Persisted session ownership and folder rows exist independently of live panes;
Textual and provider catalogue adapters run over disposable stores. Sandbox executor/Textual
shutdown stalls; narrow elevated runs complete. Initial cache-lock/sandbox failures retained.
Verdict: holds for source integration. Worksheet generated and completed late, after the
first implementation/test cycle; this is a process deviation, not pre-edit worksheet evidence.

## Premise 2
Assumption: A real provider can resume the exact saved conversation in an isolated user route.
Checked: Existing native/Telegram/demo failures and owner deferral in the canonical plan.
Result: No new independent authenticated user route established in this task.
Verdict: not checkable here — native behavior and independent demonstration require their
own route and evaluator; source test success does not satisfy this premise.
