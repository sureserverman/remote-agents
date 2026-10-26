# Stage 1 premises — 2026-10-26-session-defaults-and-project-rollover-sub-01-codex-permissions-plan

Goal: Every managed Codex start receives one deterministic, inspectable launch preset.
Risk: HIGH — permission boundaries and durable continuation metadata.

Outcomes:
- Works: Every managed Codex start receives one deterministic, inspectable launch preset.
- Invalid or unavailable input → a named refusal with existing sessions preserved.
- Interrupted operation → durable state permits safe retry or a visible hold, never an unrecorded second launch.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: The installed Codex exposes the requested fresh and resume launch policy flags.
Checked: `codex --version`; `codex --help`; `codex resume --help`.
Result: codex-cli 0.164.0 advertises `--approve-for-me`, sandbox, approval and explicit bypass flags for both entry points.
Verdict: holds for CLI syntax. Native effective-policy qualification is reserved for the final combined gate.

## Premise 2
Assumption: Locked dependencies and the current settings, launch and architecture baseline work in an isolated checkout.
Checked: `uv run --locked pytest tests/unit/test_config.py tests/unit/adapters/tmux/test_profiles.py tests/contract/adapters/tmux/test_resume_profiles.py tests/contract/test_profiles.py tests/contract/adapters/tmux/test_launch.py tests/architecture -q`.
Result: 520 passed in 39.82s on Python 3.14.4; uv 0.11.9, tmux 3.4. Disposable storage only.
Verdict: holds.
