"""Both surfaces name a session's state with one word, from one authority (BL-031).

DEC-020 split ORPHANED into an adopted live agent and a record whose evidence supports no
action. The detail screen already distinguished them -- different sentence, and only the
adopted half carries a Force stop row -- but both *list* rows read `· orphaned ·`, so an
owner scanning the list could not tell which without opening each one. The complaint DEC-020
answers is felt in the list, which is where the owner looks first.

The two surfaces had byte-identical copies of the row format, which is how they drifted into
the same defect at the same time and would have drifted apart the moment one was fixed alone.
`state_word` is the single authority now; this file is what keeps it single.
"""

from datetime import UTC, datetime

import pytest

from remote_agents.adapters.telegram.service import _session_row_label
from remote_agents.adapters.tui.model import session_row
from remote_agents.application.session_actions import state_word
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def _record(state: SessionState, provenance: OrphanProvenance | None = None) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        state,
        datetime.now(UTC),
        orphan_provenance=provenance,
    )


_ORPHAN_CASES = [
    (OrphanProvenance.ADOPTED, "adopted"),
    (OrphanProvenance.AMBIGUOUS, "unverifiable"),
    (None, "orphaned"),
]


@pytest.mark.parametrize(("provenance", "expected"), _ORPHAN_CASES)
def test_the_two_kinds_of_orphaned_read_differently(provenance, expected) -> None:
    """The whole point: a list row now says which kind it is.

    The `None` case keeps the bare word deliberately. A row written before migration 6 has no
    provenance and cannot have it back-derived, so `orphaned` unqualified is the honest
    answer -- it says "this is orphaned and nobody recorded which kind", which is true.
    """
    assert state_word(SessionState.ORPHANED, provenance) == expected


def test_the_three_orphan_cases_are_actually_distinguishable() -> None:
    """Guard against a future edit collapsing two of them back onto one word.

    Asserting each mapping individually would still pass if two of them were changed to the
    same string, which is the exact regression this closes.
    """
    words = {state_word(SessionState.ORPHANED, provenance) for provenance, _ in _ORPHAN_CASES}

    assert len(words) == 3


@pytest.mark.parametrize("state", list(SessionState))
def test_every_non_orphaned_state_still_reads_as_its_own_value(state) -> None:
    if state is SessionState.ORPHANED:
        pytest.skip("ORPHANED is the one state that does not use its own value")
    assert state_word(state, None) == state.value


@pytest.mark.parametrize(("provenance", "expected"), _ORPHAN_CASES)
def test_both_surfaces_render_the_same_row(provenance, expected) -> None:
    """The parity half. Two copies of one format is how they broke together."""
    record = _record(SessionState.ORPHANED, provenance)

    assert session_row(record) == _session_row_label(record)
    assert f" · {expected} · " in session_row(record)


@pytest.mark.parametrize("state", list(SessionState))
def test_both_surfaces_agree_for_every_state(state) -> None:
    record = _record(state)

    assert session_row(record) == _session_row_label(record)
