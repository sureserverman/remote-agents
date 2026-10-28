# Project routing Stage 1 gate evidence

2026-10-28. Worktree `/tmp/remote-agents-project-routing`, branch
`session-defaults-project-routing`. Canonical plan: Portfolio/infra/remote-agents/plans/
2026-10-26-session-defaults-and-project-rollover-sub-03-routing-and-recovery-plan.md.

Stage 1 has a **source gate pass with its independent user-route demo deferred by the
owner** on 2026-10-28 (OD-1). The exact reply was "1", selecting "Defer it to the final
combined gate and continue source work." The failed demo remains failed evidence; its
three Outcomes must be demonstrated at the final combined gate. Stage 2 has not started
at this checkpoint. The implementation and local source checks are complete. Native provider
acceptance and the previously deferred Telegram demonstration remain unqualified.

## Source and verification

- `7674d2f5`: exact recorded thread ownership; fresh unbound Codex identity refuses.
- `4e70b46a`: frozen project, folder, branch, source channel and permission preset.
- `97ff2bcc`: explicit source-root adoption, exact finished-request correlation, nonfailure
  workflow completion, and shared destination/wait/refusal text. Migration 24 preserves
  rollover rows, rowids, history and constraints; old launches retain v1 request capability.
- `b7989f00`: add the missing workflow_finished literal to the exhaustive view test table.
- `5a6ef1e9`: turn destination canonicalization/inspection errors into named refusals.

Evidence ran on the host with disposable repositories, SQLite databases, fake terminals,
real isolated tmux toy providers, and actual built Claude/Codex planning writers from
`/tmp/staged-planning-session-defaults` at `a3673b1130d69e7dc128c40aa8c87186013e93b6`.
This proves local wiring and state; toy providers are unproven against native providers.

| Check | Observed result |
| --- | --- |
| Task 1.1 | 105 passed; bound mutation proof; final required task review approved |
| Task 1.2 original | 161 passed; bound mutation proof; required re-review approved |
| Task 1.3 | 485 passed, 1 existing ORPHANED parity skip, 57.50s; bound mutation proof; required task review approved without findings |
| Stage integration: cross_project_rollover, handoff_envelope_compat, rollover_recovery | 164 passed, no skips, 39.89s |
| Stage regression entry: rollover domain/application/store/service/notifications, affected profile/store pins, session rows, architecture and isolated terminal/Telegram fake e2e | 1071 passed, 1 missing test-expectation literal failed, 1 existing ORPHANED parity skip, 75.94s |
| Expectation repair: session_views, rollover domain, session_row_parity | 516 passed, 1 existing ORPHANED parity skip, 3.43s |
| Final path-refusal fix scope: cross_project_rollover, rollover_store, handoff_envelope_compat | 224 passed, no skips, 51.39s |
| Refusal mutation | 24 failed, 200 passed, 51.10s; exact staged bytes restored |
| Record audit after declaration amendment format repair | No findings; checks 1 through 10 ran |

The stage-scope regression entry was not re-bought after repairs. Its result stands with the
focused repair checks, per the execution protocol. Failed runs, original proof and mutation
captures remain under `/tmp/routing-stage1-*.log` and `.claude/proof-attempts/` in this worktree.
They are not replaced by the passing results.

Class sweeps:

```
rg -n -A12 'RolloverState.CANCELLED:|S.CANCELLED:|len\(RolloverState\)' src tests
rg -n '\.resolve\(' src/remote_agents/application/rollover_destinations.py \
  src/remote_agents/application/services.py src/remote_agents/adapters/workflow/destinations.py
```

The first found the product map, domain count and sibling expectation table. Only the sibling
expectation literal was missing. The second found candidate canonicalization and Git inspection
under the routing boundary; folder launch binding already catches faults durably. Both v1 and
v2 inspection failures now get the same named refusal. Python 3.12's real symlink cycle raised
an uncaught RuntimeError before repair and returned `destination-root` after repair. Portable
fault-injection cases cover project/worktree paths before and after freezing, plus both wire
versions' inspection errors, without relying on Python 3.14's different resolve behavior.

## Independent review and demo

Review scope: high, explicitly amended from standard for identity and schema/ownership paths.
Dispatch: 0 of 0 implementation YES tasks; all three implementation tasks ran inline as NO.
No implementation delegation occurred.

- Tier 1: routing_task11_final_review, routing_task12_rereview, routing_task13_review,
  all planning:code-reviewer, approved their respective final task diffs. Failed earlier
  reviews and their repairs remain in the canonical execution history.
- Tier 2 A: routing_stage1_review_a, planning:code-reviewer, reviewed
  `18df0f90..b7989f00`: no Critical, one Important unavailable-path refusal gap. That existing
  function was repaired in 5a6ef1e9 and verified at fix scope. No new function or redesign was
  introduced. The execution protocol closes Important repairs on their focused evidence;
  this is not a claim that reviewer A issued a later approval.
- Tier 2 B: routing_stage1_review_b, planning:code-reviewer, independently reviewed the same
  range: APPROVE, no findings. Both reviewers were dispatched before the first batch wait.
- Evaluator: routing_stage1_demo, planning:goal-evaluator, received the generated stage brief
  unchanged. Initial CLI-only evidence was preserved, then followed up with actual PTY/tmux
  interaction. All three Outcomes remain `cannot reach by the user's route` and Blocking.

The isolated dashboard showed `No sessions are running.`, `Projects 0 · recent first`, and
`No project matches that filter.` It exposed help/add-project/quit/palette actions, but no
selected managed session for rollover. Sending the rollover key left that empty state.
Ctrl-C displayed quit confirmation; Ctrl-Q stopped the isolated socket. This is an unavailable
demo environment within the authorized boundary, not proof that the product lacks a route.
The config-path refusal seen earlier was not a handoff refusal and is not counted as one.

The full failed demo is preserved in the committed Stage 1 demo worksheet. Source tests,
imports and the synthetic integration were not substituted for that user-route demonstration.
No normal configuration, trust, provider/model selection, installed service, production
session or external Telegram route was changed.

## Remaining gate obligation

The independent user-route demo needs either an authorized usable isolated provider/session
route now or the owner's explicit deferral/opt-out for this Stage 1 demo. Existing native and
Telegram deferrals cover the final combined gate; owner-decision matching found no applicable
entry for this additional Stage 1 obligation. The execution skill's Outcome stop condition
requires an owner decision before marking Stage 1 green or starting Stage 2.

One remediation round was reserved for this gate, within its two-round budget. Its source
repairs are verified; the demo is the remaining Blocking condition. This is not budget
exhaustion, a measured context handoff, a completed sub-plan, or master completion.

## Owner resolution, 2026-10-28

The owner replied "1" to the two choices recorded at the previous stop, selecting
"Defer it to the final combined gate and continue source work." This resolves the
Stage 1 demo blocker for source continuation. It does not supply a successful demo or
waive the final demonstration. Canonical OD-1 and the demo worksheet's Not demoed section
retain the reply, selected option text and unchanged failed observations. The completed
round's failed assessment remains in the owned controller history; this later owner
resolution does not replace that receipt.
