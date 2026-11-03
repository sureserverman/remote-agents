# Bulk native folder diagnostics

The owner requested collecting failures together and fixing them in bulk, after four
supplemental native runs stopped before the full folder/catalogue/manual/restart journey.
Those failed runs remain preserved. This change qualifies the diagnostic harness locally;
it does not claim that the native journey or original master has passed.

The existing owner command remains:

```bash
/tmp/ra-folder-bound-native-20261103/run.sh
```

Its source-pinned bootstrap delegates to `tests/support/run_folder_native.sh`. It runs all
offline admission controls with `--maxfail=0` and separately records the namespace probe,
even if one preflight fails. It starts the native case only when both pass. The native
case declares 60 checks, a 420-second work budget, bounded final diagnostics, and a
separate 60-second cleanup allowance. The existing private fixture setup/owned-process
retirement retain their own prerequisite and lifecycle bounds.

Each reachable operation records its result and full exception traceback immediately in
a private, exclusive-create, fsynced JSONL receipt. Ordinary errors do not stop independent
checks. Cancellation/termination is recorded at the leaf and propagated; it does not
authorize additional launches. A failed prerequisite leaves an explicit blocked result.
Cleanup and its credential-metadata invariants run independently after collection, including
when earlier checks failed. Cleanup failure is included in the final aggregate report.

If catalogue discovery fails but the native binder independently established the real
thread ID and the original session is confirmed ended, the harness can still diagnose
manual resume and automatic restart against that observed thread. This route is explicitly
`observed_thread_diagnostic_only`. It cannot pass `qualified.manual` or `qualified.restart`:
those require actual catalogue resolution with the exact project root/folder. Unknown native
identity or an unproven source stop blocks further launches. No native history or provider
row is synthesized. The default model-free mode denies every terminal model/nudge/slash
prompt; the separately proposed one-turn mode below permits one exact owned-session message.

The full-path audit also fixed a latent fixture bug: `ProviderConversationId` is a dataclass,
so catalogue matching must compare its `.value`, rather than its string representation,
with the native thread ID. The entire-workflow control uses the real typed conversation
record and reaches catalogue-based qualification; leaf-only mocks had missed this defect.
Sweep: `rg -n 'str\([^\n]*provider_conversation_id' src tests` found only this instance.

Each run directory contains:

- `admission.log` and namespace stdout/stderr for both preflights;
- `pytest.log` for the aggregate native result;
- `native-result.jsonl` for every check, exception, selection provenance and private diagnostics;
- `native-result.jsonl.summary.json` for every declared passed/failed/blocked result;
- `runner-summary.json` for source/writer identity, preflight and pipeline exits, and completion.

The runner rejects skipped/missing/incomplete native evidence, failed or blocked checks,
missing cleanup/prompt-budget/qualified-route checks, absent final success marker, and log-writer
failure even when pytest itself exits zero. Existing report/receipt paths and symlinks are
never overwritten. All newly created evidence is private. Source application/dependency
inputs remain byte-equivalent to the installed d5 candidate; no working service, normal
profile, provider/model selection, hook trust, or publication changes are involved.

## Local verification and independent review

`uv run --locked pytest tests/contract/test_native_bulk_checks.py
tests/contract/test_folder_native_admission.py -q --maxfail=0`: **49 passed in 2.10s**.
These are offline controls, including genuine disposable JSON-RPC children and the actual
shell runner with fake tools; they never start Codex or spend a model turn. They exercise:

- the complete healthy workflow with real typed conversation records;
- eight simultaneous failed checks, continued resume/restart diagnostics and cleanup;
- failed stop safety, malformed intents, work-budget expiry and unreachable dependencies;
- cancellation/system-exit leaf tracebacks, propagation and independent cleanup;
- both failed preflights, skipped native tests, missing/invalid reports and log-writer errors;
- private evidence reservation, symlink refusal and credential-free offline fixture admission.
- real application DTOs, constrained method signatures, typed project/profile admission,
  and continued safe diagnostics after an observational validator fails.

Ruff check/format: **806 files**, passed. Bash syntax and ShellCheck passed. The ShellCheck
SC2329 suppression documents the verified indirect EXIT-trap invocation only.

Both `planning:code-reviewer` agents, `recovery_boundary_review` and
`routing_stage3_final_review_b`, independently reviewed the complete delta and approved it.
The Important cancellation/traceback finding was fixed as a class with one exception
recorder for leaf/fixture/execution failures, then verified at fix scope. Final pipeline
and credential-free control corrections were also reviewed. No Critical/Important finding
or reviewer-owned job remains. This source/control evidence leaves actual native acceptance,
BL-141, original round5 and master close-out open until the real owner run supplies proof.

## First bulk run: result-contract drift corrected

The actual f340aa96 owner run with Codex0.166.0 completed its 54-check report in8.61s:
15passed,1failed,38blocked; cleanup and no-model checks passed. Fresh launch returned an
owned UNTRUSTED session, but its validator read `LaunchOutcome.created`, a field that exists
only on `ResumeOutcome`. The offline fake fabricated that field, so the original44 passing
controls had not proved compatibility with the actual application return contract. Full
failure evidence remains under recovery/folder-bound-native-qualification-20261103/
attempt-5-bulk-launch-outcome-contract; none of the blocked checks is accepted.

The whole-path controls now return real slotted/frozen LaunchOutcome, ResumeOutcome,
SessionRecord, TerminalObservation and Backend objects, real ProductionPaths and native
fixture containers, and CompletedProcess. Use-case doubles have the actual method
signatures, reject unknown methods and fail unconfigured calls. Real conversation and
restart-report types remain in use. Before changing the workflow, these stricter controls
reproduced the same missing-field failure locally (1FAIL0.45s; strict boundaries1FAIL0.48s).

Acquisition, safety admission and observation are separate checks for fresh/manual/restart
resources. Only an actual SessionRecord/SessionId under the expected project/profile is
followed. A validator error or `ResumeOutcome.created=False` is reported without hiding
otherwise safe readiness/restart diagnostics; it still prevents overall acceptance.
Foreign identity and an unproven stop continue to block follow-up actors. The manifest now
has60 checks. The added controls exercise those cuts, real DTO shapes, and method-signature
rejection. Both independent reviewers audited the actual source contracts and approved the
class repair, with no Critical/Important findings or owned jobs.

AST sweep across every src/tests Python file for `launch` functions constructing a
`SimpleNamespace` with `created` finds no remaining instance. The current harness's
`.created` access is confined to actual ResumeOutcome data. Intent fields and retention
were also checked against real runtime serialization/cleanup source. These repairs remain
source/control proof; actual naming/catalogue/manual/restart acceptance is still pending.


## Second bulk result and catalogue prerequisite

The actual owner run at850f0c38 (`ra-folder-native-20261103-Epv8zc`) completed all60
checks in36.35s: **55passed,1failed,4blocked**. The only failure was `catalogue.resolve`
with zero listed rows. The dependent folder/project and qualified manual/restart checks
remain blocked. Real diagnostic manual resume, automatic restart, exact folder/thread/
preset preservation, no-model guard, credential invariants and cleanup all passed.
The raw evidence and hashes are retained under `recovery/folder-bound-native-qualification-
20261103/attempt-6-bulk-named-empty-catalogue/`. These component proofs are retained,
while catalogue acceptance and BL-141 stay open.

The native row was named, unarchived, paginated, and had `has_user_event=0`. Read-only
inspection of the exact installed Codex0.166.0 executable found an active-list SQL
condition `threads.archived = 0 AND threads.preview <> ''`. The exact retired row's
preview was not captured, so empty preview remains an inference rather than a recorded
value. Source review found outside-folder binding enumeration and private catalogue
routing already present. Naming alone did not establish catalogue eligibility in this run.
No product source, provider policy or normal profile change is indicated by this evidence.

[Official app-server documentation](https://learn.chatgpt.com/docs/app-server) specifies
that `thread/list` applies its filters before pagination, defaults to the configured provider
and interactive sources, and `useStateDbOnly=true` avoids transcript scans. It documents
naming as metadata; it does not establish that an empty named active thread passes this
installed binary's preview filter. No history injection, database edit or archived-thread
substitution is used to make acceptance pass.

## Prepared one-turn option: owner authorization required before execution

OD-3's accepted scope expressly excludes new paid native runs. This option is prepared and
source-tested, **not authorized or executed**. The proposal permits one new user message
in the existing disposable fixture, under the owner's existing model/provider selection:

```text
Reply exactly RA_FOLDER_NATIVE_OK. Do not use tools or change files.
```

An explicit runner fourth argument `one-turn` selects the exact internal authorization
literal. The original command still defaults to model-free, overwrites inherited turn
authorization, and cannot silently spend a turn. The guard admits the exact text to the
verified fresh session once, consumes its allowance before awaiting delivery, and restores
deny-all in `finally`. Errors, WAIT, uncertainty, cancellation and subsequent nudges never
retry. This bounds application prompt delivery, not the provider's internal request retries
or exact billing amount. No model/provider/profile/trust configuration is selected or changed.

One-turn mode declares62checks, including actual send success and selected native metadata
`has_user_event=1`, non-empty-preview boolean and an idle pane before ending. Qualified
manual explicitly requires this history prerequisite; qualified restart inherits it. All
checks and cleanup must pass, with final prompt count1 bound to the requested runner mode.
Model-free mode retains60checks and count0. No reply-content or paid native pass is inferred
from offline tests. The new receipts record selected-thread source/provider and preview
presence only, plus actual private `thread/list` cwd/row-count/cursor metadata. Catalogue
requests and payloads are delegated unchanged.

Offline controls: **67passed in3.02s**, including the observed55/1/4 failure pattern,
exact grant/no retry, real PromptDelivery/PromptOutcome values, empty preview after send,
failed send with still-returned catalogue row, mode/count/history-report binding, actual
shell one-turn routing and inherited authorization override. A reviewer found that the
individual qualified labels could initially pass despite failed history setup. The added
whole-path test went red (1failed0.50s); the conditional history dependency repairs that
class. The original failure is preserved with the green controls under
`recovery/bulk-native-diagnostics-20261103/catalogue-eligibility-remedy/`.

This remedy does not close the original run, invent a sixth round, waive catalogue
acceptance, change the installed application, or authorize another paid/native bundle.
