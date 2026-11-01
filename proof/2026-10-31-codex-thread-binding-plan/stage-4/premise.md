# Stage 4 premises — 2026-10-31-codex-thread-binding-plan

Goal: the owner has Codex rollover working again on the service they use.
Risk: MEDIUM - spends credits and changes the live service's pin

Outcomes:
- Works: a Codex plan hands off and its fresh successor adopts, in the same project and across projects
- An unmanaged Codex in a managed folder → still exit 3
- Any native case fails → nothing is merged or installed, and the log is reported

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: real Codex at the plan's measured version is installed and logged in.
Checked: `codex --version`; `ls -la ~/.codex/auth.json`.
Result: `codex-cli 0.166.0`; auth.json present, owner-only, dated Oct 29.
Verdict: holds.

## Premise 2
Assumption: the live service exists at the rollback pin, so the install and its rollback are real.
Checked: `~/.local/share/uv/tools/remote-agents/uv-receipt.toml`; `systemctl --user is-active remote-agents`.
Result: receipt pins rev 9aca8e4e04b9; service `active`.
Verdict: holds.

## Premise 3
Assumption: the qualified built writer the Codex drills install is on disk.
Checked: `ls staged-planning-crossroot/.agents/plugins/marketplace.json`; `git log -1` there.
Result: present, at 7f83446 (staged-planning 1.4.2).
Verdict: holds.

## Premise 4
Assumption: `bwrap` can create a namespace, which every native drill needs.
Checked: `bwrap --unshare-pid --ro-bind / / --dev /dev --proc /proc true` in this shell.
Result: `No permissions to create new namespace`, exit 1. Same as earlier stages: the owner's terminal is the route (OD-5).
Verdict: not checkable here — this shell has no user namespaces; the owner's terminal has run them since Stage 1.

## Premise 5
Assumption: Enter on a pasted line this long runs it as a shell command, and `C-u` clears a pasted draft.
Checked: not run here, for the reason in Premise 4. Carried from Stage 3 premises 2 and 3.
Result: unmeasured until Task 4.2.
Verdict: not checkable here — needs the owner's terminal. Task 4.1 writes the cases, Task 4.2 runs them.

## Premise 6
Assumption: the owner's Codex account has room for the set: near a limit Codex raises dialogs a drill cannot pass.
Checked: newest `rate_limits` in `~/.codex/sessions`.
Result: the newest rollout carrying one is months old, so it says nothing about today.
Verdict: not checkable here — no recent reading; Task 4.2's first drill pane shows it, and its screens are read before any code is blamed.

## Premise 7
Assumption: the owner has a Codex Remote Control client to look with at Task 4.4.
Checked: nothing here can see it.
Result: unknown.
Verdict: not checkable here — the owner's device; OD-7 has them say what they see.
