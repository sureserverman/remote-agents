# Thread binding: Stage 4, remediation round 1

Base: 49b0fb57, branch codex-thread-binding. This report belongs to the commit containing it.
Owned run: 3b1ccfe8-dcea-43b6-ae28-b50eec64056a. Round 1 of 2 is active. The original routing run and its four spent rounds are unchanged.

## Repairs

- The shared Backend inventory now explicitly includes unbound_watch, as the binding composition and architecture already require. The sealed equality assertion is retained. RED: 1 failed, 9 passed; affected inventory/composition/architecture/notice checks: 98 passed.
- CatalogueGap uses one daemon worker rather than asyncio's default executor. Its wait remains bounded, it starts no second read while one is stuck, and a hung mount cannot hold process shutdown. Late errors are logged once in the worker, including after a timeout or loop closure; completion consumes the future exception. RED: late-error and subprocess shutdown controls failed; fixed catalogue suite: 22 passed. On Python 3.14, a shielded future also logged the late exception, so the bounded wait uses asyncio.wait instead.
- The token design probe requires a new output file before its first pane. The file is created exclusive and mode0600; observations are flushed and fsynced individually. Partial results survive a later failure, and existing files/symlinks are refused without overwriting evidence. Four local tests pass. The owner's token runner now chooses a timestamped output. No token/native run was repeated; only the plain control is asserted, other results remain design observations.

Class sweeps: rg for Backend.__dataclass_fields__/fields(Backend)/unbound_watch over tests; rg for CatalogueGap over src/tests; rg for RA_TOKEN_PROBE_OUT over tests and the named owner runner. The missing inventory was the only unmatched shared capability. CatalogueGap is the affected timeout worker. The one token diagnostic was the optional-output instance.

## Verification

Host source/fixture evidence, not installed-service evidence:

- Catalogue/context/lifecycle: 67 passed.
- Complete architecture suite plus catalogue, artifact, context and lifecycle checks: 399 passed in68.65s.
- Ruff check: passed; format check:799 files already formatted.
- Live acceptance collection:32 selected,80 deselected; collection started no provider.
- Owner runner bash syntax: passed.
- Independent planning:code-reviewer recovery_boundary_review: APPROVE,0Critical/Important, scoped active diff against49b0fb57.
- Independent planning:code-reviewer routing_stage3_final_review_b: APPROVE,0Critical/Important for CatalogueGap and its tests. Suggestion retained: internal timeout flag is not currently exposed as health status; warning logs remain observable.

The initial local native-isolation selection reported3failed32passed because this agent shell cannot create a bwrap namespace; the failures occur before application code. They are not passing native evidence. No sandbox, service, profile, account or hook configuration was changed to bypass it.

## External runs and limits

The completed owner-terminal full suite ra-plan-scope-20261101T163412.log reported6failed9812passed114skipped1error. The inventory defect is repaired above. Its remaining live-provider failures and ownership-cleanup error must be classified from their actual results, not replaced with fixture passes.

At report writing the owner's repeated full suite ra-plan-scope-20261101T171832.log is still running. It began on49b0fb57 before these repairs; record its completed result separately and use the documented fix-scope checks for this patch. No extra full-suite run was launched by the executor.

Owner-terminal ra-live-owner-20261101T170302.log reports5passed16deselected82.40s for the changed binding proof. This is a binding proof, not a new complete native identity/rollover/adoption/restart bundle. Prior native and installed owner-smoke evidence remains in the binding plan and stage4/demo.md with its original commit attribution.

Stage4 green, binding close-out, merge/push, and the original master close-out remain pending the final gate assessment. This repair is not installed.

## Completed external run and follow-up repairs

Owner full suite171832 completed9failed9809passed114skipped1error in1688.99s. The subsequent owner binding-proof174644 passed5with16deselected in85.25s. The full run is not green. Its startup tree was49b0fb57; source/fix-scope work overlapped it, so no final-tree whole-suite claim is made.

The executor should have serialized its tmux-backed checks. Investigation found a real harness defect: cleanup considered every new test socket its own, including another independent run's later server. Thus disappearing-socket failures during the overlap are contaminated, not proven product defects. New cleanup requires the server's inherited pytest owner marker to equal this pytest PID; uncertain/foreign ownership refuses cleanup. Real independent-child controls went1FAIL2PASS before the fix to3PASS after; own cleanup and the existing nested-child guard remain tested. Reviewer routing_stage3_final_review_b APPROVE with no findings on this delta.

The owner log also retained an actual Cursor2026.10.22 idle screen whose new block separator broke the strict composer matcher. Captured verbatim as idle_20261022.txt: RED1FAIL2PASS for its matrix checks. The vertical now permits only the measured whitespace/block-glyph row before its existing footer, preserving busy/dialog predicates. Full saved-capture matrix plus ownership and status-bar checks:389PASS8.75s, serialized. The isolated status-bar file also passed64checks; neither pass substitutes for native steady-pane behavior.

Remaining native failures are explicit: three Codex prompt-relay cases stopped at Hooks need review because the plain full-suite launcher omitted the already-approved disposable-home opt-in; the Cursor stop case remained at Trusting workspace; the old cleanup refused an unreadable same-user process and retained its home. Do not bypass ownership to make cleanup green. The owner's final-check script now supplies REMOTE_AGENTS_LIVE_HOOK_TRUST=owner-approved (existing recorded authorization). No hook setting or native run was changed automatically.

Next verification must run from the owner's normal terminal, serially, on the committed repaired tree: ~/.cache/staged-planning-sweep-gate/run-stage4-final.sh. This agent session cannot create the required bwrap namespace. The active gate round stays reserved while awaiting that verification. No merge, push or installation is performed from a failed gate.

## Clean owner run at 7cdcdebf

With nothing else running: 3 failed, 9826 passed, 114 skipped, no errors, binding proof 5 passed, isolation 28 passed. Every failure in the earlier live, e2e and status-bar set passed, including real Codex and Cursor turns.

The three failures were in the tests, not the product. `conftest` now reads a server's owner marker, and one architecture test's fake `subprocess.run` gave it none. Two cases that assert the refusal without the owner's hook-trust opt-in read it from the environment, and the owner's runner now sets it for the whole suite. Those two now clear it themselves. Sweep: `grep -rn "hook_trust_approved\|answer_hook_review\|HOOK_TRUST" tests` and `grep -rln "pytest_sessionfinish" tests`; no other member. The three files pass with the opt-in set and unset.
