# Stage 1 premises — 2026-10-26-session-defaults-and-project-rollover-sub-03-routing-and-recovery-plan

Goal: A ready handoff starts at most one successor under the correct destination project and folder.
Risk: HIGH — cross-project attribution and stop authority.

Outcomes:
- Works: A ready handoff starts at most one successor under the correct destination project and folder.
- Invalid or unavailable input → a named refusal with existing sessions preserved.
- Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: The qualified built writer can interoperate with existing same-root reader and persisted launch paths before the v2 receiver is implemented.
Checked: RA_STAGED_PLANNING qualified checkout; preflight real built-writer compatibility, rollover service/recovery, permission integrations and architecture baseline.
Result: 479 passed, 1 pre-existing parity skip in 63.60s; writer tests did not skip. Both repo and disposable SQLite fixtures usable; migration head22.
Verdict: holds.

## Premise 2
Assumption: A live provider thread can be bound unambiguously to its managed session when its shell changes folder.
Checked: Existing hook_identity, activity_spool, turn/conversation mark and roster implementations read; existing live drill collects3cases and has not run. Shared-daemon inherited id is explicitly not evidence. Strict exact-binding source tests now cover missing/stale/duplicate/forged thread and root claims.
Result: Existing exact resume identifiers are available to the source resolver; fresh hook-created turn/conversation marks are circular evidence, confirmed by an unmanaged same-folder real-hook reproduction. Such fresh bindings must refuse. Their hook bootstrap uses folder/time heuristics and is not authenticated native ownership. Current installed-provider behavior must be measured by the final gate; no native call executed during source work.
Verdict: not checkable here — live execution reserved for final combined gate; native identity claim remains unqualified.

## Premise 3
Assumption: Persisted source/destination records and acceptance can preserve exactly-once launch and idle-only stop.
Checked: Preflight isolated rollover service/recovery tests and current durable store schema.
Result: Existing v1 controls passed; v2 and cross-project invariants are Task1.2/1.3 deliverables, never assumed from those controls.
Verdict: holds for the baseline; new behavior requires stage proof.
