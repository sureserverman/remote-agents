# OpenCode's plugin events — what they actually carry

**Measured 2026-09-06** against `opencode 1.18.16` on this host, before anything in this
repository was written to parse them.

This is a **measurement**. It exists because two other sources were consulted first and both
were wrong in ways that would have produced a parser reading fields that do not exist.

## Why a measurement rather than the documentation or the types

**The documentation** (`opencode.ai/docs/plugins/`) named three hooks: `"session.idle"`,
`"permission.asked"` and `"permission.replied"`.

**The installed typed interface** (`@opencode-ai/plugin@1.0.85`,
`node_modules/@opencode-ai/plugin/dist/index.d.ts`) names a different set entirely —
`event`, `config`, `tool`, `auth`, `chat.message`, `chat.params`, `permission.ask`,
`tool.execute.before`, `tool.execute.after`. There is **no `session.idle` hook**; session idle
arrives through the generic `event` hook.

And the SDK's `Permission` type (`@opencode-ai/sdk`, `dist/gen/types.gen.d.ts:356`) describes a
shape the running binary does not send:

| the SDK type declares | the live event actually carries |
|---|---|
| `type` | `permission` |
| `pattern?: string \| string[]` | `patterns: string[]` |
| `title: string` | **absent** |
| `time: { created }` | **absent** |
| `messageID`, `callID` at top level | nested under `tool: { messageID, callID }` |
| — | `metadata: { command }`, `always: string[]` |

A parser written from the types would have read `title` and `type` and found neither. This is
the same failure `activity_spool._DISCRIMINATING_FIELDS`' comment records for Claude —
`error_type` and `end_reason` taken from a symbol table, both wrong, `limit_reached`
unreachable in silence for months — reached through a different door.

## Method

A throwaway plugin registered on the `event` hook, `permission.ask` and
`tool.execute.before`, loaded through a **disposable `XDG_CONFIG_HOME`** so the owner's own
`~/.config/opencode/` was neither read as configuration nor modified. Authentication lives
under `XDG_DATA_HOME` and was left alone, which is what let a real model run happen at all.
Each callback appended its whole payload to a capture file.

Five runs; three completed and two hung. The hangs are recorded below rather than omitted.

## What fired

| callback | fired? | samples |
|---|---|---|
| `event` → `session.idle` | yes, exactly once per run, at the end | **2** |
| `event` → `permission.asked` | yes, when a tool needed approval | **1** |
| `event` → `permission.replied` | yes, immediately after | 1 |
| `permission.ask` (the typed hook) | **never** | 0 |
| `tool.execute.before` | yes | 2 |

`permission.ask` not firing is recorded as an observation, not a conclusion: `opencode run` is
non-interactive and auto-rejects (`permission requested: bash (echo measured);
auto-rejecting`), so it is possible the hook is bypassed on that path rather than absent. **The
`event` stream carried the permission either way**, which is the route this project can rely on
without settling the question.

## `session.idle` — 2 samples

```json
{
  "id": "evt_077e88f9d001Y9FCDDGF4Rcjg0",
  "type": "session.idle",
  "properties": { "sessionID": "ses_f88178fefffepRTQfx1rPZaakQ" }
}
```

| field | type | present | notes |
|---|---|---|---|
| `id` | `str` | 2 of 2 | the event's own id, not a session's |
| `type` | `str` | 2 of 2 | `"session.idle"` |
| `properties.sessionID` | `str` | 2 of 2 | **OpenCode's** session id, not this project's |

**There is no agent text on this event, and no field that could carry any.** Not a truncated
message, not a summary, nothing. This is the load-bearing finding for the provider vertical:
an OpenCode `completed` notification cannot carry a detail from this source, ever — unlike
Codex's `Stop`, which had `last_assistant_message` waiting to be admitted once it was measured.

Getting the agent's last words would mean asking the SDK client for the session's messages —
a **different mechanism** that reads conversation content, and therefore a retention decision
under DEC-013 rather than a parser widening. It is not proposed here.

## `permission.asked` — 1 sample

```json
{
  "id": "evt_077ec8868002drRikTqG0ZVBDa",
  "type": "permission.asked",
  "properties": {
    "id": "per_077ec8868001kKHImdAyFhgwUG",
    "sessionID": "ses_f88138530ffed6WJgzlefG9Ahs",
    "permission": "bash",
    "patterns": ["echo measured"],
    "metadata": { "command": "echo measured" },
    "always": ["echo *"],
    "tool": { "messageID": "msg_...", "callID": "call_..." }
  }
}
```

| field | type | present | what it holds |
|---|---|---|---|
| `properties.permission` | `str` | 1 of 1 | **the tool class** — `"bash"`. Names the ask without carrying it. |
| `properties.patterns` | `str[]` | 1 of 1 | **the literal command** — `["echo measured"]` |
| `properties.metadata.command` | `str` | 1 of 1 | **the literal command**, again |
| `properties.always` | `str[]` | 1 of 1 | a command glob the owner could approve standing — `["echo *"]` |
| `properties.id` | `str` | 1 of 1 | the permission's id |
| `properties.sessionID` | `str` | 1 of 1 | OpenCode's session id |
| `properties.tool.messageID` / `.callID` | `str` | 1 of 1 | conversation identifiers |

**One sample, and one value.** `permission` was observed only as `"bash"`, exactly as Codex's
`tool_name` was observed only as `"Bash"`. Its value space is **unverified beyond that
instance**, and any consumer must be total over unrecognised values rather than assume the set.

## The session id problem, and why it is not one

`properties.sessionID` is **OpenCode's** identifier (`ses_…`), which this project has never
heard of. Every spooled record is keyed on `REMOTE_AGENTS_SESSION_ID`, and
`spool_agent_event` returns 0 without it.

**Measured: the environment variable is visible to plugin code.** A plugin reading
`process.env.REMOTE_AGENTS_SESSION_ID` inside the OpenCode process saw
`0191f2c2-0000-7000-8000-0000measure01`, the value the launcher had exported, and saw no other
`REMOTE_AGENTS*` variable. So the plugin keys its records the same way every other provider's
hook does, and OpenCode's own session id is never needed or stored.

This was the one finding that could have blocked the vertical outright. It does not.

## Licensing — what a parser may read

Per event, and nothing else:

- **`session.idle` → `properties.sessionID` is not even needed** (the env var supplies identity).
  The event's *occurrence* is the whole signal. **No detail. No ask.**
- **`permission.asked` → `properties.permission` at most**, as an **ask class**, never as
  `detail` — the same boundary DEC-074 drew for Codex's `tool_name`, and for the same reason.
- **Never** `patterns`, `metadata` (any key), `always`, `tool`, or any field not listed above.
  `patterns` and `metadata.command` are the literal command; `always` is a glob over commands.
  All three would put a shell command into an unprompted phone notification.

## Not established here

- **`permission`'s value space.** One sample, one value (`"bash"`). Whether `edit`, `read`,
  `webfetch` or others appear, and how they are spelled, is unknown.
- **Whether the `permission.ask` hook is usable.** It did not fire on the non-interactive path
  that also auto-rejects; it was not tested on an interactive one.
- **Why two runs hung.** Runs asking for an *edit* permission (`"edit": "ask"` configured)
  produced only `plugin.loaded` and were killed at the timeout, where bash-permission runs
  completed with an auto-reject. Recorded because it bears on how a live drill should be
  driven, not because it is understood.
- **Anything about `session.error`, `session.compacted` or the other ~30 event types** the SDK
  declares. Only the two this project would map were pursued.
