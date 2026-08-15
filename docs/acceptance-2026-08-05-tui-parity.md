# Acceptance: the local terminal reaches parity with the Telegram control plane

Date: 2026-08-05
Release: 0.5.0
Plan: `2026-08-05-tui-bot-parity-plan.md`

Before this work `remote-agents tui` was a launch wizard: it chose a project and an agent,
launched, and exec-ed into the pane. Every post-launch capability the bot had — the session list,
detail and state, copy attach, graceful/cleanup/force stop, Claude Remote Control, inspect output,
and resume — was absent from it, so a host with no Telegram credentials and no running `serve`
could start work and then do nothing else with it. This records what was verified on this machine.

## What was verified against real tmux on this host

`tests/live/test_tui_parity.py`, opt-in behind `REMOTE_AGENTS_LIVE_ACCEPTANCE=1`, **run and
passed** — 3 passed in 9.38s:

- `test_the_terminal_manages_a_session_the_service_started` — a session is launched through a
  composition standing in for the service, on its own database connection. Everything after that
  is driven through the terminal's own composition on a *second* connection: it lists the session
  it did not start, reads its state and the explanation for it, renders the copy-attach command
  and asserts it equals `" ".join(attach_argv(session_id))` byte for byte, captures the pane's
  output through the shared `sanitize_terminal_text`, gracefully stops it (asserting the policy
  allows `graceful` from `RUNNING`), then cleans it up (asserting `cleanup` is what `PRESERVED`
  offers) and confirms the service-side connection sees `ENDED`.
- `test_the_terminal_force_stops_a_second_session_it_started` — a second real session, force
  stopped from the terminal, ending `ENDED`.
- `test_the_terminal_lists_resume_capable_agents_without_resuming_anything` — the resume read
  path against the real host: the terminal composition wires a `ConversationService`, and every
  capability it reports is truthful about itself (any profile reporting no catalogue carries a
  reason).

### Safety of the run

The tests only ever act on session ids **they launched in the same run**; they never list-and-stop,
and every session started is retired in a `finally` even on assertion failure. Verified directly
around the run: the owner's live session `ra-9cb5c8aa-e603-41f8-9f98-81297475bb77` was present
before and after with an unchanged creation timestamp, and afterwards it remained the only
non-`ended` row in the store. Session rows went 33 → 38 across the whole session, all of the new
ones `ended`.

### The first attempt failed, and why

The first run failed both destructive tests at launch with `SessionState.FAILED`. The cause was in
the test, not the product: its service-side helper built a `ProjectCatalogueProvider` and read
`.paths` without calling `refresh()` first. That routing table is empty until refreshed (0 entries
before, 94 after on this host), so the terminal could not resolve any project's directory and every
launch through it failed immediately. The pre-existing
`test_add_project_and_tui_journey.py::test_a_terminal_launch_attaches_and_stops_from_a_second_connection`
was run as a control and **passed**, which is what localized the fault to the new test. The helper
now refreshes before reading, and the idempotency keys carry a UUID so the acceptance can be re-run
on the same day.

## What is not covered here, and is deliberately an owner step

- **Resume of a real conversation.** The read half — capabilities and catalogue — is exercised
  above. Actually resuming would start a second live agent pane against a real saved conversation,
  so it stays an owner-driven step. All five profiles on this host currently report
  `capability_unqualified` for resume (`doctor --profiles`), so there is nothing resumable to
  select here regardless.
- **Driving the flows through the Telegram bot itself.** `BL-002` already tracks the full mobile
  owner journey and is unchanged by this work.
- **The keyboard journey through the running app.** Covered at the unit tier by Pilot, including
  the destructive-reachability checks; this file records the service-boundary behaviour against
  real tmux.

## Nothing in the backlog is closed by this

`BL-001` and `BL-002` remain open and Telegram-scoped. This work opened `BL-004`, `BL-005`,
`BL-006`, and `BL-007`.

> Later note, added 2026-08-15: this section describes the state of the backlog on
> 2026-08-05 and is left as written, because it is a dated record of one acceptance run
> rather than live documentation. `BL-004`, `BL-005` and `BL-006` have since been closed by
> the 2026-08-13 backlog-closure plan; the identifiers above no longer resolve to open
> entries.
