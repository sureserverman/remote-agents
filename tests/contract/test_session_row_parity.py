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
assert the policy against something that is not a second copy of itself, so they fail when
the policy changes -- which is the whole of what this file now claims. Two compare against
literals (`_ORPHAN_CASES`); the third compares `state_word(state, None)` against
`state.value`, which is the enum rather than a literal. It still has teeth -- any change to
the word mapping breaks it -- but "against literals" named the mechanism more strongly than
it is, and the distinction is the kind this file exists to keep straight.

**One test here does compare the two surfaces, and can fail when one drops a piece:**
`test_both_surfaces_draw_the_same_rollover_notes` drives the bot's sessions page and the local
sessions screen over one backend and reads the rollover note off what each actually drew
(DEC-091). A surface that stopped handing its rows the rollover mark would draw no note there.

Rewritten at Task 2.4 of the shared-use-cases sub-plan, under DEC-019: the merge is
permitted, and leaving a docstring claiming a detection the file can no longer perform is the
defect that decision exists to prevent.
"""

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.telegram.service import build_private_bot

# Both of these are now `application.session_views.session_row`, re-exported. The two import
# paths are kept deliberately: they are what a surface would have to stop offering before it
# could hold its own copy again, so importing them here still asserts something -- just not
# what the assertions below appear to assert. See the module docstring.
from remote_agents.adapters.telegram.service import session_lines as _session_lines_label
from remote_agents.adapters.telegram.service import session_row as _session_row_label
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.model import session_lines, session_row
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.rollover_book import RolloverBook
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
from remote_agents.domain.rollover import RolloverState


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


@pytest.mark.parametrize(("provenance", "expected"), _ORPHAN_CASES)
def test_both_surfaces_render_the_same_two_line_row(provenance, expected) -> None:
    """The 2026-09-02 redesign's row, pinned the way `session_row` is: both import paths reach
    `application/session_views.session_lines`, and the second line carries the state word."""
    record = _record(SessionState.ORPHANED, provenance)

    assert session_lines(record) == _session_lines_label(record)
    assert session_lines(record)[1].startswith(f"{expected} · ")


# Rollover notes, drawn by both surfaces (Stage 3 Task 3.1) -------------------------------------

_PROJECT = CatalogProject("opaque-editor", "editor", "infra", "Registered")
_AT = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def _numbered(sequence: int, state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=sequence)),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", sequence),
        state,
        datetime.now(UTC),
    )


#: #1 handed over to #2 (COMPLETED); #3's rollover failed after launching #5, which is gone;
#: #4 asked for one. Each listed row's note, as both surfaces must draw it.
_LISTED = (
    _numbered(1, SessionState.PRESERVED),
    _numbered(2),
    _numbered(3),
    _numbered(4),
)
_EXPECTED_NOTES = {
    1: "continued as #2",
    2: "continued from #1",
    3: "rollover failed · predecessor preserved",
    4: "rollover pending · waiting for workflow boundary",
}


async def _rolled_book(path: Path) -> RolloverBook:
    store = SQLiteRolloverStore(open_database(path / "sessions.sqlite3"))
    walks = (
        (_LISTED[0], _numbered(2), "h-0123456789abcdef0001", RolloverState.COMPLETED),
        (_LISTED[2], _numbered(5), "h-0123456789abcdef0003", RolloverState.FAILED),
    )
    for predecessor, successor, handoff, last in walks:
        opened = await store.open_for_ready(
            predecessor.session_id,
            handoff,
            project_id=predecessor.project_id,
            profile_id=predecessor.profile_id,
            plan=None,
            at=_AT,
        )
        assert opened is not None
        steps = [RolloverState.SUCCESSOR_STARTING, RolloverState.ADOPTING]
        if last is RolloverState.COMPLETED:
            steps += [RolloverState.SUCCESSOR_ACCEPTED, RolloverState.PREDECESSOR_STOPPING]
        for offset, state in enumerate([*steps, last], start=1):
            successor_id = (
                successor.session_id if state is RolloverState.SUCCESSOR_STARTING else None
            )
            await store.advance(
                opened.id,
                state,
                at=_AT + timedelta(seconds=offset),
                successor_session_id=successor_id,
            )
    book = RolloverBook(store, now=lambda: _AT + timedelta(minutes=1))
    requester = _LISTED[3]
    await book.request(
        requester.session_id, project_id=requester.project_id, profile_id=requester.profile_id
    )
    return book


class _Listing(SessionUseCaseDouble):
    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return _LISTED


def _backend(book: RolloverBook):
    return backend_for(
        sessions=_Listing(),
        projects=object(),
        catalogue=(_PROJECT,),
        refresh_catalogue=lambda: (_PROJECT,),
        rollovers=book,
    )


async def _bot_notes(book: RolloverBook) -> dict[int, str]:
    """Each row's second line as the bot sent it, unescaped, keyed by its sequence."""
    boundary = build_private_bot(7, 11, backend=_backend(book))
    reply = await boundary._sessions_reply()
    lines = dict(re.findall(r"</b> #(\d+)\n<code>(.*?)</code>", reply.text))
    return {int(sequence): html.unescape(line) for sequence, line in lines.items()}


async def _tui_rows(book: RolloverBook) -> dict[int, str]:
    """Each row of the local sessions screen as drawn, keyed by its sequence."""
    app = RemoteAgentsTui(
        TuiContext(
            backend=_backend(book),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )
    async with app.run_test(size=(220, 40)) as pilot:
        await app.action_sessions()
        for _ in range(200):
            await pilot.pause()
            found = app.screen.query("#choices")
            options = found.first(OptionList).options if found else ()
            rows = [str(option.prompt) for option in options]
            if len(rows) == len(_LISTED):
                break
        drawn = {}
        for row in rows:
            match = re.search(r" #(\d+)(?: |$)", row)
            assert match is not None, row
            drawn[int(match.group(1))] = row
        return drawn


async def test_both_surfaces_draw_the_same_rollover_notes(tmp_path: Path) -> None:
    """The one comparison in this file with teeth: each surface's own rendered rows, one backend.

    The bot's second line ends with the note; the local row carries it after the sequence. The
    words are pinned against literals, so a surface that dropped the mark (or drew the note
    from anywhere but `session_views`) fails here rather than agreeing with itself.
    """
    book = await _rolled_book(tmp_path)

    bot = await _bot_notes(book)
    tui = await _tui_rows(book)

    assert set(bot) == set(tui) == set(_EXPECTED_NOTES)
    for sequence, note in _EXPECTED_NOTES.items():
        assert bot[sequence].endswith(f" · {note}"), (sequence, bot[sequence])
        assert f"#{sequence} · {note}" in tui[sequence], (sequence, tui[sequence])


async def test_without_a_rollover_book_neither_surface_draws_a_rollover_note() -> None:
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=_Listing(), rollovers=None))
    reply = await boundary._sessions_reply()

    for note in _EXPECTED_NOTES.values():
        assert note not in html.unescape(reply.text)
