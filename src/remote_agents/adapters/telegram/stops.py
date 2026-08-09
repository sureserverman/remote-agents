"""Replay-safe confirmation state for the approved destructive session actions."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.application.commands import CleanupCommand, ForceStopCommand, GracefulStopCommand
from remote_agents.application.session_actions import StopFailure, available_actions, stop_failure
from remote_agents.domain.models import ProfileId, SessionId, SessionState


@dataclass(frozen=True, slots=True)
class StopRequest:
    action: str
    session_id: SessionId
    profile_id: ProfileId


@dataclass(frozen=True, slots=True)
class StopResult:
    """Whether the command was dispatched at all, and why it did not take effect if it was.

    Two questions rather than one bool, because they have different answers and the caller
    acts differently on each. `dispatched` false means the recheck refused — a stale token, a
    session that moved on — and nothing reached the terminal. `dispatched` true with a
    `failure` means the command ran and did not work, which is the case BL-008 recorded: the
    bot inferred "it did not exit in time" from the session still being listed, which is right
    for `graceful_timeout` and confidently wrong for `unknown_session`, where nothing was ever
    sent.

    `failure` is the same `StopFailure` the local surface renders, from
    `application.session_actions` — DEC-007 requires the two surfaces to agree about what a
    stop did, and the cheapest way to agree is to be handed the same words.
    """

    dispatched: bool
    failure: StopFailure | None = None

    def __bool__(self) -> bool:
        """Refuse to be a bool at all, because this used to be one and callers remember.

        Not a convenience — a poison pill. `execute` returned a plain bool until BL-008, and a
        dataclass instance is unconditionally truthy, so every `assert await execute(...)` and
        `if not await execute(...)` left behind by the change keeps *running* and stops
        *checking*. Nothing in Python signals that: there is no type checker configured in
        this project, so the only alternative was a repo-wide grep, which is a sweep that has
        to be remembered rather than one that happens.

        Defining it to mirror `dispatched` would have been worse than leaving it undefined. It
        resurrects the exact ambiguity the two fields exist to remove — `if result:` could
        fairly mean "the command ran" or "the command worked", and for a graceful stop those
        are different questions with different answers.

        Raising found three surviving assertions the moment the suite ran, in two files this
        change never touched. Suggested by the Tier-1 review after the author's own grep for
        `if not …execute(` had missed all three, which is the argument for it in one line.
        """
        raise TypeError("StopResult has no truth value; read .dispatched or .failure")


_NOT_DISPATCHED = StopResult(False)


class StopController:
    def __init__(self, callbacks: CallbackStateStore) -> None:
        self._callbacks = callbacks
        self._force_confirmed: set[str] = set()

    def offer(
        self,
        session_id: SessionId,
        profile_id: ProfileId,
        state: SessionState,
        action: str,
        owner_id: int,
        chat_id: int,
        view_revision: int,
    ) -> str | None:
        if action not in available_actions(state):
            return None
        return self._callbacks.create(
            action, f"{session_id}:{profile_id}", owner_id, chat_id, view_revision, mutation=True
        )

    def confirm_force(self, token: str, owner_id: int, chat_id: int, view_revision: int) -> bool:
        state = self._callbacks.resolve(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )
        if state is None or state.action != "force":
            return False
        self._force_confirmed.add(token)
        return True

    def claim(
        self, token: str, owner_id: int, chat_id: int, view_revision: int
    ) -> StopRequest | None:
        state = self._callbacks.resolve(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )
        if state is None or (state.action == "force" and token not in self._force_confirmed):
            return None
        if not self._callbacks.claim_mutation(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        ):
            return None
        session_value, profile_value = state.entity_id.split(":", maxsplit=1)
        return StopRequest(state.action, SessionId.parse(session_value), ProfileId(profile_value))

    async def execute(self, request: StopRequest, service: object, record: object) -> StopResult:
        """Recheck the current record, dispatch one typed command, and report what it did.

        The return type is a `StopResult` rather than a bool because a bare bool could only
        answer "did anything run", and the thing BL-008 is about is the difference between a
        graceful stop that worked and one that did not — a distinction `graceful_stop`'s
        observation has always carried and this method used to discard. `dispatched` is the
        old bool, unchanged in meaning, so every refusal path answers exactly what it did.

        Only the graceful branch can report a failure. `cleanup` returns nothing at all, and
        `force_stop`'s observation describes a kill the service has already recorded as an
        event; neither has two causes that read alike, which is the problem being fixed.
        """

        if record.session_id != request.session_id or record.profile_id != request.profile_id:
            return _NOT_DISPATCHED
        # The record is re-read here because it may have moved on since the token was
        # issued, but the rule it is checked against is the shared one. A private copy is
        # what let an offered action be silently refused at dispatch.
        if request.action not in available_actions(record.state):
            return _NOT_DISPATCHED
        if request.action == "graceful":
            observation = await service.graceful_stop(
                GracefulStopCommand(request.session_id, request.profile_id)
            )
            return StopResult(True, stop_failure(observation))
        if request.action == "cleanup":
            await service.cleanup(CleanupCommand(request.session_id))
            return StopResult(True)
        await service.force_stop(ForceStopCommand(request.session_id))
        return StopResult(True)
