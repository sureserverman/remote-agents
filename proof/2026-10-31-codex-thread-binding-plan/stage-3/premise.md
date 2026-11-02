# Stage 3 premises — 2026-10-31-codex-thread-binding-plan

Goal: the owner launches or rolls over a Codex session on either surface and it is bound with nothing to do.
Risk: HIGH - types into a pane, and orders itself against the adoption template

Outcomes:
- Works: the session is bound within seconds of its composer, and the pane shows one `!` line
- Held on a trust dialog → nothing is typed until the owner answers, then it binds
- Binding not seen in time, owner's launch → the session stays usable, and the owner gets one notice saying its handoffs will refuse
- Binding not seen in time, rollover successor → the rollover fails `binding-failed`, the predecessor is kept, and the owner sees why
- Owner already typing in the pane → nothing is overwritten; the bind waits for an idle, empty composer, then falls to the not-seen case
- Service restarts mid-bind → the pending file stays, the next pass retries once with a fresh nonce, and a bound thread is not rebound
- A dialog holds the keys, or the start of the line is lost, so it would reach the agent as a prompt → the line is not submitted, the composer is cleared, and it falls to the not-seen case
- Claude, OpenCode, Cursor → no typing and no file

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: Codex draws a pasted `!` line in a form the declared `shell` pattern reads, wrapped.
Checked: `in_shell_mode` on the 12 screens in `recovery/thread-binding-line-routes.json`.
Result: true on all 8 fresh screens, typed and pasted, `--approve-for-me` included; false on the 4 after `/new`.
Verdict: holds.

## Premise 2
Assumption: pressing Enter on a line this long runs it as a shell command.
Checked: not run. The drill's matcher was wrong, so Enter was never pressed, and OD-4's reruns are used.
Result: only short `!` lines have been seen to run.
Verdict: not checkable here. Task 4.1 proves it on the paste route, per preset.

## Premise 3
Assumption: `C-u` clears a pasted draft from the composer.
Checked: not run on a real pane.
Result: unmeasured. A failure leaves a draft, which later sends refuse as `composing`; nothing is submitted.
Verdict: not checkable here. Task 4.1 covers it.
