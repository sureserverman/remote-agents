"""Set a running thread's permissions on the provider's shared server, after launch.

No permission preset is a launch argument (`domain.profiles.PermissionPreset`): Codex runs a TUI
started with any config override on an embedded app server, off the shared daemon, and so
without Codex Remote Control. A provider whose threads live on a shared server declares this
port in its descriptor (`ProviderDescriptor.thread_permissions`) and applies a preset to a
thread the service already knows is the session's (DEC-128, DEC-138). Nothing here names a
provider (DEC-122).
"""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.profiles import PermissionPreset


class ThreadPermissionsError(Exception):
    """The preset could not be applied. `reason` is short, owner-readable and holds no secret."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ThreadPermissions(Protocol):
    """Apply one preset to one thread, or raise `ThreadPermissionsError` saying why not."""

    async def apply(self, thread_id: str, preset: PermissionPreset) -> None: ...

    async def server_identity(self) -> str | None:
        """Which server instance is running, or None when that cannot be read.

        Opaque, and equal only for the same running instance: a restarted server has forgotten
        what was applied, so a changed identity means every thread is owed its preset again.
        """
        ...
