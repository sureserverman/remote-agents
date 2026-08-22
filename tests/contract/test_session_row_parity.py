"""What a session's state is called, and that neither surface can call it something else.

DEC-020 split ORPHANED into an adopted live agent and a record whose evidence supports no
action. The detail screen already distinguished them -- different sentence, and only the
adopted half carries a Force stop row -- but both *list* rows read `· orphaned ·`, so an
owner scanning the list could not tell which without opening each one. The complaint DEC-020
answers is felt in the list, which is where the owner looks first.

**This file no longer detects divergence between the surfaces, and says so rather than
implying otherwise (DEC-019).** It used to compare two byte-identical copies of the row
format -- `adapters/telegram/service.py: _session_row_label` against
`adapters/tui/model.py: session_row` -- and that comparison could genuinely fail, because
either copy could be edited alone. There is now one `application/session_views.py:
session_row`, and both imports below resolve to it. `test_both_surfaces_render_the_same_row`
and `test_both_surfaces_agree_for_every_state` therefore compare a function with itself:
**they cannot fail, and they are kept only because both import paths must keep resolving.**
An adapter that grew its own copy again would be caught by
`tests/unit/application/test_session_views.py:
test_no_adapter_redefines_the_row_or_the_area_predicate`, not by anything here.

What still has teeth in this file is everything about `state_word`: that the three ORPHANED
cases stay three distinct words, and that every other state reads as its own value. Those
assert the policy against literals rather than against a second copy of itself, so they fail
when the policy changes -- which is the whole of what this file now claims.

Rewritten at Task 2.4 of the shared-use-cases sub-plan, under DEC-019: the merge is
permitted, and leaving a docstring claiming a detection the file can no longer perform is the
defect that decision exists to prevent.
"""

from datetime import UTC, datetime

import pytest

# Both of these are now `application.session_views.session_row`, re-exported. The two import
# paths are kept deliberately: they are what a surface would have to stop offering before it
# could hold its own copy again, so importing them here still asserts something -- just not
# what the assertions below appear to assert. See the module docstring.
from remote_agents.adapters.telegram.service import session_row as _session_row_label
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
    """Now a tautology on its first line and a real assertion on its second.

    The equality compares one function with itself and cannot fail. The `in` check below it
    still pins the rendered word against a literal, which is what makes this parametrization
    worth keeping at all.
    """
    record = _record(SessionState.ORPHANED, provenance)

    assert session_row(record) == _session_row_label(record)
    assert f" · {expected} · " in session_row(record)


@pytest.mark.parametrize("state", list(SessionState))
def test_both_surfaces_agree_for_every_state(state) -> None:
    """Kept as a resolution check, not a divergence check: it asserts that both import paths
    still reach the shared function, and nothing more. It cannot fail on a format change."""
    record = _record(state)

    assert session_row(record) == _session_row_label(record)
