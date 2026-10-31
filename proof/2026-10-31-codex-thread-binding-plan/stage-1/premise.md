# Stage 1 premises — 2026-10-31-codex-thread-binding-plan

Goal: the owner knows, before any service code changes, that a `!` command can record a binding under every preset.
Risk: MEDIUM - rests on undocumented `!` behaviour in one Codex version

Outcomes:
- Works: `proof/thread-binding-premise.json` shows the `!` shell's thread equals the later turn's thread, under all four presets
- A preset's `!` shell cannot write outside its workspace → the plan stops as a false premise, and the owner sees the preset and the error
- A thread holding only a `!` command cannot be resumed → recorded, and Task 3.3 keeps the resume route to threads that had a turn

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: a `!` shell holds the thread id the later turn uses, on real Codex 0.166.0.
Checked: `run-live-checks.sh token`, `ra-live-owner-20261031T173415.log`, five panes on one home.
Result: bang and turn thread ids are equal in all five panes.
Verdict: holds, also per preset in `ra-live-owner-20261031T182317.log`.

## Premise 2
Assumption: a `!` shell can write outside its workspace under every preset's flags.
Checked: `tests/live/test_codex_thread_binding_live.py`.
Result: `ra-live-owner-20261031T182317.log`: wrote under all four presets and `--sandbox read-only`, error file empty.
Verdict: holds.

## Premise 3
Assumption: managed launches with a preset still meet the shared daemon.
Checked: `ra-live-owner-20261031T180114.log`, footer warning and marker per preset.
Result: `approve_for_me` and `ask` run embedded; `full_access` and `provider_default` share it.
Verdict: false as written. Owner, 2026-10-31: "Keep binding for all presets (Recommended)" and "Measure it in this plan" (OD-6, OD-7).
