# Acceptance: project creation and the local terminal surface

Date: 2026-08-05
Release: 0.4.0
Plan: `2026-08-04-add-project-and-local-tui-plan.md`

Two capabilities were added: creating and registering a project from the command line, the
Telegram bot, or the local terminal; and a local terminal surface that launches a curated agent
and then attaches to its pane.

## What was verified unattended on this host

Every check below was run against this machine's real configuration, and each one restored what
it touched.

**Project creation, end to end against the real registry.** `remote-agents add-project --area
infra --name ra-plan-probe` created `~/dev/infra/ra-plan-probe`, appended one entry, and printed
the canonical path. `doctor --json` went from `registered 86 / catalogue 95` to `87 / 96` and
stayed `healthy: true`; the appended block was the last five lines and matched the file's existing
conventions. A second identical run exited 1 with `project directory already exists` and changed
nothing. Removing the five appended lines and the directory returned the registry to a
byte-identical file (md5 `<redacted>` before and after) and the counts to
`86 / 95`.

The same journey through the application service is covered by the opt-in test
`tests/live/test_add_project_and_tui_journey.py::test_a_created_project_reaches_a_running_catalogue_and_is_then_removed`,
which was **run and passed** on this host: it created the scratch project against the real
registry, proved a live `ProjectCatalogueProvider` picked it up as `Registered` with its path
resolvable, then removed the entry and the directory and asserted the registry was byte-identical.

**The terminal on the real catalogue.** `RemoteAgentsTui` was driven headlessly against the
production composition. It rendered all 95 catalogued projects, registered before unregistered,
and listed all five curated profiles. Driven by keystrokes only, the filter accepted a full
multi-character query, enter moved into the list, and every list step took the keyboard with its
first row highlighted — including the Review screen, which opens on its non-mutating row.

**The attach handoff against real tmux.** On a disposable socket, the exact argument vector the
terminal execs was confirmed to be what reaches `execvp`, and `tmux has-session` accepted the
target form `ra-<session>:`. Started with `$TMUX` set, the handoff refused to nest and printed the
command instead.

**Cross-surface management.** `tests/e2e/test_resilience.py::test_a_second_process_stops_a_session_it_never_launched`
runs real tmux with a fake agent and proves a terminal launched by one process is gracefully
stopped by a second one composed the way the composition root composes it. It was verified to fail
without the fix it guards.

## What still needs the owner

The Telegram half of the journey was not driven. This host was running two of the owner's own live
`claude-remote` sessions at acceptance time, and the remaining checks would add a third real agent
session to a control plane in active use, so they were left opt-in rather than run:

```bash
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 \
  uv run --locked pytest -m live_acceptance tests/live/test_add_project_and_tui_journey.py -q
```

`test_a_terminal_launch_attaches_and_stops_from_a_second_connection` launches one real managed
session through the terminal's own composition, checks the attach vector, then gracefully stops
and cleans it up from a second connection standing in for the service. Run it when no session is
in flight.

Then work the local-terminal checklist in the operator runbook by hand. Two steps matter most:

- **Step 5, the graceful stop from Telegram.** Until this release, the bot met a session it had
  not itself launched with `unknown_session`: it sent no keys, left the session running, and still
  replied that the stop had completed. That silent failure also affected the bot's own sessions
  after any service restart. It is fixed and covered by tests, but it is the one path that used to
  lie, so confirm by hand that a terminal-launched session actually stops from Telegram.
- **Step 4, the attach and detach.** Confirm the terminal is replaced by the pane, that detaching
  leaves the session running and still listed in Telegram, and that the agent is the one chosen.

## Known limits at this release

- A record left in `STARTING` is not manageable from the bot (**BL-003**). The trigger is narrow —
  a launch that raises after the record is saved but before readiness — and recovery is
  `tmux -L remote-agents kill-session` by hand.
- The complete mobile owner journey through Telegram still relies on an owner witness
  (**BL-002**), unchanged by this release.
- A project created by the command line appears in a running bot only after a refresh, because the
  two are separate processes with their own catalogues. This is documented in both the README and
  the runbook.
