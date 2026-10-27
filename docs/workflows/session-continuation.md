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
  4. Reuse an already running conversation without a second pane or a permission mutation.
     A replayed command cannot create an unrecorded second launch.
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
