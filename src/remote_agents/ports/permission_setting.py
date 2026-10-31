"""Persist the permission default; invalid reads and failed writes raise explicitly."""

from typing import Protocol

from remote_agents.domain.profiles import PermissionPreset


class PermissionSettingPort(Protocol):
    async def read(self) -> PermissionPreset: ...

    async def write(self, value: PermissionPreset) -> None: ...
