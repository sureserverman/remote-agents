"""Shared choices and use case for managed launch permissions."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionBindings
from remote_agents.ports.permission_setting import PermissionSettingPort
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.turn_markers import ConversationMarks


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


class SessionPermissions:
    """Resolve one launch choice; snapshots never track native in-pane changes."""

    def __init__(
        self,
        settings: PermissionSettings,
        bindings: PermissionBindings,
        sessions: SessionStore,
        *,
        marks: ConversationMarks | None = None,
    ) -> None:
        self.settings = settings
        self.bindings = bindings
        self._sessions = sessions
        self._marks = marks

    async def resolve(
        self,
        profile_id: ProfileId,
        override: PermissionPreset | None,
        inherit: SessionId | None,
        *,
        conversation: str | None = None,
    ) -> PermissionPreset | None:
        supported = self.settings.profiles.get(profile_id)
        if supported is None:
            if override is not None:
                raise ValueError("permission presets are unavailable for this provider")
            return None
        if override is not None:
            preset = PermissionPreset(override)
        elif inherit is not None:
            source = await self._sessions.get(inherit)
            if source is None or source.profile_id != profile_id:
                raise ValueError("permission source is missing or belongs to another provider")
            preset = await self.bindings.for_session(inherit)
            if preset is None:
                preset = await self.settings.read()
        else:
            preset = None
            if conversation is not None:
                await self.remember_marks()
                preset = await self.bindings.for_conversation(profile_id, conversation)
            if preset is None:
                preset = await self.settings.read()
        if preset not in supported:
            raise ValueError("permission preset is unavailable for this provider")
        return preset

    async def remember_marks(self) -> None:
        """Retain ended sessions' associations before temporary marks are forgotten."""
        if self._marks is None:
            return
        for record in await self._sessions.list():
            if record.profile_id not in self.settings.profiles:
                continue
            source = await asyncio.to_thread(self._marks.latest, str(record.session_id))
            if source is not None:
                await self.bindings.remember(record.session_id, source)
