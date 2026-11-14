"""Read launch snapshots and track what the daemon confirmed."""

from dataclasses import dataclass
from typing import Protocol

from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import PermissionPreset


@dataclass(frozen=True, slots=True)
class PermissionState:
    """What a session asked for and what its daemon last confirmed."""

    requested: PermissionPreset
    applied: PermissionPreset | None
    """The preset the daemon last confirmed. Cleared by a failure: an apply that timed out
    may still have landed, so what the server holds is then unknown and is applied again."""
    daemon: str | None
    """The identity of the daemon that confirmed `applied`."""
    failure: str | None
    """Why the last apply failed; cleared by a success or a new request."""


class PermissionBindings(Protocol):
    async def for_session(self, session_id: SessionId) -> PermissionPreset | None: ...

    async def requested(self, session_id: SessionId) -> PermissionPreset | None: ...

    async def state(self, session_id: SessionId) -> PermissionState | None:
        """None for a session launched before presets were applied on the daemon."""
        ...

    async def mark_applied(
        self, session_id: SessionId, preset: PermissionPreset, daemon: str
    ) -> bool:
        """Record that the server confirmed `preset`. False, and nothing written, when the
        session asks for another preset by now: a late confirmation of an abandoned request
        must not read as the confirmation of the current one."""
        ...

    async def mark_failed(
        self,
        session_id: SessionId,
        reason: str,
        *,
        requested: PermissionPreset,
        daemon: str | None = None,
    ) -> bool:
        """Record why applying `requested` failed, and under which server (`daemon`, None when
        unknown). False, and nothing written, when the session asks for another preset by now:
        that failure is not about what it asks for."""
        ...

    async def set_requested(self, session_id: SessionId, preset: PermissionPreset) -> None:
        """Raises `LookupError` for a session with no state: its request cannot be kept."""
        ...

    async def restore_requested(
        self, session_id: SessionId, preset: PermissionPreset, *, replacing: PermissionPreset
    ) -> bool:
        """Ask for `preset` again after a change to `replacing` did not take, and forget what
        the server confirmed, so the earlier preset is applied afresh. False, and nothing
        written, when the session asks for something other than `replacing` by now."""
        ...
