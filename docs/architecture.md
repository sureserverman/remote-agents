# Architecture

This describes the tree as it is built, not a design it is meant to grow into. Every
structural claim below was checked against `src/remote_agents/`,
`tests/architecture/check_imports.py` and `src/remote_agents/bootstrap.py` before it was
written down; where the code and the intent behind it differ, the code is what is described
and the difference is said out loud. It claims only what it checked (DEC-019).

The decision register it cites is not in this repository. It lives at
the owner's private decision register, and a `DEC-NNN` here is a
pointer into it. What this document adds is the part the register cannot carry: the shape the
four sub-plans of the "one backend, two frontends" refactor left behind, stated once, in one
place, instead of being reachable only by grepping module docstrings for a decision id.

## The four layers, and the one rule a script enforces

`src/remote_agents/` has four layers, plus a package root that is not one of them:

| Layer | Directory | May import from |
| --- | --- | --- |
| `domain` | `domain/` (8 modules) | `domain` only |
| `ports` | `ports/` (14 modules) | `domain`, `ports` |
| `application` | `application/` (23 modules) | `application`, `domain`, `ports` |
| `adapters` | `adapters/` (60 modules, six families) | `domain`, `ports`, and its own family |

`tests/architecture/check_imports.py` parses every module under `src/` with `ast`, resolves
relative imports to absolute names, assigns each module a layer from its path, and reports
every internal import that crosses one of those boundaries inward-only rules. It is not a
grep: it reads the import statements, so a decision id mentioned in prose is not a hit and an
aliased import is still an import. `tests/architecture/test_check_imports.py` runs it over
`src/` itself — the caller the script would otherwise never have — and the tree currently
reports zero violations.

Two exceptions are written into the checker, and both are narrow:

- **Driver adapters may also import `application` and `config`.** `DRIVER_ADAPTERS` is
  `{"telegram", "tui"}`. The other four adapter families — `agents`, `projects`, `sqlite`,
  `tmux` — may not; they see `domain`, `ports` and themselves. An adapter family may never
  import another adapter family, driver or not.
- **The modules that may compose adapters are enumerated by name.**
  `COMPOSITION_ROOTS = {"bootstrap.py", "agent_event.py"}` — a closed set, not a position.
  A module at the package root that is *not* in that set — `__init__.py`, `__main__.py`,
  `config.py`, `production.py`, `service_probe.py` — may import only the composition roots
  themselves, `config` and `production`; it may not reach an adapter. This is DEC-015: *"The rule is that
  the set stays closed and enumerated, not that it has one member."* `agent_event.py` exists
  as a second root precisely so the hook command installed into the operator's global agent
  settings — which fires in every Claude session on the machine — does not import
  `bootstrap` to be told it has nothing to do. Two constraints travel with that entry and are
  live in the tree: `agent_event` imports nothing from `bootstrap` (its only imports are
  `argparse`, `sys` and `pathlib`), and `__main__.main` stays at module scope, because
  `[project.scripts]` resolves `remote_agents.__main__:main` and deferring the *definition*
  rather than the imports breaks the console script and the systemd unit with it.

`check_imports.py` is the only place any of this is enforced. Everything in the four sections
below is a rule the type system does not hold, guarded — where it is guarded at all — by the
tests in `tests/architecture/`.

## ARCH-B1 — one `Backend`, and both frontends receive it

`application/backend.py: Backend` is a frozen, slotted dataclass carrying nine fields:
`sessions`, `projects`, `conversations`, `catalogue`, `refresh_catalogue`, `profiles`,
`capture`, `activity_feed`, `max_label_length`. It is the whole set of use cases a frontend
may drive. Before it, `bootstrap` composed the Telegram service and the local surface
separately — two `SessionService` instances over one SQLite file, two catalogue providers,
two profile probes — and only one of the two halves was typed at all: `PrivateBotBoundary`
declared its launcher `object | None` and reached into it by name, so a capability the
composition root forgot to wire produced no error, just a row that quietly stopped being
offered on one surface.

The module imports `application.profiles`, `application.project_catalog`, `domain.models` and
`ports.agent_activity`, and nothing else from the package. An adapter type here would be an
ARCH-02 violation the checker fails on (DEC-015), and the mistake is not hypothetical:
`bootstrap.LocalRuntime` used to be typed against a Telegram wizard type and hand it to the
local surface, which converted it back.

**Three fields are typed `object | None` today, deliberately and temporarily.** `sessions`,
`projects` and `conversations` name their real types only in their docstrings
(`application.services.SessionService`, `application.project_admin.ProjectCreationService`,
the conversation service); naming them in the annotations would be correct and would pull the
whole port graph into every module that reads a `Backend`, including both frontends' test
doubles. The field docstrings say this is for one release. Read `Backend` as documented rather
than as annotated on those three.

**Optionality is a record of what a process wired, not a licence to skip wiring.** The bot's
boundary has always answered "that is unavailable" rather than failing to start — at thirteen
guarded entry points, on the count `backend.py` and `TuiContext` both record — so the type has
to be able to represent a host that wired nothing. The
local surface takes the opposite contract and enforces it: `TuiContext.__post_init__` refuses
a backend missing `sessions` or `projects`. Nothing anywhere probes for a capability by name
any more — absence is a declared field checked as `is None`, which is what
`tests/architecture/test_frontends_share_one_backend.py` rule 2 exists to keep true.

## ARCH-B2 — composed once per process, not once globally

`bootstrap.compose_backend(config, connection, paths, ...)` is the single function that builds
a `Backend`, and it is called twice in production: once from `_private_boundary` (the `serve`
composition) and once from `local_context` (the local surface composition). What used to be
four call sites that happened to agree is one, so a capability added for one surface cannot
miss the other.

**The connection is the caller's, and `compose_backend` never opens one.** This is the
sharpest constraint on the shape, and the two strategies must not be described as collapsed:

- `serve` opens one connection with `paths.open_database(...)` and holds it for the life of
  the process.
- A surface process opens `leased_connection(config.database_path)` — `LeasedConnection` in
  `adapters/sqlite/database.py`, per-operation open and close, nested transactions refused
  loudly (`RuntimeError("leased transactions do not nest")`), a transaction owned by the
  asyncio task that opened it and refused to any other. This is DEC-035, which
  replaced the old "the terminal execs away" guarantee with a narrower and stronger one: the
  surface may now live indefinitely beside attached sessions because it holds no database
  handle between store operations. The README states the same guarantee in the same words.

DEC-035's guarantee is therefore a property of the connection handed in, not of the backend.
`tests/integration/test_composition_lifecycle.py` pins both halves —
`test_compose_backend_opens_no_connection_of_its_own` and its neighbours. A `compose_backend`
that opened its own handle would not be a tidy-up; it would remove the thing that makes the
concurrent writer count safe (DEC-005 as corrected, below).

Four of its keyword parameters exist so the caller can share objects it needs anyway rather
than have them built twice: `projects` and `runtime` (the profile probe shells out once per
profile, and the caller needs the terminal and gateway regardless), `store` (the service's
reconciler and quiet watcher are meant to be looking at the same one), and `activity_feed`
(its bound lives in the terminal package, and importing it here would make `serve` load the
terminal library at composition time). Passing `projects` in does **not** skip a catalogue
refresh — `compose_backend` always calls `refresh()`, so the backend's snapshot is its own.

## ARCH-B3 — per-surface wiring stays per-surface

Anything only one process has stays out of `Backend` and is wired by that process's own
composer.

**`serve` owns**: the shared `SessionLocks` instance — constructed exactly once in
`bootstrap._private_boundary` and handed to both `SessionService` and
`ReconciliationService`, which is the whole of DEC-030's fix; the `ReconciliationService`
itself, constructed in exactly one place in `src/`; the pane quiet watcher; and the durable
Telegram stores (callbacks, chat-view anchors, standing notifications).

`SessionService.__init__` still falls back to `locks or SessionLocks()`, so the local
surface's service builds a lock map of its own. That is per-process by design and is not
cross-process serialization — see the process model below.

**The local surface owns**: console *hosting*. `local_context` builds a `ConsoleComposer` only
when `hosting_mode(os.environ)` says this process is inside the console, and wires
`open_in_console`, `console_sync`, `console_flash` and a `RecoveryReport` from it; and
`attach_argv`, which goes onto `TuiContext` rather than onto `Backend` (DEC-039 decides what
that command names; DEC-040 is the exchange model it names it under).

**Both processes wire `hide_in_console`, from two different composers**, and the asymmetry is
in what each composer is allowed to do rather than in who has one. Both are built by
`_console_composer`, so both name the same lock file. The surface's composer builds and
arranges the console — it calls `ensure()`, and the recovery report and the layout come from
it. The bot's is hide-only: nothing in `_private_boundary` calls `ensure`, and `hide` degrades
to nothing on a host with no console at all, which is every host that has never run
`remote-agents`. It exists for one operation — stepping the console out of the way before a
stop destroys a pane — because without it, stopping a displayed session from a phone left the
agent's pane to be killed inside the console window and the console sat a pane short until its
next reload.

`hide_in_console` reaches `SessionService` as a composition-time argument, not as a `Backend`
field, so each process gets whatever its own composer wired and neither stop dispatch has to
know which it got.

## ARCH-B4 — what a shared use case is

A shared use case is a function under `application/` that both frontends call. It takes an
already-decided input and returns a value the frontend renders. It does not call back into a
screen, it does not mint or scope a callback token, and it reaches neither PTB nor Textual —
no module under `application/` imports either library, and every occurrence of "mint" or a
callback-state name there is prose.

The three modules the refactor created or consolidated under this shape:

- `application/stops.py` — the one stop dispatch. Ending a session used to be written twice,
  in `adapters/telegram/stops.py` and `adapters/tui/app.py`, against the same vocabulary, and
  the two drifted for six days on the one path that destroys a session. The re-read of the
  record lives here rather than in the caller, because DEC-007 and DEC-008 both say that what
  refuses a second stop is re-reading and re-checking at issue time. It never asks a question:
  DEC-025 says a confirmation is only ever awaited from a screen's own handler, so a shared use
  case taking a confirmation callback would be the forbidden shape with an extra step. The bot
  keeps its two-press force confirmation and the surface keeps its modal. It reports rather
  than renders — the outcome carries which refusal happened, and each surface writes its own
  sentence from it.
- `application/session_actions.py` — the single authority over which lifecycle actions a
  session offers. Availability is a narrowing of the domain's legal transitions, never a
  widening, and `tests/architecture/test_policy_matches_domain.py` enforces that direction.
- `application/resume_flow.py` — the page size and the capability filter, each of which
  existed three times. The flow itself deliberately stays per-surface: the bot composes its
  own unavailability wording and mints its own tokens (DEC-011), and the local surface keeps
  its reads inside its navigation guard.

`tests/architecture/test_frontends_share_one_backend.py` rule 3 pins that these names are
defined once, under `application/`, and that an adapter does not redefine one. That file also
states its own limits, at length: it is a static lexical sweep over
`src/remote_agents/adapters/`, and it does not see a shared use case copied into an adapter
under a different name.

## DEC-044 — the rule moves, its state does not

This is the entry that explains why `application/` modules take containers as arguments, and
it is easy to misread the layering without it. When a rule is lifted out of a driver adapter
into `application/`, the **rule** moves and the **state it operates on stays behind**.

`application/notification_policy.py` is the worked example. `ActivityNotifier` in
`adapters/telegram/notifications.py` still owns its suppression map and its backlog deque; the
policy module holds the taper, the reset rule, the retention floor, the grouping collapse and
the eviction rule, and is handed the container to apply them to. Every clock reading arrives
as an argument — nothing there reads a clock, a bot, a session store or a socket — which is
what lets DEC-031's eight-hour proof be a loop over integers rather than a fake clock threaded
through a Telegram double.

Two consequences a reader should carry: the module is **clock-free but not side-effect-free**
(`grouped_for_delivery` is pure; `record_sent`, `forget_expired` and `enqueue` mutate the
caller's container), and encapsulation cannot enforce the split, so the guard is a test that
sweeps for the *write into the container* rather than for the type of what is written.

## The process model — one `serve`, three pane processes, one SQLite file

Every process this project runs opens the same database file, and refuses to open any other:
both `serve` and `_run_surface` go through `_private_state_config`, which raises unless
`config.database_path` is exactly the private state directory's. Sharing the store is not a
configuration accident.

- **`remote-agents serve`** — one process. It holds one long-lived connection and runs the
  Telegram bot, the reconciler, the pane quiet watcher and the activity poller over it.
- **`remote-agents pane <name>`** — one process per console pane. `PANE_NAMES` is
  `("projects", "sessions", "feed")` and the parser's `choices` is keyed off it, so the count
  is three and cannot drift from `PANE_SURFACES`. A Textual app owns a terminal, so the
  console's three tmux panes are three processes rather than three widgets; each composes the
  same surface through `_run_surface` over its own `LeasedConnection`.
- **`remote-agents tui`** — the combined dashboard, the same `_run_surface` body with a
  different runner, for a bare shell outside the console.

**On the writer count, the register and this document agree on the number and not on the
unit, so both are given.** DEC-005 as corrected 2026-08-20 says there may be **up to five
concurrent writers: the bot, the reconciler, and one process per console pane** — five
writers across four processes, because the bot and the reconciler share `serve`. The count of
concurrent *surface processes* the tree supports is three (the console's panes); a
`remote-agents tui` in another terminal would compose a fourth, and nothing prevents it, but
no code or decision enumerates that arrangement. The reasoning is what carries either way, and
it is DEC-035's lease: no surface holds a handle between store operations, so more writers is
more contention and not a new hazard.

What that costs, and what it does not, is set out in `docs/operator-runbook.md` under
"Terminal and service on one database". In short: duplicate-command protection *is* durable
across processes, because every launch claims an idempotency key with a unique insert
(DEC-011's `claim_mutation` is the same shape and for the same reason). The per-process
`SessionLocks` are *not* — each `SessionService` constructs its own, so the reconciler's
shared-lock fix (DEC-030) covers `serve` and does not extend across the process boundary. On a
single-owner host that costs nothing; a writer that cannot take the SQLite file lock within
one second fails rather than waits, which surfaces as a reported error rather than a damaged
record. Each process also holds its own catalogue snapshot and its own profile probe, taken
when it started, which is why two surfaces can disagree about which projects or agents exist
without either being wrong.
