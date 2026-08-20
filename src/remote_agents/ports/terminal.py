"""Technology-neutral terminal observations and capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TrustState


class TerminalTargetMissing(RuntimeError):
    """Raised when a managed target no longer exists on the terminal.

    A session killed out from under the service — by an OOM kill, or by a terminal
    crash that took every pane with it — leaves a durable record pointing at a target
    that is simply gone. That is ordinary evidence of an ended session, not a fault, so
    it is a distinct type callers can answer rather than an opaque failure they can only
    propagate. It subclasses RuntimeError so existing handlers keep their behaviour.
    """


#: The `detail` values a terminal adapter may set on an observation that reports no pane.
#:
#: They live on the port because they are the vocabulary of the boundary itself: the adapter
#: is the only thing that can tell these apart, and the application is the only thing that
#: decides what each one means to the owner (`application/session_actions` renders them,
#: `application/services` maps them to lifecycle events). Defined in the application, they
#: made a terminal adapter import the application to say what it had observed — ARCH-02's
#: inward rule inverted for three string constants, and the reason `check_imports` finds
#: nothing to complain about now.
UNKNOWN_SESSION = "unknown_session"
GRACEFUL_TIMEOUT = "graceful_timeout"
OWNERSHIP_LOST = "ownership_lost"


@dataclass(frozen=True, slots=True)
class TerminalObservation:
    session_id: SessionId
    live: bool
    preserved: bool
    detail: str = ""
    project_id: ProjectId | None = None
    profile_id: ProfileId | None = None

    host_session: str | None = None
    """Which terminal session is *showing* this pane, when the terminal can say.

    Provenance, never a lifecycle input. A pane can be hosted by a session that is not its
    own — the console displays an agent by taking its pane — and the point of recording that
    is so a reader can tell "displaced" from "gone" without inferring it. Reconciliation must
    keep deciding on identity alone: if the host changed a verdict, moving a pane would move
    a session's state, which is the coupling pane addressing exists to remove.

    `None` from any terminal that does not track hosting, which keeps the port honest about
    what it can answer rather than inventing a default that reads as fact.
    """


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
    async def trust_state(self, session_id: SessionId) -> TrustState: ...
    async def answer_trust(self, session_id: SessionId) -> TrustState: ...
    async def inspect(self, session_id: SessionId) -> TerminalObservation | None: ...
    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def cleanup(self, session_id: SessionId) -> None: ...
    async def force_stop(self, session_id: SessionId) -> TerminalObservation: ...
