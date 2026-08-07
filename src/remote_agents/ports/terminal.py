"""Technology-neutral terminal observations and capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState


class TerminalTargetMissing(RuntimeError):
    """Raised when a managed target no longer exists on the terminal.

    A session killed out from under the service — by an OOM kill, or by a terminal
    crash that took every pane with it — leaves a durable record pointing at a target
    that is simply gone. That is ordinary evidence of an ended session, not a fault, so
    it is a distinct type callers can answer rather than an opaque failure they can only
    propagate. It subclasses RuntimeError so existing handlers keep their behaviour.
    """


@dataclass(frozen=True, slots=True)
class TerminalObservation:
    session_id: SessionId
    live: bool
    preserved: bool
    detail: str = ""
    project_id: ProjectId | None = None
    profile_id: ProfileId | None = None


class TerminalPort(Protocol):
    async def managed_process_roots(self) -> tuple[int, ...]: ...
    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def resume(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        source_id: ProviderConversationId,
    ) -> TerminalObservation: ...
    async def copy_attach(self, session_id: SessionId) -> str | None: ...
    async def remote_control(
        self, session_id: SessionId, desired_state: RemoteControlState
    ) -> RemoteControlState: ...
    async def inspect(self, session_id: SessionId) -> TerminalObservation | None: ...
    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def cleanup(self, session_id: SessionId) -> None: ...
    async def force_stop(self, session_id: SessionId) -> TerminalObservation: ...
