"""Claude's hook configuration: which file, which events — the machinery lives in
`adapters.agents.hook_settings` and asks this value (DEC-063)."""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.agents.hook_settings import _HookProvider

INSTALLED_EVENTS = ("Stop", "StopFailure", "Notification")

RETIRED_EVENTS = ("SessionEnd",)
"""Events this installer used to own and must still clean up after.

**Without this, dropping an event strands it for ever.** `_without_our_groups` opens by
skipping any event it does not own, so an event removed from `INSTALLED_EVENTS` stops being
inspected at all -- and our group under it is copied across untouched by *both* the install
path and the uninstall path, which share the predicate. The hook would go on firing on every
host with none of this project's tooling able to remove it, and it could not be worked around
by uninstalling first, because that would mean running the *old* uninstall before taking the
upgrade.

So the sweep is over what we own **now or ever did**, and an event leaves `INSTALLED_EVENTS`
by moving here rather than by disappearing. An entry stays until every host has run the
installer at least once since the event was dropped; there is no way for this process to know
when that is, and the cost of keeping one is a dictionary lookup per install.

`SessionEnd` was dropped 2026-08-23 (DEC-051): its record was spooled, read, deleted and then
discarded at the mapping, because there is no `ActivityKind` for it -- `ended` was retired for
reporting an exit the owner had just caused. It wrote a file per session end, in every Claude
session on the machine, that nothing ever consumed.
"""


#: Flagless: claude's hook commands predate `--provider` and a flagged reinstall would add a
#: second entry beside every existing one instead of replacing it.
PROVIDER = _HookProvider(
    "claude", Path(".claude/settings.json"), INSTALLED_EVENTS, RETIRED_EVENTS, flagless=True
)
