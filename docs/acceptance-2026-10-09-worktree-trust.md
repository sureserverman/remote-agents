# Acceptance: does a session worktree inherit folder trust?

**Date:** 2026-10-09
**Host:** the owner's Linux workstation (the one `serve` runs on)
**Agents measured:** codex-cli 0.162.0; Claude Code 2.1.295 (`--permission-mode default`);
cursor-agent 2026.10.01-e373342
**Plan:** `2026-10-09-session-worktrees-design.md` § Measurement, carried into
`2026-10-09-decisions-and-worktrees-sub-04-session-worktrees-plan.md` (Task 5.3)
**Honors:** DEC-016 — remote-agents never writes trust on the agent's behalf, so what each agent
does on its own in a fresh worktree is what the owner will see.

## Why this document exists

A session worktree is a new folder. Whether each agent then raises its folder-trust dialog
decides whether the owner must answer a question per worktree, and on which agents. The design
needed the answer before it chose to offer worktrees at all, and nothing in the agents'
documentation states it, so it was measured.

## Method

Each agent was started in a scratch tmux server (`tmux -L ra-measure`) in three folders, its
screen read 10–12 seconds after start, and the pane killed without answering anything:

1. the main repository, already trusted by that agent;
2. a git worktree of that repository, created under the scratchpad;
3. a brand-new git repository — the positive control, which must raise the dialog.

No answer was given, so no trust entry could have been written. Afterwards
`~/.codex/config.toml`, `~/.claude.json` and `~/.cursor/projects/` were checked and held no
entry for any measured folder, and both worktrees were removed with `git worktree remove`.

## Result

| Agent | Trusted main repo | Worktree of it | New git repo (control) |
|---|---|---|---|
| codex-cli 0.162.0 | no dialog | **no dialog — inherits** | `Folder access` / `Trust this folder?` |
| Claude Code 2.1.295, `--permission-mode default` | no dialog | **no dialog — inherits** | `Accessing workspace:` / `Quick safety check` |
| cursor-agent 2026.10.01-e373342 (repository: translator) | no dialog | **`Workspace Trust Required` — asks** | asks |

Codex and Claude carry the main repository's trust into its worktree; Cursor asks again for each
worktree, so a Cursor worktree session starts `untrusted` and the owner answers once.

## Side finding

**codex-cli 0.162.0 raises no trust dialog outside a git repository.** A fresh folder under
`/tmp` and one under `~/dev` both opened straight to the composer. The 2026-09-28 drill raised it
in "a never-asked directory"; a drill that needs Codex's trust dialog must now use a fresh git
repository instead.

## What follows from it

- remote-agents pre-writes no trust for a worktree (DEC-016): two agents need none, and the
  third asks exactly as it would for any new folder.
- The live check `tests/live/test_worktree_launch.py` re-measures the two that matter on each
  run it is selected for: a Codex worktree launch must settle with no dialog, and a Cursor one
  must show *Workspace Trust Required*.
