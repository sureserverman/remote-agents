# Original final round5: binding crash recovery

Base: a5da12cd, branch session-defaults-project-routing. This evidence belongs to the commit containing it. Owner OD-4 approved only this recovery class within the already-reserved fifth round. Four earlier assessments and ceiling5 remain unchanged.

## Defect and repair

A real child exiting86 after claiming a nonce but before publishing its thread left no pending state; the restarted binder sent no retry and no notice. The repair keeps the claim until publication and recovers its exact first/retry marker. A committed thread is recognized independently of temporary roster/own-mark vetoes, so it is preserved and never retyped.

A private0600no-follow/single-link flock and reentrant process lock serialize every mutation, inspection and pruning operation. Nonce replacement cannot overtake an in-flight publisher. Live claims survive age cleanup; ambiguous, malformed or unreadable claims refuse all mutations before any pending or published evidence changes. The only new persistent artifact is thread-binding/.lock; it holds no nonce or identity payload.

## Verification

- Initial real child crash/lock/corruption controls:9failed1passed on the original source.
- After-publication restart controls, with missing roster and own-mark veto:2failed before their repair.
- Corrupt-claim mutator matrix (mint/request/give-up/consume):12failed before its repair; fixed controls preserve original pending bytes and published bindings through prune.
- Focused store/restarted-binder/exact-identity controls:198passed1.78s.
- Affected architecture, binding transport, identity and original five-project/permission integration:658passed92.72s.
- Security handoff/surface checks:137passed0.68s.
- Final Ruff check and format:passed,801files formatted.
- Cross-process held publisher/replacement test uses process handles and owned temporary files; it proves the fresh nonce waits and revokes only after publication completes.
- Reviewer recovery_boundary_review: after-publication Important closed/APPROVE at its exact fix scope.
- Reviewer routing_stage3_final_review_b: blind-retirement Important closed/APPROVE at its exact fix scope; no Critical/Important/Suggestion remains in that delta.

Every child exits or is killed and waited by its owning test. No live provider/model turn, normal credentials, production service or hook/profile setting was changed by these source checks. These tests exercise abrupt process exit and reconstruction of the real binder, not a live Telegram route.

The existing native12/binding5 receipts remain attributed to application basea5da12cd/d0e5ecbc; this source repair is a new delta verified at fix scope. No claim is made that the paid native bundle ran on this patch. The installed service still has the old implementation until separately authorized delivery.

## Remaining original acceptance

The independent evaluator's original Outcomes remain unreachable within its permitted user-route tools. In particular, actual existing-bot stale/unavailable refusal and interruption/no-duplicate observations are still missing. Earlier owner Settings/override passes do not replace them. No Stage3/master completion, install, push, budget reset or extra round is claimed here. BL-147's host sandbox limitation remains separately recorded.
