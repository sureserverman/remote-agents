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
than a set of keys anything writes. It is still the honest place for the list, and
`reported_activity_kinds_for` is derived from the same two events.
"""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.agents.hook_settings import _HookProvider, _PluginEntry
from remote_agents.adapters.agents.opencode.plugin import (
    PLUGIN_MARKER,
    PLUGIN_RELATIVE_PATH,
    plugin_source,
)

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
