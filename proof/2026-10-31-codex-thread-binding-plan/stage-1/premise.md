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
Verdict: holds for plain, `-c` and `--no-daemon` launches; per preset, measured by Task 1.2.

## Premise 2
Assumption: a `!` shell can write outside its workspace under every preset's flags.
Checked: `tests/live/test_codex_thread_binding_live.py`.
Result: pending Task 1.2.
Verdict: not checkable here - this shell cannot create a bwrap namespace; the owner runs it (OD-4).

## Premise 3
Assumption: managed launches with a preset still meet the shared daemon, so the binding is the fix they need.
Checked: `src/remote_agents/domain/profiles.py`: `ask` passes `-c`, and `approve_for_me` passes `--approve-for-me`.
Result: Codex's footer says `-c` means embedded mode. `ask` is therefore probably off the daemon, and the default preset is unknown.
Verdict: not checkable here - Task 1.2 records `daemons`, `marker_seen` and the warning per preset. If managed presets are off the daemon, Remote Control's reach (DEC-071) and this plan's premise go to the owner.
