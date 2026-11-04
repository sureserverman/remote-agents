# Stage 3 independent user-route demo

Status: **FAIL**. This uses the supplied public wheel through its shell CLI and dashboard/pane route only; no source, diff, product import, or test suite was used. Telegram sending/polling was not authorized and was not attempted.

Artifact: `/tmp/routing-stage3-product-build-final/remote_agents-0.67.2-py3-none-any.whl`
SHA-256: `41c89a53a05f7fd2f11f78eb2a3c3afc56496f14571138ea1c6c2caf6d18de39`

| Outcome | Verdict |
| --- | --- |
| 1 | cannot reach by the user's route |
| 2 | cannot reach by the user's route |
| 3 | cannot reach by the user's route |

## Environment precondition capture

The initial `pip install --no-deps <wheel>` capture was an evaluator setup error, not a packaging finding: `remote-agents --help` failed first for `ModuleNotFoundError: No module named 'yaml'`, then in a system-site venv for `ModuleNotFoundError: No module named 'telegram'` (exit 1). This capture is retained but excluded from the findings.

The corrected isolated installation was:

```sh
UV_CACHE_DIR=/tmp/routing-uv-cache uv pip install --python /tmp/routing-stage3-demo-venv-complete/bin/python /tmp/routing-stage3-product-build-final/remote_agents-0.67.2-py3-none-any.whl
```

Sandbox DNS initially failed while fetching `psutil`; the same normal dependency-resolving command was then permitted outside the sandbox. It installed `python-telegram-bot==22.8`, `textual==8.2.8`, `psutil==7.2.2`, and `pyyaml==6.0.3`.

## Public service-transport prerequisite

Using the same private config and fixture roots, its private generated
`telegram.env` was withheld, and `serve` was invoked with `env -i` (a minimal
`PATH`, fixture `HOME`/`CODEX_HOME`/state/registry variables only, and no
Telegram variables). The public command was:

```sh
remote-agents serve --config /tmp/routing-stage3-demo-complete-home/.config/remote-agents/config.toml
```

It returned exit 1 before any backend contact with the named result:

```text
remote_agents.config.ConfigError: Telegram environment file is missing
```

`remote-agents serve --help` returned exit 0 and exposed only `--config`; it
does not document an isolated/no-Telegram service transport option. An
owner-supplied separate Telegram bot/account (and its private environment file)
is therefore the remaining prerequisite for the public service loop that would
drive a native session rollover/restart journey. No fake token, Telegram send,
or Telegram poll was used.

## Outcome 1

> Works: The owner can run a five-project master plan through rollovers and restart without manually fixing project or permission selection.

Verdict: **cannot reach by the user's route**.

Evidence:

- With private `HOME`, `CODEX_HOME`, tmux temporary directory, state, and registry fixtures, the dependency-complete `remote-agents --help` returned exit 0 and displayed public commands including `add-project`, `tui`, `pane`, and `console`.
- The public CLI created five owned projects:

  ```sh
  for task_project in p1 p2 p3 p4 p5; do
    remote-agents add-project --area demo2 --name "$task_project"
  done
  ```

  It printed one path for each under `/tmp/routing-stage3-demo-projects/demo2/` and reported creation of an owned projects registry.
- The required public dashboard/pane entry was run:

  ```sh
  remote-agents pane projects
  ```

  Its visible UI said `Choose a project — 10 available` and listed `p1` through `p5` as `registered`; five earlier owned folders were `unregistered`. The visible instruction said `a session opens in this slot`.
- Selecting a project would launch a real provider session, while the required
  public service loop has no isolated/no-Telegram route and stops with the
  named missing Telegram-environment prerequisite above. No five-project
  rollover, restart, destination adoption, or permission inheritance can
  therefore be observed.

## Outcome 2

> Invalid or unavailable input → a named refusal with existing sessions preserved.

Verdict: **cannot reach by the user's route**.

Evidence:

- The public CLI gives named refusals without starting a session:

  ```text
  remote-agents add-project --area /tmp/routing-stage3-demo-projects/p1 --name p1
  area must be lowercase letters, digits, and single hyphens (1 to 64 characters)
  exit=1

  remote-agents add-project --area demo --name p1
  area is not an existing development-root directory
  exit=1

  remote-agents add-project --area demo --name p1
  project directory already exists
  exit=1
  ```

- No pre-existing provider session could be created because the required public
  service loop is blocked on the owner-supplied separate Telegram bot/account
  prerequisite, so preservation of an existing owner session could not be
  demonstrated.

## Outcome 3

> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

Verdict: **cannot reach by the user's route**.

Evidence:

- The dashboard/pane reached project selection, but opening a project needs the
  public service loop, which is blocked on the named owner-supplied separate
  Telegram bot/account prerequisite. There was therefore no actual operation
  to interrupt, no retry/hold screen, and no launch record to inspect. No test,
  import, or synthetic session was substituted.

## Findings

- **Blocking — Outcome 1 not demonstrated:** the public dashboard visibly lists five registered owned projects, but the required service loop has no isolated/no-Telegram route and exits with `Telegram environment file is missing`. An owner-supplied separate Telegram bot/account is required before the provider-backed rollover/restart journey can be reached. Evidence: Public service-transport prerequisite and Outcome 1.
- **Blocking — Outcome 2 incomplete:** named public-route refusals were shown, but existing-session preservation was not reachable. Evidence: Outcome 2.
- **Blocking — Outcome 3 not demonstrated:** no actual operation was available to interrupt, so durable retry/hold and exactly-once behavior remain unobserved. Evidence: Outcome 3.

## Round 5 final independent assessment — 2026-11-02

Status: **FAIL**. This is a final evidence review under the owner-authorized
read-only/public-route boundary. No new native/model/provider turn, Telegram
send or poll, service restart, deployment, configuration/profile/provider/trust
change, credential access, source inspection, diff inspection, import, or test
suite was performed. The earlier independent public-route capture above remains
the direct route evidence; its Outcome sections are intentionally unchanged.

The final-gate brief records an owner-run native bundle (12 passed, with a
separate five-pass binding proof) and owner-observed service results. Those are
useful at their stated native/owner-observed scope, but do not make this
evaluator able to reach the missing isolated Telegram route, and are not
substituted for it. The brief also records the independent Telegram route as
partial: it lacks named refusal, interruption, and unavailable/stale-choice
observations. The cumulative-diff/ARCH-boundary judgment was **not run**:
this evaluator was expressly forbidden to read source or the diff.

| Outcome | Verdict |
| --- | --- |
| 1 | cannot reach by the user's route |
| 2 | cannot reach by the user's route |
| 3 | cannot reach by the user's route |

## Outcome 1 — Round 5

> Works: The owner can run a five-project master plan through rollovers and restart without manually fixing project or permission selection.

Verdict: **cannot reach by the user's route**.

Evidence:

- The retained direct public-route capture above reached five registered owned
  projects in `remote-agents pane projects`, but `remote-agents serve --config
  ...` exited 1 with `remote_agents.config.ConfigError: Telegram environment
  file is missing` before a backend contact. Its `serve --help` exposed no
  isolated/no-Telegram transport option.
- The Round 5 brief's owner-observed/natively tested results do not give this
  evaluator an isolated Telegram user route. No real five-project rollover and
  restart was newly observable here.

## Outcome 2 — Round 5

> Invalid or unavailable input → a named refusal with existing sessions preserved.

Verdict: **cannot reach by the user's route**.

Evidence:

- The retained capture records public CLI validation refusals, but no existing
  provider session could be created or preserved because the public service loop
  stopped at the missing Telegram environment prerequisite.
- The brief records a limited owner observation of `destination-unregistered`
  with its source kept. That is not an independently reachable Telegram route
  in this dispatch and cannot replace the requested user-route demonstration.

## Outcome 3 — Round 5

> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

Verdict: **cannot reach by the user's route**.

Evidence:

- The retained capture had no actual provider operation to interrupt after the
  service transport stopped; consequently no retry/hold screen or launch record
  was observable by the public route.
- The brief records a trust-hold owner observation, but the independent Telegram
  route remains unavailable here and the brief explicitly leaves the Telegram
  interruption/no-duplicate evidence open.

## Round 5 findings

- **Blocking — Outcomes 1–3 cannot be reached by the user's route:** the final
  combined gate still lacks an independently runnable isolated Telegram route;
  native tests, a binding proof, and owner-observed results are not substitutes.
  With Stage 3 as the final combined gate, this leaves the master goal's
  five-project/restart, named-refusal/preserved-session, and interrupted
  exactly-once outcomes unproven at the required user-route scope.
- **Material — unassessed architecture/diff criterion:** cumulative-diff
  compliance with ARCH-01/02/03/07 and absence of hidden installed-profile or
  deployment changes was not run, because the exact evaluation brief prohibited
  source and diff inspection.
- **Material — retained scope limitation:** the Round 5 brief reports an open
  Important claim-publication crash defect; this assessment did not execute it
  or reclassify it.

Background jobs: none started.

## Round 5 evidence-scope severity clarification — 2026-11-03

The two entries headed **Material** in the preceding follow-up are retained
measurement bounds, not unmet authored criteria and not defects requiring a
further demonstration. The evidence-scope verdict remains **PASS**.

- The gate requires an owner-operated named refusal with preserved sessions; it
  does not require a verbatim transcript. The owner explicitly confirmed the
  expected unavailable-folder/no-pane response, while the separate recorded
  session/pane evidence establishes the safe-refusal portion.
- The gate requires interrupted operation to have a safe retry or visible hold
  without an unrecorded second launch; it does not require every crash window.
  The trust-held pane interruption and one explicit safe retry were actually
  observed. Its daemon-restart and other-window limits are accurately disclosed;
  the separate historical native restart receipt remains separately attributed.

No unmet criterion is identified by either bound. They must not be read as
Blocking or Material findings, nor as a demand for new Telegram, native, or
crash-window demonstrations.

Background jobs: none started.

## Round 5 evidence-scope follow-up — 2026-11-03

Verdict for the remaining evidence-scope criterion: **PASS at its stated
scope**. This is a narrow follow-up to the final-gate judgment, not a
replacement for or revision of the preceding independent-route verdicts.

The evidence now distinguishes its layers explicitly:

- `latest-installation.json` records successful local installation of wheel
  `remote_agents-0.68.0-py3-none-any.whl`, tied to source commit
  `d5e64b7842437d2b19796e6f4ed7251e553fe74c`; it records 255 verified
  application files, unchanged dependencies and configuration, 875 preserved
  pre-existing session rows, preserved pane inventory, and an active service
  before and after the approved replacement.
- The canonical reconciliation records source-only qualification separately,
  including the historical native/binding receipts and the OD-4/OD-5 fix-scope
  source results. It expressly labels those as source/native evidence at their
  respective identities, rather than a new native rerun or deployment result.
- The owner-operated existing-Telegram receipts identify the installed commit.
  `corrected-rerun-result.json` records one ended attempt, no fixture pane or
  duplicate, no pre-existing state changes, and the owner's explicit
  confirmation of the instructed unavailable-folder/no-pane response. It
  accurately marks that confirmation as owner words, not a verbatim bot quote.
  `interruption-*.json` records an actual marker-verified Codex trust-held pane
  interruption, owner observation that it was absent, and exactly one explicit
  retry with the correct project root and saved `approve_for_me` preset; the
  receipt expressly limits this to a trust-hold pane interruption, not daemon
  restart, power loss, publication crash, or a completed model turn.

Accordingly, no fixture-only test or source result is presented here as a live
deployment result. The verified claim is local installed-candidate behavior
through the existing owner-operated Telegram service, with the stated fixture
folder and interruption limits; it is not a published release, a main-branch
deployment, a fresh paid native bundle, or a complete crash-window proof.

Residual findings:

- **Material — refusal wording evidence is owner confirmation, not transcript:**
  the corrected rerun deliberately does not reproduce a verbatim bot message;
  the supportable claim is the owner's explicit confirmation together with the
  recorded no-pane/state evidence, not a quoted bot response.
- **Material — interruption scope remains narrow:** only abrupt closure of a
  native trust-held pane followed by an explicit fresh retry was observed. The
  receipts do not demonstrate daemon restart, power loss, claim-publication
  recovery, or completed-turn interruption.

This follow-up resolves the prior **Material — unassessed
architecture/diff/evidence-scope** limitation only insofar as the named
source-versus-installed evidence-scope criterion is now documented. It does
not erase the preceding historical cannot-reach entries; their original
transport limitation is preserved as history.

Background jobs: none started.

## Executor fix-scope note: OD-4 recovery repair

This note is by the executor, not a changed evaluator verdict. The round5 Important claim-publication crash finding is repaired and tested at source fix scope (658affected checks and137security checks passed; exact review findings closed). Native receipts still refer to the pre-fix application base; no paid native rerun is claimed. The independent evaluator's cannot-reach verdicts and actual Telegram evidence gaps remain unchanged. Full source repair evidence is docs/acceptance/session-defaults-round5-crash-recovery.md.

## Executor note: separate architecture judgment completed

The evaluator's original Material architecture-not-run finding remains preserved above. A separate independent cumulative source review by recovery_boundary_review now passes 477a83e2..78f0501d against ARCH-01/02/03/07 and DEC-133/134/137/138, with no Critical/Important architecture finding. Its actual returned result is retained in docs/acceptance/session-defaults-round5-architecture-review.md. This addresses only that source judgment; the evaluator's user-route cannot-reach verdicts, the remaining Telegram observations and the final evidence judgment are unchanged.

## Executor note: OD-5 unavailable-folder refusal repair

Owner-operated Telegram acceptance exposed a generic readiness/pane warning for a known unavailable folder. The actual failure is retained in canonical recovery captures. The independently observed trust-hold native interruption plus one explicit Telegram fresh-launch retry passed with exactly one new session/pane in the same folder and preset; it does not measure daemon restart or claim-publication crash.

OD-5 approved a bounded source repair within the existing fifth round. Missing/unknown/non-directory/unresolvable folders now keep their pre-pane reason through the application to both interfaces; 875 affected checks pass, both focused reviews approve. Source report: docs/acceptance/session-defaults-round5-folder-refusal.md. This source fix is not installed and the actual Telegram refusal rerun/final evidence judgment remain open. This executor note does not change the evaluator's preserved cannot-reach verdicts or convert the observed failure into a pass.

# Recovered current Stage 3 Outcomes assessment — 2026-11-04

This is the owed current assessment within original round 5. It appends to,
and does not revise, the earlier evaluator's historical cannot-reach entries:
those describe the evaluator's then-unavailable isolated transport. The current
verdicts use the subsequently retained owner-operated native and Telegram
receipts under OD-3's input-bound evidence-reuse/fix-scope authority. No product,
provider, model, bot, service, or test command was run for this assessment.

| Outcome | Current verdict |
| --- | --- |
| 1 | holds |
| 2 | holds |
| 3 | holds |

## Outcome 1 — current

> Works: The owner can run a five-project master plan through rollovers and restart without manually fixing project or permission selection.

Verdict: **holds**.

Evidence:

- The retained owner-observed live Claude routing journey records exactly one
  successor in the stated project and folder, with the real five-project journey
  separately disclosed as unseeded; the Stage 3 brief preserves its commit and
  limits.
- The owner native receipt `ra-live-owner-20261102T171948.log` is retained as
  12 passed at its measured base, and the separate binding proof as 5 passed;
  neither is relabelled as a new run.
- The owner-run `QdkpW5` native receipt on Codex 0.166.0 directly records
  real-catalogue outside-folder resolution, manual resume, and restart. Its
  actual checks passed project, thread, folder, preset, intent, cwd, argv, and
  one-running-successor checks. The raw result remains **59 PASS, 1 FAIL, 2
  BLOCKED**, not a fabricated 62-pass result. The failed
  `native.history_ready` predicate required the legacy `has_user_event=1`;
  the recorded later row had `preview_nonempty=1` and the pane showed the
  actual reply. The separate correction assessment limits this to an invalid
  diagnostic prerequisite; it does not overwrite the raw receipt.
- The final source validation records the exact four-file master goal command
  as 112 passed at `fa401c23`; its runtime implementation is stated identical
  to installed `d5e64b78` except the version string. This is source
  qualification, not a substitute for the owner-operated evidence above.

Limit: the original first-session post-message idle observation is explicitly
NOT OBSERVED, and the amended predicate was not rerun natively. Neither is an
authored Outcome requirement; the directly observed catalogue/manual/restart
functional checks are the relevant owner journey evidence.

## Outcome 2 — current

> Invalid or unavailable input → a named refusal with existing sessions preserved.

Verdict: **holds**.

Evidence:

- `corrected-rerun-result.json` records the owner confirmation, after the
  instructed Codex press on the existing Telegram bot, of the unavailable-folder
  / no-pane response on installed candidate `d5e64b78`. It labels the owner's
  words as confirmation, not a fabricated verbatim bot quote.
- The same receipt records one ended attempt, no fixture pane or duplicate, and
  no pre-existing session-state change. `latest-installation.json` ties the
  installed candidate to `d5e64b78` and records preserved pre-existing rows,
  pane inventory, configuration, and dependencies.
- `unavailable-result.json` remains retained as the earlier generic-message
  failure; it is not erased or represented as a pass.

Limit: the owner confirmation is not a literal transcript. The authored
criterion requires owner-operated named refusal and preserved sessions, both of
which are evidenced; it does not require a verbatim bot-message capture.

## Outcome 3 — current

> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

Verdict: **holds**.

Evidence:

- `interruption-before.json`, `interruption-action.json`, and
  `interruption-after.json` record a real trust-held Codex pane, its abrupt
  closure, ended original session, no fixture panes, unchanged pre-existing
  states, and unchanged service identity.
- `interruption-owner-observation.json` records the owner's `absent`
  observation. `interruption-retry-result.json` records one explicit retry:
  one new untrusted session/pane in the same project root with the saved
  `approve_for_me` preset; the original ended and no automatic or duplicate
  successor existed.
- The earlier owner native restart receipt remains separately attributed, and
  `QdkpW5` additionally directly passed restart target/project/thread/folder/
  preset/intent/cwd/argv and single-running-successor checks.

Limit: the Telegram interruption was an abrupt native trust-hold pane closure,
not a daemon/power-loss/publication-crash or completed-turn scenario. Those are
not additional authored Outcome requirements and are not claimed.

## Current findings

**PASS.** No Blocking, Material, or Minor finding is identified for the three
authored Outcomes at their stated evidence scope. The limits above remain
disclosures, and all earlier failed/cannot-reach evidence remains preserved.

Background jobs: none started.
