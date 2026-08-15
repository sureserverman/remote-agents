"""In-memory terminal adapter for harmless end-to-end lifecycle tests."""

from __future__ import annotations

from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TrustState
from remote_agents.ports.terminal import TerminalObservation


class FakeTerminal:
    """Model only the terminal observations the core application is allowed to use."""

    def __init__(self) -> None:
        self._observations: dict[SessionId, TerminalObservation] = {}
        #: Queued answers for `trust_state`, consumed one per call. A list rather than a
        #: single value so a test can drive the sequence a real pane produces -- AWAITING
        #: before the answer, UNKNOWN after it -- which is the only way to assert that a
        #: surface stopped offering the row once the question was gone.
        self.trust_states: list[TrustState] = []
        self.trust_answers = 0

    async def managed_process_roots(self) -> tuple[int, ...]:
        return ()

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Create a live fake session without running a process.

        Ownership is recorded because the real adapter reports it and the application
        checks it: `SessionService.copy_attach` refuses a pane whose project or profile
        disagrees with the record. A fake that dropped these fields modelled a terminal
        whose panes have no owner, and made that refusal unreachable in tests.
        """
        observation = TerminalObservation(
            session_id,
            live=True,
            preserved=False,
            project_id=project_id,
            profile_id=profile_id,
        )
        self._observations[session_id] = observation
        return observation

    async def resume(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        source_id: ProviderConversationId,
    ) -> TerminalObservation:
        """Model a trusted provider selection without accepting Telegram arguments."""
        del source_id
        return await self.launch(session_id, project_id, profile_id)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        """Return the current fake terminal observation, if it remains managed."""
        return self._observations.get(session_id)

    async def copy_attach(self, session_id: SessionId) -> str | None:
        observation = await self.inspect(session_id)
        if observation is None or not (observation.live or observation.preserved):
            return None
        return attach_command(session_id, read_only=not observation.live)

    async def remote_control(
        self, session_id: SessionId, desired_state: RemoteControlState
    ) -> RemoteControlState:
        return (
            desired_state
            if await self.inspect(session_id) is not None
            else RemoteControlState.UNKNOWN
        )

    async def trust_state(self, session_id: SessionId) -> TrustState:
        """Answer from a set the test arms, so a fake never invents a blocked pane.

        Defaults to UNKNOWN: a fake that reported AWAITING by default would offer the trust
        row on every session in every test that never thought about trust.
        """
        del session_id
        return self.trust_states.pop(0) if self.trust_states else TrustState.UNKNOWN

    async def answer_trust(self, session_id: SessionId) -> TrustState:
        del session_id
        self.trust_answers += 1
        return TrustState.UNKNOWN

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """End the fake process while retaining its inspectable session.

        **Ownership is carried through the transition, not dropped at it.** A preserved pane
        keeps its `@remote_agents_*` session options in the real runtime — verified against
        tmux 3.4: a dead pane still answers `parse_pane` with its project and profile — so a
        fake that blanked them modelled a terminal the real one is not. It mattered from the
        moment PRESERVED became attachable (DEC-021): `SessionService.copy_attach` compares
        those two fields against the record, so a preserved observation with neither made the
        new read-only branch unreachable through this fake, and no fake-backed test could
        exercise the capability at all. Found by the Stage 3 gate's adversarial pass.

        This is the same reasoning `launch` records for recording them in the first place.
        """
        del profile_id
        previous = self._observations.get(session_id)
        observation = TerminalObservation(
            session_id,
            live=False,
            preserved=True,
            project_id=previous.project_id if previous is not None else None,
            profile_id=previous.profile_id if previous is not None else None,
        )
        self._observations[session_id] = observation
        return observation

    async def cleanup(self, session_id: SessionId) -> None:
        """Remove a preserved fake session."""
        self._observations.pop(session_id, None)

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        """Remove a live fake session and report its verified termination."""
        self._observations.pop(session_id, None)
        return TerminalObservation(session_id, live=False, preserved=False)
