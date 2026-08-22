"""What a session looks like in a list, and which directories may be offered as areas.

Both of these were written twice, once per frontend, with byte-identical bodies — the row at
`adapters/telegram/service.py: _session_row_label` and `adapters/tui/model.py: session_row`,
each carrying a comment naming the other (BL-031); the area predicate at
`_selectable_area`/`selectable_area`, docstring included.

**This is the move DEC-029 already made once, finished.** That decision put `state_word` into
`application/session_actions.py` on the argument that what a state is *called* is lifecycle
policy rather than surface rendering, and that byte-identical copies are how the two surfaces
broke together. `state_word` moved; the line that renders it did not, so the two surfaces went
on holding their own copies of the format around a shared word.

**Rendering, not presentation.** These return plain strings for a frontend to place. Nothing
here escapes HTML, measures UTF-16 units, or knows what a keyboard is — DEC-014 keeps the
encodable-once boundary per surface, and a shared renderer sits *upstream* of both presenters
rather than absorbing either. `adapters/telegram/presenters.py` still escapes what it sends
and the Textual side still hands its rows to a widget.
"""

from __future__ import annotations

from collections.abc import Iterable

from remote_agents.application.relative_time import age
from remote_agents.application.session_actions import state_word
from remote_agents.domain.models import SessionRecord, SessionState
from remote_agents.domain.projects import ProjectIdentity


def session_row(record: SessionRecord) -> str:
    """One list row. The state word comes from the shared policy, never from `state.value`.

    Both surfaces had this exact line, independently, which is how they rendered both kinds
    of ORPHANED identically (BL-031). `state_word` is the single authority, and now so is
    this.

    The identity is `display.rendered` rather than the generated parts, so a renamed session
    reads under its new name. The two are indistinguishable on any record without a custom
    label, which is most of them, so it is pinned by a test rather than left to inspection.
    """
    word = state_word(record.state, record.orphan_provenance)
    return f"{record.display.rendered} · {word} · {age(record.created_at)}"


def selectable_area(value: str) -> bool:
    """Offer an existing directory only when the project identity rule also accepts it.

    A predicate where the domain answers by raising: a chooser screening candidate
    directories wants a yes or no per row, and `ProjectIdentity` is the authority on which
    ones could ever become a project. Catching `ValueError` alone is deliberate — anything
    else is a fault rather than a refusal, and swallowing it here would take the chooser down
    quietly on the value it was asked to screen.
    """
    try:
        ProjectIdentity(area=value, name=value)
    except ValueError:
        return False
    return True


def listed_in_sessions(record: SessionRecord) -> bool:
    """Whether a session belongs in the list a surface draws. Exactly ENDED does not.

    **The "exactly" is DEC-017's load-bearing word, not emphasis.** That decision keeps force
    stop clearing the record — a row the owner cannot clear was judged the worse failure than
    an over-confident message — and what makes a cleared row acceptable is that every *other*
    state stays visible and actionable. Widen this by one state and the sessions DEC-017
    promises remain reachable are stranded instead, with nothing to say so.

    So the test that guards it enumerates `SessionState` and asserts the whole set, rather
    than naming the states it happens to think of. The record is kept for audit either way;
    what ENDED has lost is anything left to reach, inspect or stop.

    **Provenance is not read, and must not be.** DEC-020 gives the two kinds of ORPHANED
    different *actions*; it gives them the same visibility, and an ORPHANED row is precisely
    the one an owner most needs to see.

    Both surfaces had this as an inline generator expression, and the TUI's carried a comment
    claiming it filtered "exactly as the bot filters it" — true when written, and checked by
    nothing.
    """
    return record.state is not SessionState.ENDED


def only_listed(records: Iterable[SessionRecord]) -> tuple[SessionRecord, ...]:
    """The listable subset, in the order given.

    Order is the caller's: neither surface sorts here, and the row's own age column is what
    tells the owner how old a session is.
    """
    return tuple(record for record in records if listed_in_sessions(record))
