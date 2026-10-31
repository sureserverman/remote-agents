Executor transcription: installed evaluator is read-only; complete returned final follows verbatim.

## Close-out independent demo — PASS with recorded owner deferral

Terminal route was exercised through the shipped CLI on the private fixture. Telegram was not exercised: owner, 2026-10-27, selected deferral to the final combined gate.

| Outcome | Verdict | Evidence |
|---|---|---|
| Works: Telegram and terminal Settings and launch/resume flows expose the same persisted choices. | **Terminal holds; Telegram NOT DEMOED** | Terminal Settings displayed all four choices: Approve for me, Ask me, Full access, and Use Codex configuration. I changed Full access then restored Ask me through the visible picker; Settings displayed Ask me afterward. Fresh picker showed `Codex launch permissions · Ask me`; resume picker showed `Saved preset / current default`. Telegram has no isolated user route and was owner-deferred, with no FakeChat/test substitute. |
| Invalid or unavailable input → a named refusal with existing sessions preserved. | **holds** | Selecting an unavailable agent visibly returned `That agent cannot be launched here: executable_missing`. The pre-existing session `725c1ae3-0612-4868-ae9f-9b21d5ecc8d2` remained preserved. |
| Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch. | **holds** | Resume launch created `a615c1e2-60d9-47f1-8cad-0d2693d3a2c9`. After the owner’s one matching Ctrl-C and offline shipped startup reconciliation, SQLite showed this same UUID as `preserved`; its only events were `ready` and `reconciled_pane_dead`, each once. No second launch event appeared. |

Additional Stage 1 outcomes:

| Outcome | Verdict | Evidence |
|---|---|---|
| Works: Every managed Codex start receives one deterministic, inspectable launch preset. | **holds within terminal fixture** | The new managed Codex session `a615c1e2-60d9-47f1-8cad-0d2693d3a2c9` recorded `preset=ask` in `session_permissions`. |
| Invalid or unavailable input → a named refusal with existing sessions preserved. | **holds** | Same unavailable-agent refusal and preserved pre-existing session evidence above. |
| Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch. | **holds** | Same `preserved` reconciliation state and one `ready` event for the new session above. |

Commands used were limited to the private TUI wrapper’s `capture-pane` and `send-keys`, plus read-only SQLite queries. The wrapper ended through normal user detach; no background fixture work remains.

Findings:

- **Minor / deferred:** the isolated Telegram user route was not available. This is the owner-authorized final-gate obligation, not evidence of Telegram acceptance.
- Native provider policy and service behavior were not evaluated.
