# Acceptance 2026-10-08 — cursor-agent's user-level `stop` hook, measured on the real CLI

Task 2.3 of `plans/2026-10-08-decision-round-closures-plan.md`, measured on 2026-10-09. It is
the prerequisite of BL-106: remote-agents installs its hooks once per user, so Cursor's finished
event is only usable if `cursor-agent` reads the **user-level** `~/.cursor/hooks.json` and the
hook inherits the pane's environment. Cursor's docs (https://cursor.com/docs/agent/hooks, read
2026-10-08) list the user-level file but do not say whether the CLI reads it, nor whether a hook
inherits the parent's environment. `docs/acceptance-2026-09-22-composer-states.md` measured only
the project-level file.

## Result

| Fact | Measured |
|---|---|
| cursor-agent version | `2026.10.01-e373342` (the payload's `cursor_version` agrees) |
| The user-level `~/.cursor/hooks.json` `stop` hook fired | **Yes**, once, when the turn finished |
| The project-level `.cursor/hooks.json` `stop` hook fired (control) | **Yes**, once, for the same turn (same `generation_id`) |
| `REMOTE_AGENTS_SESSION_ID` reached the hook | **Yes**: both hooks read `measure-2026-10-09` |
| Payload keys | identical for both levels; listed below |

Both facts the plan's stop condition names hold, so BL-106 proceeds with one user-level install.

## How it was measured

- Account usage read first through `CursorUsageApiReader`: month 26.3% (Cursor pool 28.6%,
  other 2.9%), so one turn was affordable. The turn cost 17,720 input and 18 output tokens.
- A scratch project in the session scratchpad, with a project-level `.cursor/hooks.json`:
  `{"version": 1, "hooks": {"stop": [{"command": "<scratch>/hook-project.sh"}]}}`.
- The owner's existing `~/.cursor/hooks.json` (422 bytes, sha256 `0ebb667c…a83b05`; a `stop` and
  a `beforeShellExecution` entry from the planning plugin's Cursor port) was copied aside with
  `cp -p`. For the one turn it was replaced by the same shape naming `<scratch>/hook-user.sh`.
- Each hook script wrote its stdin, the **names** of its environment variables (never their
  values) and the value of `REMOTE_AGENTS_SESSION_ID`, then printed `{}`.
- `tmux -L ra-measure-1009 new-session -d -c <scratch>/proj "env REMOTE_AGENTS_SESSION_ID=measure-2026-10-09 cursor-agent"`.
  The workspace-trust dialog was answered `a` (the scratch folder). The prompt typed was
  `Reply with the single word OK and do nothing else.`; the agent answered `OK`.
- Afterwards: `tmux -L ra-measure-1009 kill-server`; the backup was copied back with `cp -p`;
  `cmp` against the backup reported no difference and the sha256 matched the one taken before;
  `tmux -L ra-measure-1009 ls` reported `no server running`.

## The captured payload

The user-level and project-level payloads were byte-identical. `user_email`'s value is redacted;
the scratch path is shortened to `<scratch>`.

```json
{
 "cache_read_tokens": 0,
 "cache_write_tokens": 0,
 "conversation_id": "c2c8d296-52d3-431b-9e33-aa8ccddbc551",
 "cursor_version": "2026.10.01-e373342",
 "generation_id": "9589d451-26d8-47e3-aa91-4a6a86f94d3c",
 "hook_event_name": "stop",
 "input_tokens": 17720,
 "loop_count": 0,
 "model": "gpt-5.2",
 "output_tokens": 18,
 "session_id": "c2c8d296-52d3-431b-9e33-aa8ccddbc551",
 "status": "completed",
 "transcript_path": "/home/user/.cursor/projects/<scratch-slug>/agent-transcripts/c2c8d296-52d3-431b-9e33-aa8ccddbc551/c2c8d296-52d3-431b-9e33-aa8ccddbc551.jsonl",
 "user_email": "<redacted>",
 "workspace_roots": [
  "<scratch>/proj"
 ]
}
```

Beyond the docs' list, the CLI also sends `session_id` (equal to `conversation_id`) and four token
counts. It does not send `cwd`.

## The hook's environment

55 variables, the same names at both levels. Besides the pane's own (`TMUX`, `TMUX_PANE`,
`REMOTE_AGENTS_SESSION_ID`, …), cursor-agent adds `CURSOR_INVOKED_AS`, `CURSOR_PLUGIN_ROOT`,
`CURSOR_PROJECT_DIR`, `CURSOR_RIPGREP_PATH`, `CURSOR_TRANSCRIPT_PATH`, `CURSOR_USER_EMAIL` and
`CURSOR_VERSION`.

## What this means for BL-106

- **One user-level install is enough.** `install-agent-hooks --provider cursor` writes
  `~/.cursor/hooks.json`; no per-project file is needed. It must merge with the owner's existing
  `stop` and `beforeShellExecution` entries, since all matching hooks run.
- **The session id arrives through the environment**, as it does for the other providers, so the
  hook can name the managed session without reading the payload's `conversation_id`.
- **`user_email` arrives twice** — in the payload and as `CURSOR_USER_EMAIL` — and neither may be
  spooled (DEC-013, DEC-037). The spool keeps only the fields it admits, so a Cursor branch that
  admits only `stop` with `status == "completed"` keeps both off disk.
