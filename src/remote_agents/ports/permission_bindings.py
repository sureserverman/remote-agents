"""Read launch snapshots and retain content-free conversation associations."""

from typing import Protocol

from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import PermissionPreset


class PermissionBindings(Protocol):
    async def for_session(self, session_id: SessionId) -> PermissionPreset | None: ...

    async def for_conversation(
        self,
        profile_id: ProfileId,
        source_id: str,
    ) -> PermissionPreset | None: ...

    async def remember(self, session_id: SessionId, source_id: str) -> None: ...
