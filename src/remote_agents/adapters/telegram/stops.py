"""Replay-safe callback state for the approved session stop actions."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.application.session_actions import (
    FORCE,
    available_actions,
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
