# Stage 1 demo — 2026-10-31-codex-thread-binding-plan

Environment: read-only review on 2026-10-31 of the owner's native run on codex-cli 0.166.0. Codex was not rerun. Inputs: `/mnt/vault/Portfolio/infra/remote-agents/recovery/thread-binding-premise.json`, `~/.cache/staged-planning-sweep-gate/ra-live-owner-20261031T182317.log`, and the run's leftover files in `/tmp/pytest-of-user/pytest-156/tool-shell0`.

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: `proof/thread-binding-premise.json` shows the `!` shell's thread equals the later turn's thread, under all four presets
Verdict: holds
Evidence:
- Plan goal command exits 0. Task 1.2's test command exits 0.
- The quoted path no longer exists. The plan moved the file to `recovery/thread-binding-premise.json`, and that file is the one read here.
- Log: `24 passed`, then `1 passed, 14 deselected in 163.39s`. The log has no per-preset data.

| Preset | bang_wrote | bang_error | bang_thread == turn_thread | Marker seen | Thread |
|---|---|---|---|---|---|
| preset-approve-for-me | true | empty | yes | false (embedded warning on screen) | 01a19333-f335-7903-aa32-589f746c41a6 |
| preset-ask | true | empty | yes | false (embedded warning on screen) | 01a19334-20c1-7e93-8b67-52d740ba1db7 |
| preset-full-access | true | empty | yes | true | 01a19334-4d1a-7e72-b8f9-2739ad369481 |
| preset-provider-default | true | empty | yes | true | 01a19334-78e3-7c42-b51f-5e95f9112f83 |

- All 7 panes have distinct thread ids.
- The `outside-<name>` file left on disk equals `bang_thread` in all 7 panes.
- Limit: the file was written under `/tmp`, not the activity directory. See M2.

## Outcome 2
> A preset's `!` shell cannot write outside its workspace → the plan stops as a false premise, and the owner sees the preset and the error
Verdict: holds
Evidence:
- This failure did not occur. All four presets and `--sandbox read-only` have `bang_wrote: true` and an empty `bang_error`.
- The failure branch was not exercised on Codex. This verdict rests on the checks below, and it has a defect (M1).
- The preset is visible: each record has `name`, `flags` and `bang_wrote`.
- The stop works at Task 1.2. On a copy with one preset set to `bang_wrote: false`, Task 1.2's test exits 1. The goal gate still exits 0 (M3).
- The error text would not reach `bang_error`. I ran the drill's `bind.sh` line, without Codex, against an unwritable folder. It printed `cannot create ...: Permission denied` to the shell's own stderr, exited 2, and created no `outside-error.txt`.
- The owner would most likely see that text in the record's `screen`, under `You ran sh bind.sh`. That is an inference: no failing screen exists.

## Outcome 3
> A thread holding only a `!` command cannot be resumed → recorded, and Task 3.3 keeps the resume route to threads that had a turn
Verdict: holds
Evidence:
- This failure did not occur. `resume_bang_only.resumed` is true.
- `before` and `after` both hold thread `01a19336-0aff-7303-bd25-7baafaea685e`.
- The screen shows both `!` commands and no turn, so the resumed thread held only `!` commands.
- A reader could see the failure through `resumed`, `after` and `screen`. The shape of a failing record was not observed.
- Task 3.3 has no resume wording. It needs none, because its condition did not arise.
- The rollout file named in the record is no longer on disk, so the record's own fields are the evidence.

## Findings

No Blocking findings.

### Material

- **M1: `bang_error` cannot carry the error it exists for.** `bind.sh` is `printf ... > "<outside>" 2> outside-error.txt`. The shell handles redirects left to right, so a failed `>` stops before `2>` applies. The replay under Outcome 2 shows this. A rerun on a new Codex version (the Preflight's "or the measurements are redone") would show a failed preset with a blank error. Fix: put `2>` first.
- **M2: the write target does not test what the research said Stage 1 would measure.** Research line 21 names the activity directory. The drill wrote a sibling file under `/tmp`. Codex's docs say the sandbox workspace "includes the current directory and temporary directories like `/tmp`" (https://learn.chatgpt.com/docs/agent-approvals-security). So under `ask` (`workspace-write`) the write would succeed even if the `!` shell were sandboxed. The read-only pane supports the premise: its `!` wrote under `/tmp` while its turn failed with "the filesystem is read-only". That is an inference for the other presets, not a measurement. The result file does not record the target path. No later task names a preset for the real `bind-thread` verb. Fix: have a live drill write outside `/tmp` under each preset, in the activity directory.
- **M3: the stage's goal gate does not check the stage's goal.** A copy with one failed preset still passes the gate with exit 0. Only Task 1.2's test catches it. The gate proves one daemon and a control marker, not "under every preset".
- **M4: `one_daemon: true` reads wider than it is.** The approve-for-me and ask records list daemon 3543965, yet their screens say "Running without the shared background server" and their `marker_seen` is false. The field describes the host's daemon, not which sessions use it. The plan's amendment and Premise 3 state this correctly. A reader of the top-level field alone would be misled.

### Minor

- **N1: `after_new.after` did not come from a `!` shell.** The first 32 characters were lost and the agent ran the rest as a prompt, as the plan's amendment says. The record has no field for this and looks like the `before` record. The thread id did change, but that change was read from an agent turn. Task 1.1 calls for "one `!`" after `/new`, and that was not measured.
- **N2: citations point at a log without the facts.** Plan line 58 and Premises 1 and 2 cite `ra-live-owner-20261031T182317.log`, which holds only two pytest summaries. The facts are in the result file. Premise 3 cites `ra-live-owner-20261031T180114.log` for a footer warning, and a search for "embedded", "warning" or "shared background" finds nothing there. That log ends in a failed run. The warning is visible in the result file's screens.
- **N3: the Outcome text was changed without the owner's words.** `premise.md` requires the owner's words for any Outcome change. The plan's gate amendment moved the path in Outcome 1 and quotes none. `premise.md` and this file's quoted line still name the old path. The content is unchanged.
- **N4: `token` is empty in every record.** It reads `RA_BINDING_PROBE`, which was unset. Nothing explains the field to a Stage 2 reader, and the design does not use it.
