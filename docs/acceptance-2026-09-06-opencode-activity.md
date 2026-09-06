# OpenCode's plugin events — what they actually carry

**Measured 2026-09-06** against `opencode 1.18.16` on this host, before anything in this
repository was written to parse them.

> **This document was substantially wrong when first written, and was corrected the same day
> after a gate evaluator checked it.** The payload tables were right; the section explaining
> *why a measurement was needed* was not — it asserted that OpenCode's documentation and its
> typed SDK were both wrong about the shape, and neither claim survives. The correction is kept
> visible rather than silently rewritten, because a document whose whole subject is *what a
> confident claim from a symbol table costs* has no business hiding one of its own.

## Why a measurement — the honest version

Not because the published sources are wrong. Because **the sources were misread, and the only
thing that settles a payload is the payload.**

- **The documentation is correct.** `opencode.ai/docs/plugins/` presents `session.idle`,
  `permission.asked` and `permission.replied` as **event types delivered through the generic
  `event` hook** — `event: async ({ event }) => { if (event.type === "session.idle") … }` — not
  as hook keys. A summarised fetch of that page rendered them as hook keys, and this document's
  first draft repeated that reading and blamed the page for it.
- **The typed SDK is correct too, at the version that ran.**
  `@opencode-ai/sdk@1.18.16`'s v2 surface (`dist/v2/gen/types.gen.d.ts:1128`) declares
  `permission.asked` as `{id, sessionID, permission, patterns, metadata, always,
  tool?{messageID, callID}}` — **an exact match for the capture below, including the optional
  `tool`.**
- **What the first draft actually read was a stale copy of a different thing.**
  `@opencode-ai/plugin@1.0.85` and `@opencode-ai/sdk@1.0.85`, vendored into an unrelated
  project's `node_modules` (`ai-tools/engineering-skills/…/skillful-plus/`), whose **v1** type
  surface has no `permission.asked` at all. The `Permission` type it read is
  `permission.updated`'s properties — a **different event**. The table headed "the SDK declares
  / the live event sends" was therefore comparing one event's declared type against another
  event's live payload, from a package the measured binary never loaded. Four `@opencode-ai/sdk`
  copies exist on this host at 1.0.85, 1.14.39, 1.16.2 and 1.18.16; only the last one ran.

So the argument for measuring is not that the vendor is unreliable. It is that **a type
declaration read from the wrong package is indistinguishable, at a glance, from one read from
the right one** — and that `activity_spool._DISCRIMINATING_FIELDS`' comment records this
project losing months to exactly that class of mistake with Claude's `error_type`. The capture
is what tells you which copy you are looking at.

**The correction strengthens the result.** v2's declaration is independent corroboration of a
one-sample observation, which is precisely what a single sample most needs.

## Method, and what it wrote

A throwaway plugin registered on the `event` hook, `permission.ask` and `tool.execute.before`,
loaded through a **disposable `XDG_CONFIG_HOME`**, appending every payload to a capture file.

**Isolation was config-only, and the first draft of this section overstated it.** It said the
owner's OpenCode data was "left alone". Checked afterwards, that is false:

- `~/.local/share/opencode/auth.json` — mtime moved to the first run's second (credentials read,
  not modified in content, but the file was touched).
- `~/.local/share/opencode/opencode.db` (+ `-wal`), and the log directory — written.
- **Both completing runs are live session rows in the owner's real OpenCode database**, titled
  *"Read note.txt for contained word"* and *"Run echo measured command"*.

`XDG_DATA_HOME` was deliberately left at its default so the owner's credentials would work,
which is what made a real model run possible at all — and the consequence, which was not
thought through, is that OpenCode also persisted its session state there. A future drill that
wants true isolation must relocate `XDG_DATA_HOME` too and re-authenticate inside it.

## Runs

**Five runs: two completed, three hung.** (The first draft said three completed and two hung,
which also contradicted its own sample counts — three completing runs would have produced three
`session.idle` samples, not the two recorded.)

| run | outcome | records |
|---|---|---|
| 1 | completed | 92 |
| 2 | hung — `plugin.loaded` only | 1 |
| 3 | completed | 80 |
| 4 | hung — `plugin.loaded` only | 1 |
| 5 | hung — `plugin.loaded` only | 1 |

The three hangs each asked for something needing an *edit* permission or produced large output,
and were killed at the timeout. Recorded because it bears on how a live drill should be driven;
not understood at the time, and not claimed to be.

**Understood on 2026-09-06, at the Stage 5 gate, and it was not about the prompts.** The live
drill (`tests/live/test_opencode_activity_plugin.py`) reproduced the same silence and asked the
log instead of the terminal:

```
level=ERROR message="stream error" providerID=openai modelID=gpt-5.6-terra
  error.error="AI_APICallError: The usage limit has been reached"
```

`opencode run` prints **nothing** when a provider refuses — it retries the stream with backoff
and never exits — so a driver watching stdout sees a silent process and calls it a hang. The
correlation with edit permissions and large output was a coincidence of which runs happened to
land after the quota was spent. The drill now classifies this from
`$XDG_DATA_HOME/opencode/log/opencode.log` and skips with a named reason (DEC-059) rather than
waiting it out.

## What fired

| callback | fired? | samples |
|---|---|---|
| `event` → `session.idle` | yes, once per completing run, at the end | **2** |
| `event` → `permission.asked` | yes, when a tool needed approval | **1** |
| `event` → `permission.replied` | yes, immediately after | **1** |
| `tool.execute.before` | yes | 2 |
| `permission.ask` (the typed hook) | **never** | 0 |

`permission.ask` not firing is an observation, not a conclusion: `opencode run` is
non-interactive and auto-rejects (`permission requested: bash (echo measured);
auto-rejecting`), so the hook may be bypassed on that path rather than absent. **The `event`
stream carried the permission either way**, which is the route this project relies on without
having to settle the question.

### The `event` stream is noisy, and a parser must filter it

**14 distinct event types arrived across two runs**, and this is load-bearing for the plugin
about to be written — a handler on `event` receives all of it:

```
 90  plugin.added            9  session.status         3  message.part.delta
 20  message.part.updated    6  catalog.updated        2  session.created
 14  message.updated         4  reference.updated      2  session.idle
  9  session.updated         4  integration.updated    1  permission.asked
                             3  session.diff           1  permission.replied
```

Four of those appear in no version of the SDK's v1 `Event` union. The first draft of this
document said the other event types were "not pursued", which stated as unobserved a set that
was substantially observed.

## `session.idle` — 2 samples

```json
{
  "id": "evt_synthetic0000000000000000",
  "type": "session.idle",
  "properties": { "sessionID": "ses_synthetic000000000000000" }
}
```

| field | type | present | notes |
|---|---|---|---|
| `id` | `str` | 2 of 2 | the event's own id |
| `type` | `str` | 2 of 2 | `"session.idle"` |
| `properties.sessionID` | `str` | 2 of 2 | **OpenCode's** session id, not this project's |

**There is no agent text on this event, and no field that could carry any.** This is the
load-bearing finding for the provider vertical: an OpenCode `completed` notification cannot
carry a detail from this source, ever — unlike Codex's `Stop`, which had
`last_assistant_message` waiting to be admitted once it was measured.

Getting the agent's last words would mean asking the SDK client for the session's messages — a
**different mechanism** that reads conversation content, and therefore a retention decision
under DEC-013 rather than a parser widening. Not proposed here.

## `permission.asked` — 1 sample

```json
{
  "id": "evt_synthetic0000000000000001",
  "type": "permission.asked",
  "properties": {
    "id": "per_synthetic000000000000000",
    "sessionID": "ses_synthetic000000000000000",
    "permission": "bash",
    "patterns": ["<the literal command>"],
    "metadata": { "command": "<the literal command>" },
    "always": ["<a glob over commands>"],
    "tool": { "messageID": "msg_…", "callID": "call_…" }
  }
}
```

| field | type | present | what it holds |
|---|---|---|---|
| `properties.permission` | `str` | 1 of 1 | **the tool class** — `"bash"`. Names the ask without carrying it. |
| `properties.patterns` | `str[]` | 1 of 1 | **the literal command** |
| `properties.metadata.command` | `str` | 1 of 1 | **the literal command**, again |
| `properties.always` | `str[]` | 1 of 1 | a glob the owner could approve standing |
| `properties.id` | `str` | 1 of 1 | the permission's id |
| `properties.sessionID` | `str` | 1 of 1 | OpenCode's session id |
| `properties.tool.messageID` / `.callID` | `str` | 1 of 1 | conversation identifiers; **`tool` is optional per the v2 type** |

**One sample, one value.** `permission` was observed only as `"bash"`, exactly as Codex's
`tool_name` was observed only as `"Bash"`. Corroborated in kind by the v2 type (`permission:
string`), which confirms the *shape* but says nothing about the value space — any consumer must
be total over unrecognised values.

## `permission.replied` — 1 sample

Recorded because it fired and was captured; **not licensed for anything**, since nothing in
this project needs to know how the owner answered.

```json
{ "sessionID": "ses_…", "requestID": "per_…", "reply": "reject" }
```

## The session id problem, and why it is not one

`properties.sessionID` is **OpenCode's** identifier (`ses_…`), which this project has never
heard of. Every spooled record is keyed on `REMOTE_AGENTS_SESSION_ID`, and `spool_agent_event`
returns 0 without it.

**Measured: the environment variable is visible to plugin code.** A plugin reading
`process.env.REMOTE_AGENTS_SESSION_ID` inside the OpenCode process saw the value the launcher
had exported, and saw no other `REMOTE_AGENTS*` variable. So the plugin keys its records the way
every other provider's hook does, and OpenCode's own session id is never needed or stored.

This was the one finding that could have blocked the vertical outright. It does not.

## Licensing — what a parser may read

Per event, and nothing else:

- **`session.idle` → nothing.** Not even `sessionID`; identity comes from the environment
  variable. The event's *occurrence* is the whole signal. No detail, no ask.
- **`permission.asked` → `properties.permission` at most**, as an **ask class**, never as
  `detail` — the boundary DEC-074 drew for Codex's `tool_name`, for the same reason.
- **`permission.replied` → nothing.**
- **Never** `patterns`, `metadata` (any key), `always`, `tool`, `id`, or any field not listed.
  `patterns` and `metadata.command` are the literal command; `always` is a glob over commands.
- **Every other event type → dropped at the handler**, by name, rather than parsed and
  discarded later.

## Not established here

- **`permission`'s value space.** One sample, one value (`"bash"`). OpenCode's config schema
  suggests a small closed set (`edit`, `bash`, `webfetch`, …) but that was not measured.
- **Field *presence*, as distinct from field names.** From one sample, "present in 1 of 1" is
  weak evidence that a field is always present; the v2 type marks `tool` optional, and others
  may be in practice.
- **Whether the `permission.ask` hook is usable.** It did not fire on the non-interactive path
  that also auto-rejects; it was not tested interactively.
- ~~**Why three runs hung.**~~ **Established 2026-09-06** — a provider at its usage limit, which
  `opencode run` reports only to its log file while retrying silently. See "Runs" above.
- **`tool.execute.before`'s payload**, though it was captured twice and carries a literal
  command and an absolute path. Not pursued because this project maps no activity to it — and
  named here so a future reader knows it was seen and set aside, not missed.
- **The other ~20 event types the v2 SDK declares** and which did not arrive in two short runs.
