# Stage 2 demo — 2026-10-26-session-defaults-and-project-rollover-sub-01-codex-permissions-plan

Environment: real shipped CLI/TUI; private home/database/project/tmux, synthetic executable; offline real serve startup. No isolated Telegram user route. NOT native provider qualification.

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: Telegram and terminal Settings and launch/resume flows expose the same persisted choices.
Verdict: cannot reach by the user's route
Evidence: Terminal Settings displayed all four choices and persisted Ask me. Actual fresh Codex fixture launch recorded UUID725c1ae3-0612-4868-ae9f-9b21d5ecc8d2 with ask snapshot; resume displayed Saved preset / current default. No isolated live Telegram user route was configured. FakeChat and test/import calls were not substituted.


## Outcome 2
> Invalid or unavailable input → a named refusal with existing sessions preserved.
Verdict: holds
Evidence: Root changed only disposable default_permissions to invalid_fixture_preset. Shipped CLI printed codex.default_permissions must name a supported permission preset. Existing UUID remained preserved with exactly ready and reconciled_pane_dead events. Root restored ask before final observation.


## Outcome 3
> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.
Verdict: holds
Evidence: Exact owned pane showed ^C then Pane is dead after a single Ctrl-C. Root ran shipped startup reconciliation offline. Evaluator reopened shipped wrapper and saw 1 preserved / Sessions 1 preserved; same durable UUID, no new ready event or session.


Executor transcription: evaluator role is read-only. The sections above transcribe its returned observations; the following complete final report is copied verbatim from its rollout. No verdict was upgraded.

## Stage 2 user-route demo — FAIL

I made no repository edits; the read-only evaluator boundary requires the report be transcribed by the owner.

| Outcome | Verdict | Evidence |
|---|---|---|
| “Works: Telegram and terminal Settings and launch/resume flows expose the same persisted choices.” | **cannot reach by the user's route** | Terminal evidence holds: Settings displayed the four choices; choosing `Ask me` persisted visibly. Fresh Codex launch recorded session `725c1ae3-0612-4868-ae9f-9b21d5ecc8d2` with snapshot preset `ask`; resume showed `Saved preset / current default`. No isolated live Telegram route was configured, and I did not substitute FakeChat or test/import calls. |
| “Invalid or unavailable input → a named refusal with existing sessions preserved.” | **holds** | Owner changed only disposable `default_permissions` to `invalid_fixture_preset`. Running `python3 /tmp/remote-agents-stage2-isolated/run.py` printed: `codex.default_permissions must name a supported permission preset`. SQLite still showed the existing UUID as `preserved`; its event rows remained only `ready` and `reconciled_pane_dead`. |
| “Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.” | **holds** | The owned managed pane visibly showed `^C` then `Pane is dead`. After the owner’s single Ctrl-C and offline shipped-CLI reconciliation, reopening the shipped wrapper showed `○ 1 preserved` and `Sessions 1 · ○ 1 preserved`. The persisted session remained the same UUID in state `preserved`; no additional `ready` event/session appeared. |

Finding:

- **Blocking** — Telegram’s required user route was unavailable in this isolated fixture, so the cross-surface Outcome 1 and Stage 2 goal cannot be accepted from this demonstration.

The private wrapper had already exited after the final observation; I left no background work running.
