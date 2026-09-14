# Acceptance: reliable limits, a reachable Settings screen, and an F-key console

Date: 2026-09-13 (sub-plan 01 measurements; later sections land with their sub-plans)
Branch: `limits-sources`
Plan: `2026-09-13-reliable-limits-and-fkey-console-master-plan.md` — Sub-plan 01
`…-sub-01-limits-sources-plan.md`, Sub-plan 02 `…-sub-02-settings-and-limits-pane-plan.md`,
Sub-plan 03 `…-sub-03-fkey-console-plan.md`, Sub-plan 04 `…-sub-04-release-plan.md`

**The owner's live console and settings are untouched by everything below.** Every drive here
uses a scratch state directory, a copy of the settings file, or a disposable tmux socket.

---

## Section 1 — The status-line hop's cost (Sub-plan 01, Task 2.5)

The hop runs on every status-line update in every Claude Code session on the machine
(debounced at 300 ms; an in-flight script is cancelled by a newer update), so its start-up
cost is the whole question. Budget: **150 ms** end to end through the console script.

Measured 2026-09-14 on the owner's host (Python 3.14.4), the documented stdin example
(`tests/provider_contract/fixtures/claude/statusline_stdin/documented-example.json`) on stdin,
a scratch `--state-dir`, and `--then 'cat >/dev/null'` standing in for the previous command;
ten runs each, wall time from `/usr/bin/time -f %e`:

| Entry point | Command | n | min | median | max |
|---|---|---|---|---|---|
| `uv run` in front | `printf '%s' "$(cat <fixture>)" \| /usr/bin/time -f %e uv run --no-sync python -m remote_agents statusline --state-dir <scratch> --then 'cat >/dev/null'` | 10 | 40 ms | **50 ms** | 50 ms |
| The console script | `… \| /usr/bin/time -f %e .venv/bin/remote-agents statusline --state-dir <scratch> --then 'cat >/dev/null'` | 10 | 30 ms | **30 ms** | 40 ms |
| The interpreter alone | `… \| /usr/bin/time -f %e .venv/bin/python -m remote_agents statusline --state-dir <scratch> --then 'cat >/dev/null'` | 10 | 30 ms | **30 ms** | 40 ms |
| Baseline, `sh -c 'cat >/dev/null'` with no hop | | 10 | | 0 ms | |
| **The shipped path** — no `--state-dir`, so the state directory is resolved through `ProductionPaths` (imports `remote_agents.production` and `config`), under a scratch `HOME` | `… \| HOME=<scratch> /usr/bin/time -f %e .venv/bin/remote-agents statusline --then 'cat >/dev/null'` | 10 | 30 ms | **40 ms** | 40 ms |

The statusline hop's median through the console script is **30 ms** on the light path and
the shipped-path row above with the state directory resolved the way the installed wrapper
resolves it (the wrapper carries no `--state-dir`; the close-out evaluator caught the first
three rows measuring a branch the artefact does not ship) — both a fraction of the budget.
`-X importtime` on the module (Task 2.1's record): `remote_agents.statusline` itself is about
8 ms cumulative; the largest imports on the path are argparse's colour support and
`dataclasses`/`inspect`, none of them this project's. The written file is 0600 and carries
`rate_limits` as received plus `recorded_at`.

**Which console script.** The plan names "the installed console script". The installed tool
on this host is pinned to `v0.41.0`
(`~/.local/share/uv/tools/remote-agents/uv-receipt.toml`), which has no `statusline`
subcommand, so the console-script leg ran through this checkout's own
`.venv/bin/remote-agents` — the same generated shim shape, importing the working tree. The
release sub-plan re-measures through the installed script after `remote-agents upgrade`.

---
