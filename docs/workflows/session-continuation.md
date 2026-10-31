# Session continuation

Draft contracts captured from the implemented source. Human review is still required before
they become canonical. These describe requested launch configuration; native effective-policy
qualification remains an independent final gate.

## WF-CONT-001 — Default and one-session override

- **Entry:** Telegram `/settings` → `PrivateBotBoundary._settings_permissions_reply` and
  launch/resume selectors in `adapters/telegram/service.py`; terminal Settings →
  `SettingsScreen.choose_permissions`, `ProfilesScreen` and `ResumeConversationsScreen`.
- **Preconditions:** A descriptor declares the provider's permission capability; the persisted
  setting can be read. The four values are `approve_for_me`, `ask`, `full_access` and
  `provider_default`.
- **Steps:**
  1. Read the default on open, refresh or return. An absent key means `approve_for_me`.
  2. Settings presents all four shared labels and explanations. Saving uses
     `PermissionSettings.set` → `ConfigPermissionSetting` → the atomic config writer,
     serialized with cooperating writers; re-read the result. A refused save reports failure.
  3. Launch/resume offers an optional picker without a new required confirmation step.
     Cancel leaves the flow unchanged; an explicit choice overrides only the next new process.
  4. Keep a terminal override on its flow object; keep a Telegram override in opaque durable
     callback state. Re-check provider capability before acting; stale/unavailable choices
     receive a named refusal. A claimed mutation cannot launch twice on redelivery.
  5. `SessionService` resolves the choice, then saves the session and immutable preset in one
     SQLite transaction before starting a pane. The fixed builder adds the curated flags.
- **Postconditions:** A new managed Codex process has one inspectable requested preset.
  Its default is unchanged by an override. Failed validation starts no pane; failed saves are
  visible. Existing sessions retain their launch metadata.
- **Side effects:** Settings updates only remote-agents' config. A start writes session,
  idempotency and permission records, then creates its owned terminal pane.
- **Invariants:** Full access is a deliberate named choice. No raw launch arguments,
  model/provider change, installed-profile edit or hook-trust change comes from this control.
  Session inspection reports the stored launch request, not unobserved native in-pane edits.
- **Last verified:** 2026-10-27 against commit `9e86563a`; 163 task tests passed, and disabling
  override forwarding produced four expected failures. Native qualification is not claimed.

## WF-CONT-002 — Continuation inherits its source preset

- **Entry:** `SessionService.resume`, `InterruptedResumePass` and `RolloverPass`, through
  `application/permission_presets.py::SessionPermissions.resolve`.
- **Preconditions:** The source session or provider conversation is attributable to the same
  provider. The normal runtime includes `serve`, which owns reconciliation and restart work.
- **Steps:**
  1. For a new process, resolve explicit override → inherited session snapshot → managed
     conversation snapshot → current persisted default. An inherited legacy row without
     metadata uses the current default rather than inventing an earlier choice.
  2. Retain content-free conversation/preset associations before temporary ended-session
     conversation marks are forgotten. A default changed later cannot rewrite the snapshot.
  3. Persist the successor session and its preset atomically before its pane is created.
     Restart and rollover pass the predecessor identity explicitly; they use the same resolver.
  4. Reuse an already running conversation only after verifying a live or preserved pane
     with matching project and profile ownership. Unavailable or unreadable pane evidence
     produces a visible hold with the existing record and preset preserved. A replayed
     command cannot create an unrecorded second launch.
  5. If an operation is interrupted, durable launch state remains available to the runtime's
     recovery path. A dead owned pane is preserved by reconciliation; uncertainty is a visible
     hold/refusal rather than permission inference or a blind second launch.
- **Postconditions:** The next managed process retains its chosen source preset, unless the
  owner explicitly overrides it. Inspection distinguishes unknown legacy metadata. A failed
  continuation preserves existing session data.
- **Side effects:** Additive SQLite permission records and content-free conversation bindings;
  the existing restart/rollover lifecycle creates or adopts at most one successor.
- **Invariants:** Native permission edits are not inferred. Provider-default adds no policy
  switches. No model/provider or normal profile is selected by continuation.
- **Last verified:** 2026-10-27 against commit `9e86563a`; Stage 1's 74 launch-integration
  checks and independent real CLI/TUI recovery demo passed on its source tree. The TUI-only
  incomplete-fixture failure is retained; the complete offline runtime exposed preserved state.
  Native effective policy remains for the final combined gate.

## WF-CONT-003 — Select and adopt a frozen destination

- **Entry:** `RolloverPass.run_once`, composed by `build_rollover_pass`; an owner's
  **Rollover now** request or a supported writer's ready envelope.
- **Preconditions:** A running managed Claude/Codex predecessor, an enabled rollover
  setting, and a supported `planning-handoff` envelope. A Codex writer needs an exact
  independently recorded conversation binding; folder/time guesses, hook marks alone,
  reported roots and a shared daemon's inherited starter identity cannot establish it.
  Fresh unbound Codex conversations refuse attribution, including their first cross-repo call.
- **Steps:**
  1. Read v1 or negotiated v2. For v2, validate source identity and explicit destination
     root, branch, plan and task locator. External records directories do not select the
     execution project. Resolve one registered project or its recorded worktree.
  2. Freeze source/destination roots and project identities, target branch/plan/locator
     and inherited preset before launch. Keep that request channel and target on retry;
     later registry edits or writer reports cannot redirect the transaction.
  3. Revalidate the frozen target. Removed, changed, unregistered or ambiguous roots and
     branch mismatches receive named refusals while the predecessor remains intact.
     A legacy transaction whose ownership cannot be uniquely reconstructed holds.
  4. Persist launch intent, session, folder and permission metadata before starting one
     successor. A launch whose result was not recorded holds as `successor-unknown`;
     recovery never creates another pane to guess what happened.
  5. Wait on the destination's folder-trust dialog. Send the provider's adoption command
     only after trust and idle checks, with the explicit source-root locator. Acceptance
     must match the frozen ready digest and destination-owned state.
  6. A same-root stage successor resumes that run at the actual target task and proves its
     latest saved handoff receipt. A different root keeps independent owned state; never
     copy or reinitialize the predecessor's run to establish destination ownership.
  7. Stop the predecessor gracefully only after acceptance and another idle check.
     Restart after accepted-state persistence completes that ordering without relaunching.
- **Postconditions:** At most one successor belongs to the selected destination project,
  folder and inherited preset. Both session surfaces retain source-to-destination lineage
  and a meaningful hold/failure reason. An unsuccessful adoption preserves the predecessor.
- **Side effects:** Additive routing/session metadata, one owned pane, bounded handoff
  files and accepted-before-stop lifecycle events.
- **Invariants:** No prose-based destination guessing, raw launch arguments, daemon-starter
  fallback, force stop or automatic trust approval. Uncertainty never authorizes a second launch.
- **Last verified:** 2026-10-29 against RA `902f2a9a` and writer `0f8af59`:
  16 composed source cases and the wrong-project mutation (six expected failures).
  Native five-project/identity/adoption/restart qualification remains open.

## WF-CONT-004 — Resume in the surviving recorded folder

- **Entry:** Both Resume catalogues → `ConversationService`/`SessionFolders` and
  `SessionService.resume`; service startup → `InterruptedResumePass`.
- **Preconditions:** A provider-catalogued conversation or attributable interrupted
  session, a surviving unambiguous folder and the relevant provider capability.
- **Steps:**
  1. Catalogue the registered root, live worktrees and appropriate persisted outside-folder
     bindings, including ended sessions. Deduplicate each provider's conversation identifiers
     before pagination. Preserve historical session IDs and their explicit project association.
  2. Resolve the selected conversation to its exact project and folder. Revalidate the
     canonical registered-root snapshot and folder ownership. A removed worktree cannot be
     revived through an outside-folder row; root moves and symlink retargets refuse.
  3. Resolve the saved preset using WF-CONT-002. Save the new session, immutable registered
     root, folder and preset atomically before its pane starts.
  4. If the conversation already has a managed session, verify pane liveness/preservation
     and project/profile ownership. Otherwise remain on a named hold, preserve all existing
     metadata and launch nothing. An owner can inspect the session from Sessions.
  5. Startup uses the same folder authority and recorded preset. Legacy automatic recovery
     without immutable root provenance holds; an explicit current catalogue selection supplies
     its current project context without rewriting historical IDs or inventing past metadata.
- **Postconditions:** A continuation stays under its attributable project, surviving
  folder and chosen preset. Missing, ambiguous, unreadable or changed input produces a
  named refusal/hold rather than a fallback folder or replacement launch.
- **Side effects:** Retained content-free folder/conversation associations and additive
  root/preset snapshots; one new pane only when a fresh managed resume is authorized.
- **Invariants:** Existing presets and source records remain unchanged on refusal. This
  does not reclassify ambiguous tmux server loss or solve surviving-turn restart hardening.
- **Last verified:** 2026-10-29 against RA `477a83e2`: affected host checks141PASS,
  literal task84PASS with nine expected mutation failures, and the independent public
  TUI/native Codex launch/visible-hold observations retained with their exact source versions.
  That native demonstration does not qualify the complete outside-folder/restart matrix.

## WF-CONT-005 — Finish a matching outstanding workflow request

- **Entry:** A built writer's finished envelope → `RolloverPass._consume_finished`.
- **Preconditions:** An outstanding request naming the same managed session and source
  root; exact request ID/time correlation; supported v2 finished schema; whole-workflow
  tasks, gates and close-out complete with terminal owned execution state.
- **Steps:**
  1. Validate the finished envelope and correlate it to the outstanding request. A stale,
     foreign or mismatched signal is refused; absence of another sub-plan is insufficient.
  2. Persist the distinct nonfailure `workflow_finished` outcome before cleanup.
  3. Withdraw only that correlated request from its frozen source channel. Compare before
     removal so an old cleanup cannot unlink a different newer live request.
  4. Recover cleanup after interruption from the durable terminal outcome.
- **Postconditions:** The matching completed workflow no longer occupies the request
  slot; both surfaces show the finished outcome. No successor is created and no pane is stopped.
- **Side effects:** One terminal routing outcome and removal of matching bounded request files.
- **Invariants:** Sub-plan completion alone never declares the master finished. No open
  request is silently answered by a stale completion signal.
- **Last verified:** 2026-10-29 against RA `902f2a9a`, including source implementation
  `97ff2bcc`, and the built writer: composed finished-request, correlation, cleanup and
  interrupted-cleanup cases. These are source checks, not native workflow completion acceptance.
