# Profile compatibility and local recovery

Generated host snapshot, qualified 2026-07-30. Regenerate the machine-readable view with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The profile record contains no credentials, terminal output, project paths, prompts, or
environment values. `QUALIFIED` means the executable version exactly matches a recorded
local interactive lifecycle check. Any executable upgrade returns that profile to `BLOCKED`
until its no-prompt qualification is rerun. Authentication and workspace trust are never
bypassed; the check reports a block rather than accepting a dialog.

| Profile | Fixed launch argv | Version | Availability/auth/trust | Readiness evidence | Fixed graceful exit |
| --- | --- | --- | --- | --- | --- |
| `claude` | `claude` | `2.1.220 (Claude Code)` | `QUALIFIED` | `Claude Code`, rejecting workspace-trust dialog | `/exit`, Enter |
| `claude-remote` | `claude --remote-control ra-<uuid>` | `2.1.220 (Claude Code)` | `QUALIFIED` | `Claude Code`, rejecting workspace-trust dialog | `/exit`, Enter |
| `codex` | `codex` | `codex-cli 0.146.0` | `QUALIFIED` | `Codex` interactive UI | `/exit`, Enter |
| `opencode` | `opencode` | `1.18.10` | `QUALIFIED` | `Ask anything...` interactive UI | Ctrl-C |
| `cursor-agent` | `cursor-agent` | `2026.07.23-e383d2b` | `QUALIFIED` | `Cursor` interactive UI | `/quit`, Enter |

The profile arguments are defined in the closed catalogue; Telegram does not provide an
executable, path, raw argument, prompt, keystroke, bypass, or auto-approval flag. Live
qualification uses a generated dedicated tmux socket and sends no initial task prompt.

## Local recovery

Use the dedicated managed socket only. Never run the equivalent command without
`-L remote-agents`, and replace the placeholder with the complete generated session ID.

```bash
# Read-only inventory of managed sessions.
tmux -L remote-agents list-panes -a -F '#{session_name} #{pane_dead} #{@remote_agents_id}'

# Inspect one exact managed pane; its UUID must match the stored session record.
tmux -L remote-agents capture-pane -p -t ra-<uuid>:

# Remove one exact session only after verifying its ownership metadata and preserved output.
tmux -L remote-agents kill-session -t ra-<uuid>:
```

If SQLite is unavailable or a profile reports `BLOCKED`, do not issue a mutation from the
service. Preserve the database files and use only the read-only inventory/capture commands
until the local cause is repaired and the profile is requalified.
