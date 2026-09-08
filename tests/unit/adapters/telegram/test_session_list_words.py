"""The bot names a session's state with the lifecycle's word, never one of its own.

DEC-029 puts the vocabulary in `application/session_actions.state_word`, so a new lifecycle
state should reach this surface with no adapter change at all. That claim is easy to make and
easy to be wrong about — an adapter can carry a private mapping that happens to agree today.

**Asserted through the renderer the bot actually calls.** `session_row` is re-exported by
`adapters/telegram/service.py` but explicitly *not used* by it (see the module's own `__all__`
docstring); the bot builds every row from `session_row_parts` at `service.py:1577,1583,1760`.
A test written against `session_row` would therefore pass while the bot's real rows said
something else entirely — which is what the first version of this file did.

The companion half of this claim is the Stage 1 gate's sweep, which greps the bot's source for
the word and expects no match: asserted here, spelled nowhere in the adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remote_agents.application.session_views import StateGroup, session_row_parts
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def _record(state: SessionState) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


def test_a_trust_blocked_session_reads_untrusted_in_the_bot_s_own_row() -> None:
    parts = session_row_parts(_record(SessionState.UNTRUSTED))

    assert parts.state == "untrusted"


def test_a_trust_blocked_session_is_filed_where_the_owner_looks_for_a_hand() -> None:
    """Not IN_TRANSITION: nothing is in flight, and it stays put until a person answers."""
    parts = session_row_parts(_record(SessionState.UNTRUSTED))

    assert parts.group is StateGroup.NEEDS_ATTENTION


def test_a_trust_blocked_row_draws_no_context_gauge() -> None:
    """A bar beside `untrusted` would read as a live figure for an agent that has run nothing.

    `session_row_parts` draws a gauge only for RUNNING, so this is the existing rule meeting
    the new state rather than a new one — pinned because the new state is live-paned, which is
    the property that would tempt a future edit to widen the gauge to it.
    """
    parts = session_row_parts(_record(SessionState.UNTRUSTED))

    assert parts.gauge is None


@pytest.mark.parametrize(
    "state", [state for state in SessionState if state is not SessionState.ENDED]
)
def test_every_listable_state_gets_its_own_word_from_the_shared_policy(
    state: SessionState,
) -> None:
    """Reflective, and it can actually fail: it compares against the *policy*, not the enum.

    The earlier version asserted only that the field was non-empty, which a `StrEnum` member
    guarantees for free — it could not have failed for any state. This one fails if the bot's
    row ever stops agreeing with `state_word`, which is the thing DEC-029 is about.
    """
    from remote_agents.application.session_actions import state_word

    record = _record(state)

    assert session_row_parts(record).state == state_word(record.state, record.orphan_provenance)
