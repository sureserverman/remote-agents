"""The one door from the activity hook to a vertical's hook licence (BL-050).

The hook runs inside every agent turn on the machine, so it must not pay for what it does not
use. `registry` imports all four verticals at its top level -- 279 modules measured on
2026-10-10 against a budget of 300 -- and constructing a descriptor would also build usage
readers. So this module imports exactly one vertical's `hooks` module, the one the hook was
installed for, and reads the `HOOK_LICENCE` value that vertical's descriptor also points at.

It is the second module allowed to import a vertical (`IMPORT_ALLOWLIST`), which DEC-070's
accepted cost 3 calls a deliberate, recorded act: the registry stays the composer of the
closed table, and this is the one lookup the hook can afford.
"""

from __future__ import annotations

from importlib import import_module
from types import MappingProxyType

from remote_agents.ports.provider_descriptor import HookLicence

#: Hook name -> the vertical module that declares its licence. Keyed by hook name, not profile
#: id: Cursor's profile is `cursor-agent` and its hook is `cursor`. Spelled as module paths so
#: no vertical is imported until its own hook fires.
_LICENSING_MODULES = MappingProxyType(
    {
        "claude": "remote_agents.adapters.agents.claude.hooks",
        "codex": "remote_agents.adapters.agents.codex.hooks",
        "opencode": "remote_agents.adapters.agents.opencode.hooks",
        "cursor": "remote_agents.adapters.agents.cursor.hooks",
    }
)


def hook_licence(hook_name: str) -> HookLicence | None:
    """The licence the named hook's vertical declares, or None for any other name."""
    module = _LICENSING_MODULES.get(hook_name)
    return None if module is None else import_module(module).HOOK_LICENCE
