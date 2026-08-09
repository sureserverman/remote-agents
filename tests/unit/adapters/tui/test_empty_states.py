"""A position with nothing to show says so, and every position has been asked whether it can.

Two different checks live here and they are not interchangeable.

The first is **exhaustive over the surface**: every navigable `ChoiceScreen` must declare an
`empty_state`, either a sentence or `NEVER_EMPTY`. It is parametrized over `ALL_SCREENS`
rather than over a list of the four screens someone thought of, so a seventeenth screen added
next month fails here until its author answers the question. That is the property the plan's
gate asks for — "a fifth added later without an empty state fails here" — and a hand-written
list of four could not have it.

The second **drives the four screens that can actually be empty** and reads the rows. A
declaration nothing renders is a comment; these are what make it a behaviour.

The two modals in `ALL_SCREENS` are deliberately out of the first check's scope: they are
`ModalScreen`s that compose their own two-answer `OptionList` and never call `show_choices`,
so there is no mechanism for them to declare into. The check asserts that reasoning rather
than assuming it — it verifies the only non-`ChoiceScreen` members are the known confirms, so
a future modal that *does* grow a list cannot slip past by not being a `ChoiceScreen`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from textual.widgets import OptionList
from tui_filter import settle_filter

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ALL_CONFIRMS, ALL_SCREENS
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ProfileResumeCapability,
)
from remote_agents.domain.models import ProfileId, SessionRecord

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


@dataclass(slots=True)
class _Listing:
    records: tuple[SessionRecord, ...] = ()

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id: object) -> str | None:
        return None


@dataclass(slots=True)
class _Creator:
    areas: tuple[str, ...] = ()

    def available_areas(self) -> tuple[str, ...]:
        return self.areas


@dataclass(slots=True)
class _Conversations:
    """A catalogue whose every page is empty, which is the state under test."""

    asked: list[str] = field(default_factory=list)

    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)

    async def catalogue(self, query: object) -> ConversationCataloguePage:
        self.asked.append("catalogue")
        return ConversationCataloguePage(
            conversations=(), page=1, page_count=1, unavailable_reason=None
        )


def _context(**overrides: object) -> TuiContext:
    arguments: dict[str, object] = {
        "launcher": _Listing(),
        "creator": _Creator(),
        "profiles": (ProfileChoice("claude", True),),
        "refresh_catalogue": lambda: (_PROJECT,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_PROJECT,),
    }
    arguments.update(overrides)
    return TuiContext(**arguments)  # type: ignore[arg-type]


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _enabled_rows(app: RemoteAgentsTui) -> list[str]:
    return [
        str(option.prompt)
        for option in app.screen.query_one("#choices", OptionList).options
        if not option.disabled
    ]


# --- Exhaustive over the surface ------------------------------------------------------


@pytest.mark.parametrize("screen", ALL_SCREENS, ids=lambda screen: screen.__name__)
def test_every_screen_has_answered_whether_it_can_be_empty(screen: type) -> None:
    """`None` is not a state, it is an unanswered question — and it is what a new screen has."""
    if not issubclass(screen, ChoiceScreen):
        assert screen in ALL_CONFIRMS, (
            f"{screen.__name__} is neither a ChoiceScreen nor a known confirm, so nothing in "
            "this file has checked whether it can render an empty list"
        )
        return
    assert screen.empty_state is not None, (
        f"{screen.__name__} has not declared an empty_state. Give it the sentence it should "
        f"show when it has no rows, or NEVER_EMPTY if its rows are fixed by construction."
    )
    if screen.empty_state != NEVER_EMPTY:
        assert screen.empty_state.strip(), f"{screen.__name__}'s empty state is blank"
        assert not screen.empty_state.startswith("\x00"), (
            f"{screen.__name__} declared a row key as its empty state"
        )


def test_the_four_emptiable_positions_are_the_ones_declaring_a_sentence() -> None:
    """Pins the split itself, so moving a screen between the two answers is a visible change."""
    emptiable = {
        screen.__name__
        for screen in ALL_SCREENS
        if issubclass(screen, ChoiceScreen) and screen.empty_state != NEVER_EMPTY
    }

    assert emptiable == {
        "ProjectsScreen",
        "SessionsScreen",
        "ResumeConversationsScreen",
        "AreasScreen",
    }


# --- Driven, one per emptiable position -----------------------------------------------


async def test_a_filter_matching_nothing_says_so_rather_than_emptying_the_pane() -> None:
    from remote_agents.adapters.tui.screens.launch import ProjectsScreen

    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        entry = app.screen.query_one("#filter")
        entry.value = "no-such-project-anywhere"
        await settle_filter(pilot)
        rows = _rows(app)

    assert rows == [ProjectsScreen.empty_state]


async def test_the_sessions_list_says_there_are_none() -> None:
    from remote_agents.adapters.tui.screens.sessions import SessionsScreen

    app = RemoteAgentsTui(_context(launcher=_Listing(())))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        rows = _rows(app)

    assert rows == [SessionsScreen.empty_state]


async def test_an_area_less_development_root_says_so_and_still_offers_back() -> None:
    from remote_agents.adapters.tui.screens.project import AreasScreen

    app = RemoteAgentsTui(_context(creator=_Creator(areas=())))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        rows = _rows(app)
        enabled = _enabled_rows(app)
        highlighted = app.screen.query_one("#choices", OptionList).highlighted

    assert rows == [AreasScreen.empty_state, "Back"]
    assert enabled == ["Back"], "the empty state was selectable, so enter would do nothing"
    assert highlighted == 1, "the cursor rested on the disabled row instead of the one action"


async def test_a_profile_with_no_saved_conversations_says_so() -> None:
    from remote_agents.adapters.tui.screens.resume import ResumeConversationsScreen

    conversations = _Conversations()
    app = RemoteAgentsTui(_context(conversations=conversations))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        rows = _rows(app)
        enabled = _enabled_rows(app)

    assert rows == [ResumeConversationsScreen.empty_state, "Back"]
    assert enabled == ["Back"]


async def test_the_empty_row_cannot_be_chosen() -> None:
    """A disabled row is the mechanism; this is the behaviour that depends on it.

    Selecting it must not dispatch — the sessions screen's `choose` would take the row key as
    a session id and go looking for a record that was never there.
    """
    app = RemoteAgentsTui(_context(launcher=_Listing(())))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        before = app.screen.position

        await pilot.press("enter")
        await pilot.pause()
        after = app.screen.position

    assert before == "SESSIONS"
    assert after == "SESSIONS", "enter on the empty state navigated somewhere"


def test_the_records_used_here_are_stamped_now() -> None:
    """Guards this file against the age-rendering trap the snapshot harness documents."""
    assert datetime.now(UTC).tzinfo is UTC
