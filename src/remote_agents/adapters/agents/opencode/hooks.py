"""OpenCode's activity configuration: which file, which entry, which events.

The other two providers name a settings file and a list of hook *event names*, and the shared
machinery writes one command group per event. OpenCode has no such mechanism: a third
party runs code inside OpenCode's process by naming an ES module in the config's top-level
`plugin` array, and that module subscribes to the event stream itself. So this provider's
declaration carries a `_PluginEntry` as well, and the machinery in
`adapters.agents.hook_settings` asks it which shape to write -- shared machinery asked rather
than copied, and the whole vertical behind one descriptor (DEC-070); what an installer owes a
retired event is DEC-051. The first draft of this line cited DEC-067, which is about whether a
provider *field* may be admitted and has nothing to say about settings-file shapes; a gate
evaluator caught it. The decision this file's existence rests on is DEC-076.

`INSTALLED_EVENTS` is therefore documentation of what the generated plugin subscribes to rather
than a set of keys anything writes. It is still the honest place for the list, and the
licence's `reports` claims only what those two events can carry.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from remote_agents.adapters.agents.hook_settings import _HookProvider, _PluginEntry
from remote_agents.adapters.agents.opencode.plugin import (
    PLUGIN_MARKER,
    PLUGIN_RELATIVE_PATH,
    plugin_source,
)
from remote_agents.ports.agent_activity import ActivityKind, ActivitySource
from remote_agents.ports.provider_descriptor import EventLicence, HookLicence

INSTALLED_EVENTS = ("session.idle", "permission.asked")
"""The two `event` types the generated plugin acts on, measured 2026-09-06.

`permission.replied` fired too and is deliberately absent: nothing in this project needs to know
how the owner answered. Every other type the stream carries -- 14 were observed across two short
runs -- is dropped by name inside the plugin.
"""

RETIRED_EVENTS: tuple[str, ...] = ()
"""Declared empty from this provider's first commit rather than added when it first matters.

DEC-051. `_without_our_groups` sweeps what this installer owns *now or ever did*, so an event
dropped from `INSTALLED_EVENTS` with nowhere to go stops being inspected and is stranded on
every host that already installed it. The tuple existing from the start is what makes the first
removal a one-line edit instead of a discovery.

It is also the narrower claim for this provider than for the other two: OpenCode's events are
not keys in the operator's file at all -- one plugin entry covers both -- so retiring an event
here changes only what the generated plugin subscribes to. The declaration is kept anyway,
because the day this provider grows a second entry is not the day to remember the rule.
"""

PROVIDER = _HookProvider(
    "opencode",
    Path(".config/opencode/opencode.json"),
    INSTALLED_EVENTS,
    RETIRED_EVENTS,
    plugin=_PluginEntry(
        key="plugin",
        relative_path=PLUGIN_RELATIVE_PATH,
        marker=PLUGIN_MARKER,
        render=plugin_source,
    ),
)

#: What an OpenCode payload may contribute as DETAIL: **nothing, permanently**.
#:
#: An empty `EventLicence` rather than an absent one, and empty for a reason that will not change
#: with a wider parser. `session.idle` -- the only event that could carry a finished agent's words
#: -- has a payload of exactly one field, and that field is OpenCode's own session id
#: (`docs/acceptance-2026-09-06-opencode-activity.md`, 2 of 2 samples). There is no
#: `last_assistant_message` waiting to be admitted the way Codex's `Stop` had one; getting the
#: agent's last words would mean asking the SDK for the session's messages, which is a different
#: mechanism reading conversation content and therefore a DEC-013 retention decision rather than a
#: parser widening. Not proposed, and this licence is where a future reader is told so.
#:
#: What an OpenCode payload may contribute as an ASK CLASS. `permission.asked` only, and
#: `properties.permission` only -- the one field the measurement licenses, whose licensing
#: section reads "`permission.asked` -> `properties.permission` at most, as an ask class". Never
#: `patterns` or `metadata.command`, which are the literal command, nor `always`, which is a glob
#: over commands. `session.idle` admits none: an agent that has finished is not waiting.
#:
#: Read through `_plain_token` exactly as Codex's `tool_name` is, and for the same measured
#: reason: one sample, one value (`bash`), so the value space is unverified and a token carrying
#: a space, a slash or a quote is not a tool class this project recognises.
#:
#: The narrowing here duplicates the plugin's own, deliberately. That code lives in the operator's
#: configuration directory where a hand-edit is possible, and the spool reads a file a different
#: process wrote; a boundary that trusts what it is handed because something upstream was careful
#: is not a boundary.
HOOK_LICENCE = HookLicence(
    events=MappingProxyType(
        {
            "session.idle": EventLicence(),
            "permission.asked": EventLicence(ask_fields=("permission",)),
        }
    ),
    # Its plugin forwards only these two events, and "finished" is `session.idle`, which ends
    # no marker: a marker started here would never end (DEC-104).
    starts_turn_marker=False,
    source=ActivitySource.HOOK_EXCLUSIVE,
    # The same limit reasoning as Codex's, for the same reason: nothing in this stream tells a
    # rate limit from an ordinary finish, so neither limit kind is claimed.
    reports=frozenset({ActivityKind.COMPLETED, ActivityKind.NEEDS_ANSWER}),
)
"""What OpenCode's activity plugin may record (BL-050); the descriptor's `activity` is this
value. The event names are dotted, which is why a licence lists events as exact names rather
than reading them through the spool's plain-token guard."""
