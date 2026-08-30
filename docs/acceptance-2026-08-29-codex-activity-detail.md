# Acceptance — what a Codex hook payload actually carries

Date: 2026-08-30
Build measured: **`codex-cli 0.151.0`** (see *Build drift* below)
Host: this workstation, Linux
Method: a disposable `CODEX_HOME` whose `hooks.json` dumps hook stdin to a file

This is a **measurement**, taken before anything parses these fields. The reason it exists rather
than a reading of the binary's symbol table is recorded in `activity_spool._DISCRIMINATING_FIELDS`:
`error_type` and `end_reason` were both assumed from a symbol table, both wrong, and the result
was that `limit_reached` could never be produced — silently, for the one thing a phone
notification is most wanted for.

**What is recorded here is the field vocabulary: names, types, and whether a value was present.**
No captured payload content is reproduced (GDEC-SEC-001), and none of it is committed. The
captures live in a scratchpad outside this repository and are deleted with it.

## Boundaries the drill held

- The owner's `~/.codex/hooks.json` was neither read nor written. Its `sha256` was recorded before
  the drill and re-asserted after every run: `3301a3fe…428dc1`, unchanged throughout.
- `~/.codex/config.toml` was not modified (mtime still 2026-08-29).
- `auth.json` was **symlinked**, never copied, opened, logged or serialized — the same approach
  `tests/live/test_codex_activity_hooks.py` documents, preserving the ordinary ChatGPT entitlement
  without requiring separate API billing.
- Hook trust was granted **inside the disposable `CODEX_HOME` only**; Codex persists hook trust
  per home, so the owner's trust state is untouched.
- Every tmux session the drill created was on a `remote-agents-test-*` socket and was destroyed.
  The service's own socket was not addressed.

## `Stop` — measured on 3 payloads

| Field | Type | Observed |
|---|---|---|
| `session_id` | `str` | present, 36 chars (uuid) |
| `turn_id` | `str` | present, 36 chars (uuid) |
| `transcript_path` | `null` | **null in all 3 observed** — contrast `PermissionRequest` below |
| `cwd` | `str` | present — an absolute path |
| `hook_event_name` | `str` | `"Stop"` |
| `model` | `str` | present |
| `permission_mode` | `str` | present |
| `stop_hook_active` | `bool` | present |
| `last_assistant_message` | `str` | **present, and carries the agent's own last line** |

`last_assistant_message` is the field this sub-plan was looking for, and it is the same field name
Claude's `Stop` carries and `_DETAIL_FIELDS` already reads.

**Absent from `Stop`, though the binary's symbol table names them:** `reason`,
`agent_transcript_path`, `agent_type`, `prompt`. A parser keyed on any of those would read
nothing, which is exactly the failure this measurement exists to prevent.

## `PermissionRequest` — measured on 4 payloads

`codex exec` **cannot** produce one. It answers *"This session does not permit approval
escalation"* and auto-rejects, so only `Stop` ever fires. An approval is an interactive act, so
these were taken from a real Codex TUI in a real tmux pane — the path the service manages — with
`approval_policy = "on-request"` and `sandbox_mode = "read-only"` set in the drill home.

| Field | Type | Observed |
|---|---|---|
| `session_id` | `str` | present, 36 chars |
| `turn_id` | `str` | present, 36 chars |
| `transcript_path` | `str` | **present and a real path** — unlike on `Stop`, where it is null |
| `cwd` | `str` | present — an absolute path |
| `hook_event_name` | `str` | `"PermissionRequest"` |
| `model` | `str` | present |
| `permission_mode` | `str` | present |
| `tool_name` | `str` | present — the tool class being asked about. **Observed only as `Bash`, in all 4 samples**, so its value space is unverified beyond that one instance. Low consequence: it carries no command, path or prompt whatever its value. |
| `tool_input` | `dict` | present, keys `command` and `description` |

**Absent from `PermissionRequest`, in all 4 samples:** `last_assistant_message` (the key is not
present at all), `stop_hook_active`, and the same four the `Stop` table records as absent. The two
events do not share a payload shape, so a parser must branch on `hook_event_name` rather than
probing for whichever field it hopes to find.

**There is no field that safely names what is being asked, except `tool_name`.**

- `tool_input.command` carries the **literal command**.
- `tool_input.description` reads as prose but **carries the path too** — the observed value named
  the exact file the command would create. It is not a safe summary; it is the command restated.
- `transcript_path` is a path, and on this event it is populated.

So of the whole payload, `tool_name` is the only field that describes the request without
carrying a command, a path or a prompt.

## The pane-title marker animates

Measured while an approval stood open, sampling the managed pane's title every 0.75s for 30s:

```
"[ ! ] Action Required | <project>"   18-20 samples
"[ . ] Action Required | <project>"   20-22 samples
```

`application/activity._CODEX_ACTION_REQUIRED_TITLE` matches the literal prefix
`"[ ! ] Action Required | "`, so the `[ . ]` frame does not match.

**This is a robustness gap, not a broken watch.** Ten consecutive polls of the real
`CodexApprovalWatcher` against a settled, standing approval all matched `[ ! ]` — the `[ . ]` frame
appears while Codex is still working, and the title rests on `[ ! ]` once the dialog settles. Those
ten polls emitted nothing, which is *correct*: the marker was already present when that watcher
instance started, so the first read is a restart baseline and the edge never rises again
(DEC-063's own rule, pinned by
`test_existing_action_required_title_is_a_restart_baseline_not_a_duplicate`).

The gap is that a poll landing during the animated phase reads "no marker", which can clear the
edge and cause a later duplicate, or — if the approval is answered before the next poll — a missed
one. A predicate matching the invariant part (`"] Action Required | "`) would close it. **Not
changed here:** it is outside this sub-plan's subject and amends DEC-063's predicate, so it is
raised rather than folded in silently.

## Build drift during the run

Preflight recorded `codex-cli 0.150.1` and the plan states that a different build means
re-measuring rather than assuming. During the first interactive attempt Codex **updated itself**
to `0.151.0` (`npm install -g @openai/codex`, 15:13) — its own startup behaviour, triggered by
launching the TUI. The owner chose to keep 0.151.0, so every table above was re-measured against
it.

Three `Stop` payloads taken on 0.150.1 before the update are retained as superseded history. Their
field vocabulary was **identical** to 0.151.0's for `Stop`; no `PermissionRequest` was obtainable
on 0.150.1 because `exec` cannot escalate and the interactive attempt is what triggered the update.

One behaviour difference between the builds was observed directly: `approval_policy = "untrusted"`
is rejected by 0.151.0 with *"no longer supported; remove this setting"*. `--ask-for-approval` now
accepts only `on-request` and `never`.

## What this licenses the next task to parse

- **`Stop` → `last_assistant_message`**, bounded by `bounded_detail_line` exactly as Claude's is.
- **`PermissionRequest` → `tool_name` at most**, and nothing else.
- **Never** `tool_input` (either key), `transcript_path`, `cwd`, `prompt`, or any field not listed
  in the tables above.
