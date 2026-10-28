# Stage 2 product demo

Environment: wheel built from this checkout with `uv build --wheel --out-dir
/tmp/stage2-routing-demo/dist`; all product state and configuration were bound to
`/tmp/stage2-routing-demo`. No source, diff, test hook, or test suite was used.

## Outcome 1

> Works: A destination session remains discoverable and resumes in its own folder and preset after ending or interruption.

Verdict: cannot reach by the user's route

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
no resume or conversation-import command; the documented resume route is the
Telegram `/resume` flow. Exercising that flow requires a real configured Telegram
owner route and provider-backed conversation, which are deliberately absent here.
I therefore did not substitute a test, internal entry point, or fabricated session.

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

## Outcome 2

> Invalid or unavailable input → a named refusal with existing sessions preserved.

Verdict: cannot reach by the user's route

Evidence:

```text
$ bwrap ... remote-agents add-project --config /home/user/.config/remote-agents/config.toml --area invalid --name unavailable
area is not an existing development-root directory
exit=1
```

This is a named refusal for invalid input. The same isolated terminal route reported
`No sessions are running`, so there was no existing destination session whose
preservation could be observed. Creating one through the owner-facing route requires
the unavailable provider-backed launch/resume path; I did not manufacture database
state or call an internal API to turn the partial observation into a pass.

## Outcome 3

> Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

Verdict: cannot reach by the user's route

Evidence:

```text
$ timeout 10s bwrap ... remote-agents tui --config /home/user/.config/remote-agents/config.toml
project registry is degraded: registry_unavailable
[terminal surface] No sessions are running.
exit=124
```

With no discoverable project/session and no real configured provider route, an owner
cannot begin a destination launch or resume operation to interrupt. No retry, hold,
or second-launch observation is available from the user's route in this environment.

## Findings

1. **Blocking — stage demo is not established.** All three required destination
   resume/recovery outcomes are unreachable from the public owner route in the
   isolated environment because the required real provider/Telegram conversation
   route is unavailable. The stage's stated unverified premise remains unverified;
   this demo is not a substitute for it.

2. **Resolved during demo — the initial `registry_unavailable` message was an
   unconfigured isolated fixture, not a product defect.** The public `add-project`
   command created an owned registry and registered `registered-project` (exit 0);
   the same terminal dashboard then displayed it as `registered` and did not show
   `project registry is degraded`.

Gate result: **FAIL** (Blocking findings above). The specified pytest gate was not
run: this evaluator was expressly limited to product-as-user demonstration and may
not replace that gate with tests.
