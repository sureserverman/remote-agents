"""Shared choices and use case for managed launch permissions."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_setting import PermissionSettingPort


@dataclass(frozen=True, slots=True)
class PermissionChoice:
    value: PermissionPreset
    label: str
    description: str


PERMISSION_CHOICES = (
    PermissionChoice(
        PermissionPreset.APPROVE_FOR_ME,
        "Approve for me",
        "Workspace write with automatic approval review.",
    ),
    PermissionChoice(
        PermissionPreset.ASK,
        "Ask me",
        "Workspace write with approval requests reviewed by you.",
    ),
    PermissionChoice(
        PermissionPreset.FULL_ACCESS,
        "Full access",
        "Run without sandboxing or approval prompts. Choose deliberately.",
    ),
    PermissionChoice(
        PermissionPreset.PROVIDER_DEFAULT,
        "Use Codex configuration",
        "Add no permission flags; use the provider's configuration.",
    ),
)
PERMISSION_TITLE = "Codex launch permissions"


class PermissionSettings:
    """Read on each use so independent control surfaces observe saved changes."""

    def __init__(
        self,
        setting: PermissionSettingPort,
        *,
        profiles: Mapping[ProfileId, tuple[PermissionPreset, ...]] | None = None,
    ) -> None:
        self._setting = setting
        self.profiles = MappingProxyType(dict(profiles or {}))

    async def read(self) -> PermissionPreset:
        return await self._setting.read()

    async def set(self, value: PermissionPreset | str) -> None:
        try:
            preset = PermissionPreset(value)
        except (TypeError, ValueError):
            raise ValueError("unknown permission preset") from None
        await self._setting.write(preset)
        if await self._setting.read() != preset:
            raise ValueError("could not save permission preset; reopen Settings and retry")
