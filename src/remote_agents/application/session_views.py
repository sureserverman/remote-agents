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
from typing import Protocol

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


class _ListReadableSessions(Protocol):
    """The two questions a list open asks, named so this module needs no use-case import.

    A Protocol rather than `SessionService` because `application/session_views.py` renders and
    filters; importing the service to type one argument would tie a rendering module to the
    one that owns locks and a store. The two frontends pass the same object either way.
    """

    async def refresh_readiness(self) -> object: ...

    async def list_sessions(self) -> Iterable[SessionRecord]: ...


async def listed_sessions(sessions: _ListReadableSessions) -> tuple[SessionRecord, ...]:
    """Refresh readiness, then return the sessions worth showing. The list-open read.

    Both surfaces did exactly this, in exactly this order, and nothing held them together.
    Neither half was the duplicate — `refresh_readiness` was always one method and
    `only_listed` has been shared since Stage 2 — so what was written twice was the *pairing*,
    which is the kind of duplicate no sweep for a repeated name can see.

    **The pairing is the interesting part, because it is deliberately not applied everywhere.**
    The pass rescans every record and runs a tmux capture per FAILED session, so the paths
    that re-read one session — the bot's `_record`, the local surface's `current_record` —
    read without it, and both had written that reasoning down separately. Putting the pair
    here makes "a list open refreshes, a re-read does not" a single decision with one place to
    change it, instead of an agreement between two comments.

    Order is load-bearing and is the reason this is a function rather than two calls a caller
    makes: reading first would draw the list from records the pass is about to promote, so a
    launch that has just become ready would show as FAILED until the next open.

    What happens *after* is the caller's. The local surface hands the result to its console
    (ARCH-B3); the bot has no console to hand it to. That asymmetry stays in the frontends,
    because it is about what a surface hosts rather than about what the list is.
    """
    await sessions.refresh_readiness()
    return only_listed(await sessions.list_sessions())
