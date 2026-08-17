"""Replay-safe callback state for the approved session stop actions."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.application.commands import CleanupCommand, ForceStopCommand, GracefulStopCommand
from remote_agents.application.session_actions import (
    FORCE,
    StopFailure,
    available_actions,
    force_stop_failure,
    stop_failure,
)
from remote_agents.domain.models import OrphanProvenance, ProfileId, SessionId, SessionState
from remote_agents.ports.callback_state import CallbackStatePort

CONFIRMED_FORCE = "force.confirmed"
"""The action a force stop carries once its confirmation screen has been read.

Adapter-internal, and deliberately not a member of `available_actions`: the application layer
decides whether a force is permitted, this name only records which of the two presses this is.
"""


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
    """Mint and claim the tokens behind the approved stop actions.

    **Every token is minted unbound.** A keyboard is built before the message that will carry
    it exists — and, when a screen is redrawn in place, before the render that replaces the
    previous keyboard has pruned it. Minting a stop token already bound to the message being
    edited put it in exactly the set that render was about to discard, so the stop buttons
    were destroyed by the same pass that drew them and answered every press with a redraw.
    Found by review, reproduced end-to-end, and the reason `offer` takes no message id at all
    rather than being trusted to pass the right one.

    **Confirming a force stop is a second token, not a flag on the first.** The confirmation
    screen used to re-offer the token the owner had just pressed, which cannot survive a
    render of the same message for the reason above; and a durable "confirmed" column had the
    same problem one layer down. A press that has been confirmed is now simply a press of a
    *different action*, so the two steps cannot be confused, nothing has to be remembered
    between them, and a restart in the middle loses neither.
    """

    def __init__(self, callbacks: CallbackStatePort) -> None:
        self._callbacks = callbacks

    def offer(
        self,
        session_id: SessionId,
        profile_id: ProfileId,
        state: SessionState,
        orphan_provenance: OrphanProvenance | None,
        action: str,
        owner_id: int,
        chat_id: int,
    ) -> str | None:
        """Offer one policy-permitted action, unbound until its screen is delivered.

        Takes provenance because `available_actions` does: after DEC-020 an ORPHANED
        record's rows depend on which producer created it, and this method is one of the
        two places the bot decides what to mint.
        """
        if action not in available_actions(state, orphan_provenance):
            return None
        return self._callbacks.create(
            action, f"{session_id}:{profile_id}", owner_id, chat_id, mutation=True
        )

    def offer_confirmed_force(
        self,
        session_id: SessionId,
        profile_id: ProfileId,
        state: SessionState,
        orphan_provenance: OrphanProvenance | None,
        owner_id: int,
        chat_id: int,
    ) -> str | None:
        """Mint the button the force confirmation screen carries, and only that button.

        `CONFIRMED_FORCE` is an adapter-internal action: `available_actions` is still the sole
        authority on whether a force is permitted at all (DEC-007), and it is re-asked here.
        """
        if FORCE not in available_actions(state, orphan_provenance):
            return None
        return self._callbacks.create(
            CONFIRMED_FORCE, f"{session_id}:{profile_id}", owner_id, chat_id, mutation=True
        )

    def claim(self, token: str, owner_id: int, chat_id: int, message_id: int) -> StopRequest | None:
        """Claim a token that may actually run, which an unconfirmed force never is.

        **This re-reads rather than re-resolves**, and the distinction is the whole of a
        Critical found at the plan's final gate. `PrivateBotBoundary.callback` has already
        resolved this token for this message before dispatching here, and every stop shows a
        pending notice first — so a Telegram round trip separates that resolve from this call.
        An activity notification delivered inside it moves the token onto the re-sent screen,
        and the second `resolve` this method used to perform then matched nothing: the owner
        was told "That action has already run" about a stop that had not run, with the pane
        still alive. `message_id` is kept in the signature because the caller has it and it
        documents which press this claim is about; it is deliberately not re-compared.
        """
        state = self._callbacks.reread(token, owner_id=owner_id, chat_id=chat_id)
        if state is None or state.action == FORCE:
            return None
        if not self._callbacks.claim_mutation(
            token, owner_id=owner_id, chat_id=chat_id, message_id=message_id
        ):
            return None
        session_value, profile_value = state.entity_id.split(":", maxsplit=1)
        action = FORCE if state.action == CONFIRMED_FORCE else state.action
        return StopRequest(action, SessionId.parse(session_value), ProfileId(profile_value))

    async def execute(self, request: StopRequest, service: object, record: object) -> StopResult:
        """Recheck the current record, dispatch one typed command, and report what it did.

        The return type is a `StopResult` rather than a bool because a bare bool could only
        answer "did anything run", and the thing BL-008 is about is the difference between a
        graceful stop that worked and one that did not — a distinction `graceful_stop`'s
        observation has always carried and this method used to discard. `dispatched` is the
        old bool, unchanged in meaning, so every refusal path answers exactly what it did.

        Graceful and force can both report; `cleanup` returns nothing at all, so there is
        nothing there to read.

        **Force is read through `force_stop_failure`, not `stop_failure`.** The latter keys on
        `preserved`, which is the success for a graceful stop and is false on *every* force,
        because force removes the pane rather than keeping it — routing force through it would
        report every completed kill as a failure. What force reports is the case DEC-017 names: the
        runtime found no managed pane, killed nothing, and the service recorded
        `VERIFIED_FORCE_STOP` anyway (DEC-017, deliberately, so the row still clears). The
        session does end; the claim that a kill was observed is what stops being made.
        """

        if record.session_id != request.session_id or record.profile_id != request.profile_id:
            return _NOT_DISPATCHED
        # The record is re-read here because it may have moved on since the token was
        # issued, but the rule it is checked against is the shared one. A private copy is
        # what let an offered action be silently refused at dispatch.
        if request.action not in available_actions(record.state, record.orphan_provenance):
            return _NOT_DISPATCHED
        if request.action == "graceful":
            observation = await service.graceful_stop(
                GracefulStopCommand(request.session_id, request.profile_id)
            )
            return StopResult(True, stop_failure(observation))
        if request.action == "cleanup":
            await service.cleanup(CleanupCommand(request.session_id))
            return StopResult(True)
        if request.action == FORCE:
            observation = await service.force_stop(ForceStopCommand(request.session_id))
            return StopResult(True, force_stop_failure(observation))
        # Force is its own named branch and an unrecognised action raises, rather than the kill
        # being the trailing `else`. It was, and nothing could reach it — the `available_actions`
        # recheck above admits only the three. But "anything I do not recognise is a kill" is a
        # fail-dangerous default in the one method that kills, and any future non-destructive
        # member of `available_actions` would have become a force stop here. The TUI's
        # `_issue_stop` removed exactly this shape and said why; this is the sibling it was
        # asymmetric with, found by the Stage 3 gate's adversarial pass.
        raise ValueError(f"no command is curated for the action {request.action!r}")
