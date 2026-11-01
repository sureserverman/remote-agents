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
