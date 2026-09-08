"""The bot names a session's state with the lifecycle's word, never one of its own.

DEC-029 puts the vocabulary in `application/session_actions.state_word`, so a new lifecycle
state should reach this surface with no adapter change at all. That claim is easy to make and
easy to be wrong about — an adapter can carry a private mapping that happens to agree today —
so it is asserted here from the outside: the word appears in what the bot renders, and (in the
Stage 1 gate's companion sweep) nowhere in the bot's own source.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remote_agents.application.session_views import session_row
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


def test_a_trust_blocked_session_reads_untrusted_in_the_list() -> None:
    assert " · untrusted · " in session_row(_record(SessionState.UNTRUSTED))


@pytest.mark.parametrize(
    "state", [state for state in SessionState if state is not SessionState.ENDED]
)
def test_every_listable_state_contributes_a_word_to_its_row(state: SessionState) -> None:
    """Reflective, so the next state added fails here until it has a word.

    Read from the right: a rendered identity carries its own " · " separators, so the word is
    the second-to-last field and the age is the last. Counting from the left would be counting
    across a part whose width depends on whether the session has a custom label.
    """
    fields = session_row(_record(state)).split(" · ")

    assert fields[-2].strip()
