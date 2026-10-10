# Acceptance: Claude's `agent_needs_input` Notification, captured (BL-048)

Date: 2026-10-10 (file named for the plan's 2026-10-09 date). Claude Code 2.1.296, build
`fdb6c17a`, on Linux.

Until this capture, `tests/provider_contract/fixtures/claude/notification_agent_needs_input.json`
was synthetic and unmeasured, with `message` absent. This records one real payload: its field
names and value shapes only. Every value quoted below is synthetic (GDEC-SEC-001).

## What fires it

The 2.1.296 binary emits `notificationType: "agent_needs_input"` from the **background-agents
view** (`claude agents`), not from the blocked agent. When a background session the view lists
changes to the `blocked` band, the view posts a notification. Its text is
`` `${label} needs your input: ${needs}` ``, or without the colon part when `needs` is empty.
The view's own `Notification` hook then fires. A string read of the binary found:
- The emitter fires only on a band *change* the view watches. The first sighting of a session
  is a silent seed, so a session already blocked when the view opens produces nothing.
- A `blocked` session whose `needs` is `send a prompt to start` is skipped.
- `needs` is cut to 120 characters.

**What this means for this service.** The hook's spool keys a record on the managed pane's
session variable, not on the payload. So the notice reaches this service only when
`claude agents` itself runs in a managed pane, and it is recorded as *that* pane's
`needs_answer`, not the blocked background agent's. Background agents with no managed view
open notify nothing here. This was not observed end to end, because the drill stripped the
variable; it follows from the spool's code. BL-130 holds the owner's decision.

## Method

Following `docs/acceptance-2026-09-19-ask-payloads.md`:

1. A disposable git workspace under the session scratchpad. Its
   `.claude/settings.local.json` held one `Notification` hook that dumped raw stdin to a file
   beside it.
2. A scratch tmux server `-L remote-agents-test-capture`, started with
   `REMOTE_AGENTS_SESSION_ID` stripped (the executing session runs in a managed pane, so the
   variable had to be removed, and was checked unset inside the pane).
3. `claude --bg --permission-mode default '<run one shell command>'` from the workspace. The
   folder-trust prompt for the scratch path was answered *yes* in the TUI.
4. `claude agents --cwd <workspace>`, so the view listed only the drill's own sessions.

## Attempts (2 of the 3 allowed)

- **Attempt 1.** The background agent was already `blocked` (waiting on its Bash permission)
  before the view opened. The view showed it under "Needs input" and fired **no**
  `agent_needs_input`, which is the seed rule above, observed. The only dump was the agent's
  own `permission_prompt` `Notification`.
- **Attempt 2.** With the view open, a second background agent was started from another tmux
  window. The view saw it working, then blocked, and its hook fired **one**
  `agent_needs_input`. The agent's own `permission_prompt` fired as well.

Both agents were stopped with `claude stop`. The transient daemon that `--bg` started
(`claude daemon run --origin transient`) and its spare hosts exited once the view, its last
client, was gone (`claude daemon status`: not running).

## The payload

Keys, in the order sent: `session_id`, `transcript_path`, `cwd`, `scratchpad_dir`,
`hook_event_name`, `message`, `notification_type`.

| Field | Shape |
|---|---|
| `session_id` | `str`, a UUID. It is the **agents view's** session, not the blocked agent's: it matched neither agent's own `permission_prompt` dump, and its transcript sat in a different directory from theirs. |
| `transcript_path` | `str`, absolute path ending `.jsonl` (the view's transcript) |
| `cwd` | `str`, absolute path |
| `scratchpad_dir` | `str`, absolute path |
| `hook_event_name` | `"Notification"` |
| `message` | `str`, `"<label> needs your input: <needs>"` |
| `notification_type` | `"agent_needs_input"` |

There is no `title` and no `prompt_id`, unlike `permission_prompt`, which carries `prompt_id`.

**`message` is present.** So the spool's detail reads it, under Claude's existing licence
(`detail_fields` = `message`, `last_assistant_message`), and the old `detail is None` pin was
wrong for this event.

**What `message` carries.**
- `<label>` was the background session's name. Here that was the first words of the prompt
  given to `claude --bg`, cut with `…`, because no name was set.
- `<needs>` was the pending ask, e.g. `approve Bash: <the command>`.

A synthetic example of the shape: `Rename the… needs your input: approve Bash: echo synthetic`.

This is what the existing licence already admits for every Claude `Notification` (DEC-098 admits
the owner's own command in `detail`). No retention changed here. That a label can be the opening
words of a prompt is recorded for the owner, not decided here.

## What stays unexplained

The 19 historical detail-less `agent_needs_input` rows. This capture always carries `message`,
so those rows came from a path not reproduced here.

## Isolation

- `~/.claude/settings.json` was not modified: its sha256 was the same before and after.
- The drill never carried the service's session variable. The production spool
  (`~/.local/state/remote-agents/activity`) gained no record; it holds only its `turns`
  directory, last touched before the drill began.
- One trust entry for the deleted scratch path was written to the owner's Claude config by the
  TUI's own prompt. It was left in place: the path no longer exists, and editing that file while
  Claude runs risks a lost write.
- The raw dumps and the workspace were deleted after this record was written.
