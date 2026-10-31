# Stage 2 product demo

Environment: wheel built from this checkout with `uv build --wheel --out-dir
/tmp/stage2-routing-demo/dist`; all product state and configuration were bound to
`/tmp/stage2-routing-demo`. No source, diff, test hook, or test suite was used.

## Outcome 1

> Works: A destination session remains discoverable and resumes in its own folder and preset after ending or interruption.

Verdict: holds

Evidence:

```text
$ timeout 10s bwrap ... remote-agents tui --config /home/user/.config/remote-agents/config.toml
project registry is degraded: registry_unavailable
[terminal surface] No sessions are running.
[terminal surface] Projects 0 · recent first
exit=124 (the interactive terminal surface was deliberately stopped after ten seconds)
```

The built public CLI exposes `tui`, but its empty isolated owner route contains no
project or session from which an owner can choose Resume. The public CLI help exposes
no shell resume or conversation-import command. This early evidence did not itself
reach a populated terminal Resume catalogue; the later README and TUI observations
below establish the terminal route. I therefore did not substitute a test, internal
entry point, or fabricated session.

Follow-up configured-fixture evidence (the original empty-fixture evidence above is
preserved):

```text
$ bwrap ... remote-agents add-project --config /home/user/.config/remote-agents/config.toml --area demo-area --name registered-project
note: created a new projects registry at /tmp/stage2-routing-demo/registry.yaml
/tmp/stage2-routing-demo/projects/demo-area/registered-project
exit=0

$ git -C /tmp/stage2-routing-demo/projects/demo-area/registered-project init
Initialized empty Git repository in /tmp/stage2-routing-demo/projects/demo-area/registered-project/.git/
exit=0

$ timeout 6s script -q -c 'bwrap ... remote-agents tui --config /home/user/.config/remote-agents/config.toml' /tmp/stage2-routing-demo/tui-registered.typescript
[terminal surface] registered-project  registered
[terminal surface] No sessions are running.
exit=124
```

The configured public route removes the earlier `registry_unavailable` condition and
the dashboard discovers the owned registered project. It still visibly reports
`No sessions are running` and its Sessions count is zero, so there is no catalogue
entry to select in the terminal Resume interaction. The README documents the
terminal `Sessions · Launch · Resume` navigation row as well as Telegram `/resume`;
the absence of a shell `resume` subcommand is not treated as evidence that the
terminal interaction is unavailable. The real provider-backed conversation needed
to populate a native resume candidate remains outside this isolated demo.

Native provisioned-route attempt (2026-10-28):

```text
$ /tmp/ra-stage2-native-34l79izo/bin/remote-agents tui --config /tmp/ra-stage2-native-34l79izo/config/config.toml
[TUI command palette] Resume — Reopen a saved conversation as a new session
[TUI] Choose the project whose conversation you want to reopen.
[selected project] demo/destination
[TUI] Choose the agent whose conversation you want to resume.
[selected agent] codex
[TUI] No saved conversations for this agent and project.
```

The fixture's supplied native seed completed a synthetic Codex conversation in the
same destination directory. The public TUI nevertheless refuses to list it.
Fixture-owned SQLite observation showed no rows in `sessions`,
`session_permissions`, `session_project_roots`, `session_folders`, or
`session_events`; no state was inserted or altered by this evaluator. As a result,
the saved conversation cannot be selected and no destination launch, folder, or
preset can be observed.

The initial empty catalogue has since been diagnosed as a fixture-source mismatch:
that first synthetic seed was an `exec` conversation, while the provider's default
interactive catalogue returns the later synthetic CLI conversation. No application
filter or configuration was changed.

Native interactive retry (2026-10-28; corrected selection observation):

```text
[TUI Resume → demo/destination → codex]
Reply with readiness token · resumable · 1m ago
Codex launch permissions · Saved preset / current default

[Enter while the default highlighted row was `Back`]
[TUI returns to] Choose the agent whose conversation you want to resume.
```

The selectable interactive conversation was discovered at the correct project and the
UI displayed its saved-preset/current-default relationship. The above return to the
agent picker was not a conversation launch: the list intentionally initially highlights
`Back`, and that was the row entered. It is preserved as a prior attempt, but is not
evidence of a launch failure.

On the repeat, I pressed Down once from that resting `Back` row, which visibly wrapped
the highlight to `Reply with readiness token · resumable · 4m ago`, and only then
pressed Enter:

```text
$ /tmp/ra-stage2-native-34l79izo/bin/remote-agents tui --config /tmp/ra-stage2-native-34l79izo/config/config.toml
[TUI Resume -> demo/destination -> codex]
[highlight after Down] Reply with readiness token · resumable · 4m ago
[Enter]
OpenAI Codex (v0.165.0)
/tmp/ra-stage2-native-34l79izo/projects/demo/destination
STAGE2_INTERACTIVE_READY
```

The terminal was handed to the managed resumed pane at the destination folder. Read-only
observations of the owned fixture immediately after the attach showed exactly one pane
and one running session bound to the selected native conversation and destination root:

```text
$ /tmp/ra-stage2-native-34l79izo/bin/tmux list-panes -a -F '#S:#I.#P #{pane_pid} #{pane_current_command} #{pane_current_path}'
ra-32a7e7d1-1406-4831-85f9-24b284068eca:0.0 232 bwrap /tmp/ra-stage2-native-34l79izo/projects/demo/destination

$ sqlite3 .../sessions.sqlite3 'select session_id, project_id, profile_id, state, resume_profile_id, resume_source_id from sessions; ...'
32a7e7d1-1406-4831-85f9-24b284068eca  490c08f78e937874333b11c4  codex  running  codex  01a184aa-bf13-7aa1-a73a-a7dd26d9e7a5
[session_permissions] 32a7e7d1-1406-4831-85f9-24b284068eca  approve_for_me
[session_project_roots] 32a7e7d1-1406-4831-85f9-24b284068eca  /tmp/ra-stage2-native-34l79izo/projects/demo/destination
```

This is a completed native conversation discovered and resumed by the public TUI. The
visible pre-launch row stated `Saved preset / current default`, and the resulting
session records `approve_for_me`; the pane and persisted project root both use the
destination folder.

## Outcome 2

> Invalid or unavailable input → a named refusal with existing sessions preserved.

Verdict: holds

Evidence:

```text
$ bwrap ... remote-agents add-project --config /home/user/.config/remote-agents/config.toml --area invalid --name unavailable
area is not an existing development-root directory
exit=1
```

This is the original named-refusal observation. After the actual native resume above,
I repeated the public CLI refusal without touching its managed pane:

```text
$ /tmp/ra-stage2-native-34l79izo/bin/remote-agents add-project --config /tmp/ra-stage2-native-34l79izo/config/config.toml --area /tmp/ra-stage2-native-34l79izo/projects/not-a-development-root --name unavailable
area must be lowercase letters, digits, and single hyphens (1 to 64 characters)
exit=1

$ /tmp/ra-stage2-native-34l79izo/bin/tmux list-panes -a -F '#S:#I.#P #{pane_pid} #{pane_current_command} #{pane_current_path}'
ra-32a7e7d1-1406-4831-85f9-24b284068eca:0.0 232 bwrap /tmp/ra-stage2-native-34l79izo/projects/demo/destination

$ sqlite3 .../sessions.sqlite3 'select session_id, project_id, profile_id, state, resume_profile_id, resume_source_id from sessions;'
32a7e7d1-1406-4831-85f9-24b284068eca  490c08f78e937874333b11c4  codex  running  codex  01a184aa-bf13-7aa1-a73a-a7dd26d9e7a5
```

The malformed-area control received a specific refusal (exit 1), while the
pre-existing resumed session and its single destination pane remained unchanged. That
generic validation is not sufficient for this Resume outcome. I then exercised the
Resume feature itself after an ordinary terminal detach from the managed pane:

```text
[managed terminal] [detached (from session ra-32a7e7d1-1406-4831-85f9-24b284068eca)]

$ /tmp/ra-stage2-native-34l79izo/bin/remote-agents tui --config /tmp/ra-stage2-native-34l79izo/config/config.toml
[TUI Resume -> demo/destination -> codex -> highlighted Reply with readiness token]
[Enter]
no sessions

$ /tmp/ra-stage2-native-34l79izo/bin/tmux list-panes -a -F '...'
no server running on /tmp/ra-stage2-native-34l79izo/tmux.sock

$ sqlite3 .../sessions.sqlite3 'select session_id, state, resume_source_id from sessions;'
32a7e7d1-1406-4831-85f9-24b284068eca  running  01a184aa-bf13-7aa1-a73a-a7dd26d9e7a5
```

The destination conversation remained a selectable Resume input even though the
attached managed pane was unavailable. Enter resulted only in the context-free `no
sessions`, not a named unavailable-session refusal; the stale `running` record did not
preserve a usable existing session.

Reassessment on rebuilt artifact `70a4ff8d` (wheel SHA256
`333f42149b67c79b58b1b98ef94af118261fb6c6d403d2724214cd465be19226`): the
unchanged stale record was reached again by the public TUI, with the actual conversation
row visibly highlighted before Enter.

```text
$ /tmp/ra-stage2-native-34l79izo/bin/remote-agents tui --config /tmp/ra-stage2-native-34l79izo/config/config.toml
[TUI Resume -> demo/destination -> codex -> highlighted Reply with readiness token]
[Enter]
Resume is on hold because the existing session's pane could not be verified.
Nothing new was started. Open Sessions to inspect it.

$ /tmp/ra-stage2-native-34l79izo/bin/tmux list-panes -a -F '...'
no server running on /tmp/ra-stage2-native-34l79izo/tmux.sock

$ sqlite3 .../sessions.sqlite3 'select session_id, project_id, profile_id, state, resume_profile_id, resume_source_id from sessions; ...'
32a7e7d1-1406-4831-85f9-24b284068eca  490c08f78e937874333b11c4  codex  running  codex  01a184aa-bf13-7aa1-a73a-a7dd26d9e7a5
[session_permissions] 32a7e7d1-1406-4831-85f9-24b284068eca  approve_for_me
[session_project_roots] 32a7e7d1-1406-4831-85f9-24b284068eca  /tmp/ra-stage2-native-34l79izo/projects/demo/destination
```

The repaired user route gives a specific unavailable-pane hold and starts no second
pane. The original record, provider source, preset, and destination root remained
unchanged. An earlier fast bundled-key attempt during this reassessment raised a
Textual `ScreenStackError` before selecting a conversation. Its exact command input
and traceback excerpt are retained in
`/tmp/stage2-routing-demo/2026-10-29-native-reassessment-fast-navigation-crash.txt`;
the paced user-route selection above completed without a crash.

## Outcome 3

> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

Verdict: holds

Evidence:

```text
$ timeout 10s bwrap ... remote-agents tui --config /home/user/.config/remote-agents/config.toml
project registry is degraded: registry_unavailable
[terminal surface] No sessions are running.
exit=124
```

The initial inaccessible-route evidence above is preserved. The later provisioned
native route allowed the completed conversation to be reopened. I then detached from
the managed terminal by its ordinary terminal detach control; the terminal visibly
reported `[detached (from session ra-32a7e7d1-1406-4831-85f9-24b284068eca)]`. The
fixture tmux socket was then unavailable, while the product's persisted session remained
`running`. Reopening the same highlighted conversation through public Resume printed
only `no sessions`; it did not provide a retry, a hold, a named recovery refusal, or a
second pane. This is a demonstrated interrupted-operation recovery failure, not a
manufactured database state.

The same actual interruption was reassessed through rebuilt artifact `70a4ff8d` using
the same public TUI selection recorded under Outcome 2. It returned the named visible
hold, preserved the existing source/preset/root record, and the dedicated socket still
reported no server. No replacement, reclassification, or second pane was observed.

## Findings

1. **Resolved during reassessment — an unavailable existing pane now returns a named
   visible hold.** The identical stale record reports that its pane could not be
   verified and that nothing new was started; source, preset, and destination root
   remain preserved and no second pane exists.

2. **Material — rapid public navigation can crash the TUI before a conversation is
   selected.** The exact immediate terminal write was `Ctrl-P`, `resume`, `Enter`,
   `Enter`, `Enter` (`\u0010resume\r\r\r`), and it displayed Textual
   `ScreenStackError: Can't pop screen; there must be at least one screen on the
   stack`. See `/tmp/stage2-routing-demo/2026-10-29-native-reassessment-fast-navigation-crash.txt`.
   The paced public route reached the intended hold and did not alter the stale record
   or create a pane, but the crash requires repair.

3. **Resolved during demo — the prior apparent native launch failure was selection of
   the intentionally highlighted `Back` row.** With the actual conversation row
   visibly highlighted, Enter launched one managed pane at the destination and created
   the persisted running session described above.

4. **Resolved during demo — the initial empty native catalogue was an `exec`-source
   fixture mismatch, not an application discovery defect.** A genuine interactive
   synthetic conversation appeared in the same TUI route after the provider returned
   it through its default interactive catalogue.

5. **Resolved during demo — the initial `registry_unavailable` message was an
   unconfigured isolated fixture, not a product defect.** The public `add-project`
   command created an owned registry and registered `registered-project` (exit 0);
   the same terminal dashboard then displayed it as `registered` and did not show
   `project registry is degraded`.

Product-demo result: **PASS with residuals.** The specified pytest gate was not run:
this evaluator was expressly limited to product-as-user demonstration and may not
replace that gate with tests.


## Executor fix-scope disposition (2026-10-29)

The independent Material finding above is retained. Its queued-submission and queued-Escape
paths reproduced ScreenStackError before repair. NavigationPalette now gates both on active
screen ownership and prevents duplicate inherited handler dispatch. All21palette tests pass,
including4resting/nested queued-input cases; the affected architecture arrangement controls
pass2/2. This is executor fix-scope evidence under the Material-finding protocol, not a new
independent or native reassessment. The three independent Outcome verdicts above are unchanged.
