# Stage 1 demo — 2026-10-26-session-defaults-and-project-rollover-sub-03-routing-and-recovery-plan

Environment: (see below)

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: A ready handoff starts at most one successor under the correct destination project and folder.
Verdict: cannot reach by the user's route
Evidence:
Commands run from `/tmp/remote-agents-project-routing`:

```
.venv/bin/remote-agents --help
.venv/bin/remote-agents tui --help
HOME=/tmp/remote-agents-stage1-demo .venv/bin/remote-agents tui --config /tmp/remote-agents-stage1-demo/config-user-route.toml
```

The shipped CLI exposes `tui` as its terminal dashboard and accepts only `--config`.
The dashboard command remained running as an interactive terminal application (no screen text was
captured by the non-console runner); sending `Escape`, `q`, then Ctrl-C ended it with exit 1.
There is no user-route CLI argument to submit a ready handoff or select its destination. The
documented route requires a real managed Claude or Codex session to write a handoff and the
service to receive it. Creating that provider session / binding is outside this local synthetic
fixture boundary, so I could not make or observe a successor, its project, or its folder without
substituting a test or internal entry point.

PTY follow-up (isolated `/tmp/ra-stage1-fast.sock`, `HOME=/tmp/remote-agents-stage1-demo`, and a
disposable empty `dev_root`) reached the dashboard. Its visible screen said `No sessions are
running.`, `Projects 0 · recent first`, and `No project matches that filter.` The footer offered
only `f1 help`, `f7 add project`, `f10 quit`, and `^p palette`; the selected-view instructions
were `enter open · d detail · / filter · o order`. I sent the documented `l` rollover key to
that dashboard and captured the same empty-project/session view: it did not offer a handoff,
launch, or refusal. This concrete user surface has no selected managed session on which the
rollover action can be offered; proceeding would require creating a provider-managed session,
which is reserved for the final native/provider gate.


## Outcome 2
> Invalid or unavailable input → a named refusal with existing sessions preserved.
Verdict: cannot reach by the user's route
Evidence:
I first started the supplied isolated dashboard configuration with:

```
HOME=/tmp/remote-agents-stage1-demo .venv/bin/remote-agents tui --config /tmp/remote-agents-stage1-demo/config.toml
```

It exited 1 and printed the named configuration refusal:

```
production database path must be /tmp/remote-agents-stage1-demo/.local/state/remote-agents/sessions.sqlite3; refusing to write outside the private state directory
```

That is a refusal for an unusable dashboard configuration, not for invalid or unavailable
handoff input. With the matching disposable profile path, the dashboard starts but exposes no
shell route for submitting an invalid handoff. Reaching the specified input requires the
provider-managed writer/receiver route, which is reserved for the later native/provider gate.
I therefore could not observe the claimed preservation of existing sessions by the user's route.

PTY follow-up showed the concrete empty-state boundary: `Sessions 0` and `No sessions are
running.` The only creation action offered was `f7 add project`; it was not used because a
project alone cannot provide the required invalid handoff input or an existing managed session.
No dashboard control was offered for submitting malformed/unavailable handoff input. Thus this
is not evidence that invalid handoffs preserve rows; it is evidence that no existing row or
handoff route is present in the authorised local fixture.


## Outcome 3
> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.
Verdict: cannot reach by the user's route
Evidence:
The same dashboard launch above was interrupted before any provider handoff existed. Ctrl-C gave
exit 1 and did not provide a rollover result, retry control, hold, or successor record to inspect.
The isolated state file was created at
`/tmp/remote-agents-stage1-demo/.local/state/remote-agents/sessions.sqlite3`, but this is only
dashboard startup state; it is not evidence of an interrupted rollover. The user-facing route
offers no command to initiate and interrupt the handoff operation locally. Causing one would
require a real provider/session handoff or an internal/test hook, both prohibited for this demo.

PTY follow-up also established the interaction boundary. Sending Ctrl-C did not abort a rollover;
it opened the visible dashboard confirmation `Do you want to quit? Press ctrl+q to quit the app`.
After Ctrl-Q the isolated tmux socket reported `no server running`. Because the only operation
that could be interrupted was dashboard exit (with an explicit confirmation), not a handoff,
there was still no user-route rollover operation from which a retry, hold, or second successor
could be observed.

## Findings

- Blocking — all three required product outcomes are unreachable through the shipped local user
  route within the authorised isolated-fixture boundary. The only visible refusal is for the
  dashboard configuration, rather than invalid handoff input; no ready handoff, receiver
  acceptance, successor, retry, or hold can be observed without the deferred provider route.


## Not demoed

owner, 2026-10-28: "1"

The exact reply above selected option 1 from the immediately preceding decision request.
The next quotation is the selected option's text, not a transcription of the reply.

owner selected option 1: "Defer it to the final combined gate and continue source work."

Disposition: defer this independent Stage 1 user-route demonstration to the final combined
gate and continue source work (canonical OD-1). All three original Outcome quotations,
cannot-reach verdicts, initial CLI evidence and actual PTY follow-up are preserved above.
None is relabeled holds. The same three Outcomes are now an explicit final Stage 3 gate
obligation, alongside the separately deferred Telegram and native-provider checks.
