# Stage 3 premises — 2026-10-26-session-defaults-and-project-rollover-sub-03-routing-and-recovery-plan

Goal: The owner can run a five-project master plan through rollovers and restart without manually fixing project or permission selection.
Risk: MEDIUM — real provider version and installed-writer drift can invalidate synthetic expectations.

Outcomes:
- Works: The owner can run a five-project master plan through rollovers and restart without manually fixing project or permission selection.
- Invalid or unavailable input → a named refusal with existing sessions preserved.
- Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: Both qualified built planning writers can emit actual v2 destinations into owned external-record fixtures.
Checked: RA_STAGED_PLANNING=/tmp/staged-planning-session-defaults uv run --locked pytest tests/integration/test_cross_project_rollover.py -k actual_writer_v2_destination -q
Result: 2passed134deselected0.64s, using both built host writers. Source checkout remains qualified a3673b1.
Verdict: holds

## Premise 2
Assumption: The proposed live drills can be collected without executing providers or reading login contents.
Checked: uv run --locked pytest tests/live/test_codex_permission_presets.py tests/live/test_codex_tool_shell_identity.py tests/live/test_rollover_live.py tests/live/test_restart_resume_live.py --collect-only -q
Result: 11cases collected0.28s. Audit found identity cases missing live_acceptance marks, a pre-existing deferred surviving-turn skip, and writable auth symlinks in three older drills. Task3.1 explicitly requires auditing/repairing these owned fixtures before any live execution; collection alone is not native acceptance.
Verdict: holds for collection only; live execution not checkable here until fixture repairs and final prerequisites are verified

## Premise 3
Assumption: An authorized isolated Codex login route is available without changing the owner's profile or credentials.
Checked: Stage2 actual native public TUI observations plus lifecycle recovery scan, preserved in committed stage2demo and acceptance report.
Result: Codex0.165.0 read-only bind mount worked; no credential copy, zero owned live processes after clients closed. Private home retained after original controller descriptor loss. This does not establish Claude, Telegram, all presets, identity hooks or rollover adoption.
Verdict: holds for the already-authorized Codex mechanism; remaining native/provider/Telegram environments not checkable here until final gate


## Premise 4
Assumption: The final native/provider, fresh-Codex-identity and independent Telegram routes can demonstrate the full owner journey.
Checked: Existing Codex/Claude auth metadata (regular files, contents not read); prepared native collection12selected/13total; source capability/routing constraints and prior owner authorization.
Result: Provider file-auth prerequisites exist, but complete native effective-policy/identity/adoption/restart behavior is unmeasured. Fresh unbound Codex writers deliberately refuse without independent managed conversation evidence. No isolated Telegram user route or external send was authorized. These are retained final-gate obligations, not source passes. Added at the final gate after Task3.1/3.2; the original pre-implementation premises and Outcomes above remain unchanged.
Verdict: not checkable here — requires the final actual native measurements and an authorized isolated Telegram route; no test or supplied fixture substitutes for them

# Recovered current Stage 3 premise assessment — 2026-11-04

This assesses the pre-existing Stage 3 premises against retained final-gate
evidence. It does not alter their original text or historical verdicts; it
records their current status within the same original round 5. No provider,
model, bot, service, or test command was run for this assessment.

## Premise 1 — current

Assumption: Both qualified built planning writers can emit actual v2 destinations
into owned external-record fixtures.

Current evidence: the canonical reconciliation retains the qualified writer at
`7f83446`, version 1.4.2, with both host `handoff_v2.py` hashes equal; the
final source validation repeats the same clean writer identity. The exact
four-file master goal command is retained as 112 passed at `fa401c23`.

Verdict: **holds**.

Limit: this is writer/source evidence; it is not itself a live provider claim.

## Premise 2 — current

Assumption: The proposed live drills can be collected without executing providers
or reading login contents.

Current evidence: the final-gate records retain live-suite collection and the
owner-native receipts independently. The later native work used bounded
owner-terminal wrappers and explicit prompt budgets; the QdkpW5 receipt records
one authorized turn and cleanup rather than inferring execution from collection.

Verdict: **holds**.

Limit: collection remains collection only; provider behavior is established only
by the separately cited owner-native observations.

## Premise 3 — current

Assumption: An authorized isolated Codex login route is available without
changing the owner's profile or credentials.

Current evidence: owner QdkpW5/Codex 0.166.0 completed the outside-folder
catalogue/manual/restart route with a bounded one-turn budget; the receipt
records prompt-budget and cleanup checks passing. The canonical reconciliation
keeps it distinct from the working service and records no profile/provider/trust
change.

Verdict: **holds**.

Limit: this establishes the authorized disposable owner-terminal mechanism, not
a claim about sandbox enforcement in the managed service.

## Premise 4 — current

Assumption: The final native/provider, fresh-Codex-identity and independent
Telegram routes can demonstrate the full owner journey.

Current evidence:

- Historical owner-native 12-pass and separate binding 5-pass receipts cover
  the original native identity/rollover/adoption/restart/preset scope at their
  measured base.
- QdkpW5 directly measured the missing outside-folder real-catalogue, manual,
  and restart functional path. Its raw 59-pass/1-fail/2-blocked record is
  retained; the separate immutable correction assessment identifies the failed
  legacy `has_user_event` condition as diagnostic, while the authored
  functional checks passed.
- Installed `d5e64b78` Telegram acceptance records the corrected named
  unavailable-folder/no-pane refusal with preserved sessions and the observed
  trust-hold interruption followed by one safe explicit retry without a
  duplicate.

Verdict: **holds at the stated measured scope**.

Limit: the first fresh-session idle observation remains NOT OBSERVED; raw native
qualification did not exit zero; no claim is made for all crash windows, a new
paid bundle, published deployment, or a provider behavior beyond the retained
observations. These are not unmet authored Premise 4 requirements.

## Current premise finding

**PASS.** No premise is false and no current Stop condition is identified.
Earlier not-checkable verdicts remain historical records of the unavailable
route at that time.

Background jobs: none started.
