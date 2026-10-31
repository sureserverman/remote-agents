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
