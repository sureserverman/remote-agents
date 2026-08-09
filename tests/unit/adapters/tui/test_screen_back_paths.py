"""Every registered screen walks back out to the resting position, and never off the stack.

The failure this exists to catch is the one the stage's own Risk line names: a navigation
path that silently strands the owner on a screen with no way back. No behavioural test
catches that, because a stranded screen renders perfectly — it just never leaves.

Two halves, and both are needed:

* `test_every_registered_screen_is_reachable_by_this_file` is the *exhaustiveness* half. It
  is the reason adding a screen without a back path cannot pass quietly: a new entry in
  `ALL_SCREENS` with no arrangement here fails that test rather than being skipped by a
  parametrization that only walks what it already knew about. A sweep with a blind spot
  reads exactly like a clean one, and this stage has already been bitten by one.
* The parametrized walk is the *behavioural* half: escape, repeatedly, must land on
  `ProjectsScreen` and must never raise. `pop_screen` raises `ScreenStackError` on the last
  screen (`textual/app.py`), so "the stack cannot empty" is not a convention every back path
  has to remember — it is a property of installing the resting position as the app's default
  screen, and this is what proves that property holds from every position.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from textual.screen import Screen

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import (
    ALL_SCREENS,
    AreasScreen,
    ForceConfirmScreen,
    InspectScreen,
    LabelScreen,
    NameScreen,
    ProfilesScreen,
    ProjectReviewScreen,
    ProjectsScreen,
    RemoteControlConfirmScreen,
    ResumeConfirmScreen,
    ResumeConversationsScreen,
    ResumeProfilesScreen,
    ResumeProjectsScreen,
    ReviewScreen,
    SessionDetailScreen,
    SessionsScreen,
)
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()
_REFERENCE = ConversationReference("c-" + "0" * 14 + "01")


def _record() -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _summary() -> ConversationSummary:
    return ConversationSummary(
        _REFERENCE,
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )


@dataclass(slots=True)
class _Launcher:
    record: SessionRecord = field(default_factory=_record)

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None


class _Creator:
    def available_areas(self):
        return ("dev-area", "infra")


class _Conversations:
    async def catalogue(self, query):
        return ConversationCataloguePage((_summary(),), query.page, 1)

    async def resolve_for_resume(self, _reference):
        return ResolvedConversation(_summary(), None)  # type: ignore[arg-type]

    async def capabilities(self):
        return (
            ProfileResumeCapability(
                ProfileId("claude"), catalogue_available=True, selected_resume_available=True
            ),
        )


def _context() -> TuiContext:
    return TuiContext(
        launcher=_Launcher(),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        capture=lambda _session_id: _captured(),
        conversations=_Conversations(),  # type: ignore[arg-type]
    )


async def _captured() -> str:
    return "some output"


_CAPABLE = (
    ProfileResumeCapability(
        ProfileId("claude"), catalogue_available=True, selected_resume_available=True
    ),
)
_PAGE = ConversationCataloguePage((_summary(),), 1, 1)
_RESOLVED = ResolvedConversation(_summary(), None)  # type: ignore[arg-type]


# Screens reached by pushing an instance directly. Constructor arguments are supplied here
# rather than defaulted on the screens themselves: a screen that cannot be built without the
# state it renders is the point of moving the seven navigation fields onto them.
_DIRECT: dict[type[Screen], Callable[[], Screen]] = {
    ProfilesScreen: ProfilesScreen,
    LabelScreen: LabelScreen,
    ReviewScreen: ReviewScreen,
    AreasScreen: AreasScreen,
    NameScreen: lambda: NameScreen("infra"),
    ProjectReviewScreen: lambda: ProjectReviewScreen("infra", "new-project"),
    SessionsScreen: SessionsScreen,
    SessionDetailScreen: lambda: SessionDetailScreen(str(_SESSION_ID)),
    InspectScreen: lambda: InspectScreen("output", "some output"),
    ResumeProjectsScreen: ResumeProjectsScreen,
    ResumeProfilesScreen: lambda: ResumeProfilesScreen(_PROJECT, _CAPABLE),
    ResumeConversationsScreen: lambda: ResumeConversationsScreen(_PROJECT, "claude", _PAGE),
    ResumeConfirmScreen: lambda: ResumeConfirmScreen(_PROJECT, "claude", _RESOLVED),
    ForceConfirmScreen: lambda: ForceConfirmScreen(str(_SESSION_ID), _record()),
    RemoteControlConfirmScreen: lambda: RemoteControlConfirmScreen(str(_SESSION_ID), _record()),
}


def test_every_registered_screen_is_reachable_by_this_file() -> None:
    """Adding a screen to the registry without arranging it here fails, rather than skipping.

    This is the half that makes the parametrization below a sweep rather than a sample. A
    screen absent from `_DIRECT` and from the two special cases would otherwise simply never
    be walked, and the file would stay green while covering less than it claims.
    """
    arranged = set(_DIRECT) | {ProjectsScreen}
    assert set(ALL_SCREENS) == arranged, (
        "every screen in ALL_SCREENS needs an arrangement in this file, and every "
        "arrangement needs to still be registered"
    )


async def _arrange(app: RemoteAgentsTui, pilot, screen_type: type[Screen]) -> None:
    """Put `screen_type` on the stack, the way the surface itself gets there."""
    if screen_type is ProjectsScreen:
        return
    await app.push_screen(_DIRECT[screen_type]())
    await pilot.pause()


@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
async def test_escape_reaches_the_resting_position_without_emptying_the_stack(
    screen_type: type[Screen],
) -> None:
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        assert isinstance(app.screen, screen_type), (
            f"arranging {screen_type.__name__} did not leave it on screen"
        )

        # Bounded on purpose: an unbounded loop against a screen that refuses to leave would
        # hang the run rather than fail it, and "this position never goes back" is exactly
        # the defect being looked for.
        for _ in range(10):
            if isinstance(app.screen, ProjectsScreen):
                break
            await pilot.press("escape")
            await pilot.pause()
        else:
            pytest.fail(
                f"escape never reached the project list from {screen_type.__name__}; "
                f"the owner is stranded on {type(app.screen).__name__}"
            )

        # Escape at rest must be inert rather than fatal. `pop_screen` raises on the last
        # screen, so this is the assertion that the resting position being the app's default
        # screen — not a pushed one — is what keeps the stack from ever emptying.
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert isinstance(app.screen, ProjectsScreen)
