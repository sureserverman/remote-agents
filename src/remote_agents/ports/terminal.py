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


@dataclass(frozen=True, slots=True)
class TrustAnswer:
    """What answering the folder-trust question actually did, as distinct from what the pane shows.

    **Two facts, because the caller needs both and one cannot be derived from the other.**
    `observed` is the pane afterwards. `pressed` is whether this call put a key into it.

    The second is not a pane state and could not be one. `TrustState` has exactly two members
    deliberately — `classify_trust_capture` gives the reason: nothing in a capture separates
    "answered a moment ago" from "never asked", so a third member invented from an absence is
    the failure DEC-009 names. But *did I send a key* is not read off a capture at all; the
    terminal knows it for certain, having either called `send_keys` or not. Returning it is
    reporting a fact, not inferring one.

    **What it was before.** All three refusal paths returned bare `TrustState.UNKNOWN`, and so
    did the success path — because answering clears the dialog, so the capture taken afterwards
    stops matching. The values were identical, so the surface reported *Trusted. The agent can
    continue* for a pane it had declined to touch, having already burned the one-shot token.
    Failing closed was right; saying it worked was not.
    """

    pressed: bool
    observed: TrustState


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
NOT_AWAITING_TRUST = "not_awaiting_trust"
"""A live pane that is no longer sitting on its folder-trust question.

The distinction a decline turns on. A stored record can read `UNTRUSTED` while the pane has
already been answered -- at the keyboard, or from the other surface -- because nothing reports
that back and only a later observation notices. So "the record says untrusted" is not evidence
that the agent is still waiting, and an unconfirmed kill may not act on it.
"""

TERMINAL_NOT_LIVE = "terminal_not_live"
"""A readiness recheck found no live pane for this session at all.

Distinct from a bare "not ready", and the distinction is load-bearing: an agent that is slow
or quiet is one a later pass can still promote, and a pane that is gone is not. Only the
adapter can tell them apart, and until this was named the application could only see that
`live` was False and had to treat both as "come back later" -- which left a session whose
agent quit at its own trust dialog waiting for a promotion that could never arrive.
"""


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

    awaiting_trust: bool = False
    """The agent is stopped on its own folder-trust question right now.

    A *reason*, which is the thing this type could not previously carry. `live` and
    `preserved` describe the pane; they cannot distinguish an agent that is still starting
    from one that has finished starting and is waiting for an answer nobody is at the
    keyboard to give. Without somewhere to say which, a trust-blocked launch could only be
    reported by spending the whole startup budget and then claiming a failure.

    Defaulted to False so every existing construction keeps its exact meaning: an adapter
    that cannot tell says nothing rather than guessing.
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
    async def remote_control_state(self, session_id: SessionId) -> RemoteControlState: ...
    async def trust_state(self, session_id: SessionId) -> TrustState: ...
    async def answer_trust(self, session_id: SessionId) -> TrustAnswer: ...
    async def decline_trust(self, session_id: SessionId) -> TerminalObservation: ...
    async def inspect(self, session_id: SessionId) -> TerminalObservation | None: ...
    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation: ...
    async def cleanup(self, session_id: SessionId) -> None: ...
    async def force_stop(self, session_id: SessionId) -> TerminalObservation: ...
