# Stage 2 folder continuation source evidence

Task2.1 source commit99fda552 followed Stage1green55740ad6. The initial task had63 passing
host tests and mutation proof (5fail58pass) with bytes restored. The first task review found
a Critical worktree-path revalidation gap; eight manual/restart cases reproduced it, the
shared authority and pre-bind check were fixed, and a fresh Tier1 review approved.

The first stage goal check returned1failed72passed12.91s. The stage regression check
returned8failed331passed30.34s. Failures were schema/capability/composition test drift and
new project-level root reads missing their explicit architecture inventory. The repaired
legacy migration fixture now opens schema21 and writes frozen legacy SQL, rather than
using today's writer on an old schema. Latest schema is checked from the migration register.

Two independent Tier2 reviews of55740ad6..99fda552 blocked on the same durable restart-root
gap; reviewA also found ambiguous outside-folder ownership. Both findings were confirmed:
six changed-root restart cases and one ambiguous-folder case reproduced failures before
repair. Migration25 adds immutable session_project_roots. The session, permission preset,
handoff capabilities and registered-root snapshot are saved atomically before a pane.
Restart validates the inherited source snapshot against the current project mapping.
Missing legacy root provenance visibly refuses automatic resume while preserving old
records/bindings. Manual catalogue selection supplies explicit current root context.
Outside folders with multiple historical project associations are not offered or resumed;
a worktree cannot be revived through an outside binding.

Focused affected task, inventory, migration and recovery verification:169passed12.33s.
Original broad failures stand with focused repairs; no repeated broad pass is invented.
Schema upgrade preserves legacy sessions and presets without fabricating root snapshots.
Root-update immutability and root-insert rollback are exercised by disposable SQLite tests.

Class sweeps:
- rg -n 'MIGRATIONS\[:-1\]|current_version|continuation_permissions|permission_presets' tests/integration/test_permission_presets.py tests/integration/test_worktree_roots.py tests/integration/test_tui_bootstrap_conversations.py tests/integration/sqlite
- rg -n 'project_root|inherit_permissions_of|_save_with_permissions|for_project' src/remote_agents/application src/remote_agents/adapters/sqlite src/remote_agents/ports

Record audit found an unrecognized review Markdown file under fingerprint-excluded proof/;
it was moved to docs/acceptance/session-defaults-routing-task21-review.md. The late premise
worksheet is explicitly disclosed. All evidence is host/disposable/synthetic except the
independent evaluator's actual user-route attempts, which are recorded separately and
cannot be replaced by these tests. Native/provider, external Telegram and independent
user-route acceptance remain unqualified. No installed profiles/configuration/trust,
credentials, deployment, publishing, push or merge were changed.

Final planned task proof:73passed6.93s; discovery mutant5failed68passed7.10s; staged
bytes restored. Additional root-save/startup/tmux/catalogue fix scope:92passed11.39s.
Record audit passed all10checks with no findings. Fresh Tier2A planning:code-reviewer routing_stage2_final_a and independent Tier2B
planning:code-reviewer routing_stage2_final_b reviewed55740ad6..aa89538d, including
repair99fda552..aa89538d: bothAPPROVE,0Critical,0Important,0Suggestion. Original blocked
reviews and reproductions remain evidence; no later source edits followed their approval.
Independent demo initially hit a usage limit, then ran after owner continuation. Its
configured public CLI/TUI route can register a project but has no provider-backed saved
conversation to resume; its original failed attempts are preserved. No demo pass is claimed.
The prior OD-1 deferral explicitly names Stage1; matching it at Stage2 was refused, and the
master has no applicable Stage2 entry. Independent planning:goal-evaluator routing_stage2_demo and its two route-precision
follow-ups retain all3Outcomes as cannot reach by the user's route. The registry fixture
issue was resolved using public add-project and is not a product defect; both TUI and
Telegram resume routes exist, but no authorized native conversation was demonstrated.

Gate disposition: source verified; overall Stage2 NOT GREEN, awaiting owner decision.
The active remediation assessment is round1of2; no budget exhaustion is asserted.
Source verification does not discharge the independent demo. Stage3 remains unstarted.

Dispatch:0of0 implementationYES tasks; Task2.1 inline asNO.
Review: required Tier1 initially blocked on worktree-path validation, repaired and fresh
Tier1 approved; both original Tier2 passes blocked on ownership/provenance, repaired within
round1, both fresh Tier2 passes approved the final source range. Evaluator FAIL is separate.
Residuals: provider-backed Stage2 demo, prior deferred Stage1/Telegram demonstrations,
Stage3/final native/provider qualification and master close-out. No code review suggestions.
No platform-specific or design verifier applies to this Python CLI stage. Local hook review
ledger NOT RUN: this worktree has no dispatch-log.jsonl; actual installed-role results are
retained and no hook entries were fabricated. All launched tests and agents completed;
no running owned actors remain. Independent demo instances were timeout-bounded.

The execution skill's Stop condition applies: an Outcome cannot be met or shown as written.
OD-1 matchesStage1only, notStage2; a further owner deferral or an isolated usable native
conversation route is required before Stage2green/Stage3. No source outcome is weakened.


## Native route and conservative hold repair

Owner selected option2 and confirmed "yes" to existing-login authentication through a
read-only bind mount, disposable state and verified cleanup. Current source6fc460d5 was
built as wheelSHA2566680f72c386636c115be5c90a122dfe6e1591b790b208860fabed6b6e4a8af1b.
Native Codex0.165.0 completed synthetic exec and interactive CLI conversations with0toolcalls
and on-request/workspace-write/auto_review policy. Mount metadata explicitly verifiedro;
placeholder remained0bytes/nonsymlink. No normal profile/config/model/provider/trust change.

Early demo failures were diagnosed rather than patched into the product: the exec-source
seed was intentionally excluded by the provider's default interactive thread/list filter;
a later genuine CLI thread was returned by that same default filter. The resume list rests
on Back, so Enter without moving the highlight returned to the agent picker as designed.
After selecting the actual conversation, the built public TUI created exactly one running
managed Codex session with the right source conversation, registered folder and approve_for_me
snapshot. Raw private captures remain in the owned temporary fixture, not public records.

The dedicated fixture server later became unavailable. The durable session remained running;
a public resume retry emitted bare 'no sessions' from attach. The cause of server loss is
not reclassified here. This exposed a real failure to provide the required visible hold.
The code repair inspects the existing pane before offering attach. Missing/non-live or
unreadable evidence now returns a named hold on both frontends, preserves the exact session,
source and preset, and launches nothing. A verified live or preserved pane stays attachable.
Startup outcomes also distinguish pane_unavailable from already-open. No automatic ending,
replacement, server-loss classification, or BL-136 closure is introduced.

Captured facts are in tests/fixtures/resume/native-stale-pane.json. Enumeration command:
rg -n 'get_by_resume_source|not outcome.created|ResumeOutcome\(' src/remote_agents/application/services.py src/remote_agents/adapters/tui/app.py src/remote_agents/adapters/telegram/service.py
Consumers swept also include application/interrupted_resume.py and restart_notifications.py.
Host capture/property and both frontend tests reproduced the missing hold; two test mechanics
issues (missing Path import and absent Telegram fixture profile) were corrected without
weakening assertions. The Telegram route then independently failed on the old Not resumed
message; the TUI failed on an attach request. Final host fix-scope135PASS17.01s. This is
pre-native repair evidence; native retry and fresh required review remain pending.
