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
