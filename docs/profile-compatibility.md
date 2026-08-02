# Profile compatibility and local recovery

Get the current machine-readable view with:

```bash
uv run --locked remote-agents doctor --profiles --json | python -m json.tool
```

The profile record contains no credentials, terminal output, project paths, prompts, or
environment values. `AVAILABLE` means the executable is present; its version is diagnostic
information only, so a local update does not disable Telegram launches. Every launch still has
to reach its agent-specific readiness state. Authentication and workspace trust are never
bypassed; a trust dialog remains a local operator action.

| Profile | Fixed launch argv | Availability/auth/trust | Readiness evidence | Fixed graceful exit |
| --- | --- | --- | --- | --- |
| `claude` | `claude` | executable must be present; local auth/trust stays local | `Claude Code`, rejecting workspace-trust dialog | `/exit`, Enter |
| `claude-remote` | `claude --remote-control ra-<uuid>` | executable must be present; local auth/trust stays local | `Claude Code`, rejecting workspace-trust dialog | `/exit`, Enter |
| `codex` | `codex` | executable must be present; local auth/trust stays local | `/exit` command selection and submit | `/exit`, Enter, Enter |
| `opencode` | `opencode` | executable must be present; local auth/trust stays local | `Ask anything...` interactive UI | Ctrl-C |
| `cursor-agent` | `cursor-agent` | executable must be present; local auth/trust stays local | `/quit` command selection and submit | `/quit`, Enter, Enter |

The profile arguments are defined in the closed catalogue; Telegram does not provide an
executable, path, raw argument, prompt, keystroke, bypass, or auto-approval flag.

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
until the local cause is repaired.
