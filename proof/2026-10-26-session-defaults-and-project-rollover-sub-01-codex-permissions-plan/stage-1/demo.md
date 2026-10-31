# Stage 1 demo — 2026-10-26-session-defaults-and-project-rollover-sub-01-codex-permissions-plan

Environment: host; shipped CLI at f73db7f6 (identical tree to 46169c11),
disposable HOME/config/database/project, real tmux and TUI, synthetic Codex
executable. The shipped serve CLI ran in a network-isolated bwrap namespace.
This proves local wiring, persistence and visible recovery; native provider and
Telegram service qualification remain unproven here.

Evaluator: planning:goal-evaluator, agent 01a1799a-8d04-7dc2-8b5f-def4c20e405e.
The executor transcribed this report because the installed evaluator role forbids
repository writes. The evaluator independently drove the real product route.

## Outcome 1
> Works: Every managed Codex start receives one deterministic, inspectable launch preset.
Verdict: holds
Evidence: Retained observation: real isolated TUI launch created one Codex fixture process with `--sandbox workspace-write --ask-for-approval on-request -c approvals_reviewer="user"`; SQLite recorded `preset=ask`.


## Outcome 2
> Invalid or unavailable input → a named refusal with existing sessions preserved.
Verdict: holds
Evidence: Retained observation: invalid synthetic preset printed `codex.default_permissions must name a supported permission preset`; the existing running session and sole launch record were unchanged.


## Outcome 3
> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.
Verdict: holds
Evidence: The initial failed drill is retained: without the runtime process, Ctrl-C left a dead pane while UI/DB said `running`. With the shipped offline runtime included, I independently observed the same session's persisted state as `preserved` in SQLite and the real TUI showed `○ 1 preserved` / `fixture-project … preserved`. `launches.jsonl` remained one line, so reconciliation did not launch a second process. This is the specified visible hold.

## Failed-attempt retention

The initial fixture used the wrong home/database boundary; the direct PTY driver
then could not complete navigation. A private tmux wrapper resolved interaction.
The first completed drill omitted serve, so interruption left state running; that
failed report remains in .claude/stage-1-demo-outcome3-failure.md. Round 1 was
recorded failed. Round 2 supplied the missing shipped runtime, without changing
product source. The evaluator retracted the initial exit-zero concern: the child
had exited 1, while only the outer orchestration status had been printed.


## Independent report, verbatim

| Outcome | Updated verdict | Evidence |
|---|---|---|
| 1. “Works: Every managed Codex start receives one deterministic, inspectable launch preset.” | **holds** | Retained observation: real isolated TUI launch created one Codex fixture process with `--sandbox workspace-write --ask-for-approval on-request -c approvals_reviewer="user"`; SQLite recorded `preset=ask`. |
| 2. “Invalid or unavailable input → a named refusal with existing sessions preserved.” | **holds** | Retained observation: invalid synthetic preset printed `codex.default_permissions must name a supported permission preset`; the existing running session and sole launch record were unchanged. |
| 3. “Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.” | **holds** | The initial failed drill is retained: without the runtime process, Ctrl-C left a dead pane while UI/DB said `running`. With the shipped offline runtime included, I independently observed the same session’s persisted state as `preserved` in SQLite and the real TUI showed `○ 1 preserved` / `fixture-project … preserved`. `launches.jsonl` remained one line, so reconciliation did not launch a second process. This is the specified visible hold. |

**Updated demo result: PASS.** The earlier Outcome 3 failure was caused by omitting the shipped reconciliation runtime from the synthetic fixture, not by a demonstrated product defect. Its failed evidence should remain retained as the incomplete-fixture round.

Residual limitation: I did not treat the offline runtime as a green service qualification. Its Telegram initialization could not complete in the network-isolated fixture; the pass above is limited to the independently observed local reconciliation state and user-visible hold. The private fixture TUI was quit; a subsequent private-server stop reported no server running.
