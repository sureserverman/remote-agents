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

Final repaired-tree proof/reviews, evaluator follow-up and gate disposition are pending.
