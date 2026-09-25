"""Under console hosting no screen draws a Textual Header or Footer (R9, DEC-105).

The console's function keys are drawn once, by tmux, on the window's status line. A Textual
Footer under it would draw them a second time, and a Header spends a row on a title the pane's
border already carries. Bare `remote-agents tui` has no tmux bar, so it keeps both.

**The screens are found by sweep, not listed.** Every `Screen` subclass defined in
`adapters/tui/screens/` is collected by importing the package, and the arrangement table below
must cover exactly that set, less the two bases no one navigates to. A new screen with no
arrangement fails `test_every_screen_in_the_package_is_arranged` instead of being skipped.

**Nothing the Footer said is lost.** Every key a bare Footer draws that is not a function key
(the bar draws those) must appear, by the key and its word, in the screen's hint row under
console hosting. The expectation is read off the bare Footer's own widgets, never restated.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from backends import SessionUseCaseDouble, backend_for
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static
from textual.widgets._footer import FooterKey

import remote_agents.adapters.tui.screens as screens_package
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.keys import FUNCTION_KEYS
from remote_agents.adapters.tui.screens import (
    AreasScreen,
    DashboardScreen,
    FeedScreen,
    ForceConfirmModal,
    InspectScreen,
    LimitsPaneScreen,
    NameScreen,
    ProfilesScreen,
    ProjectChooserScreen,
    ProjectReviewScreen,
    ProjectsPaneScreen,
    RemoteControlConfirmModal,
    RenameScreen,
    ResumeConversationsScreen,
    ResumeProfilesScreen,
    ResumeProjectsScreen,
    SessionDetailScreen,
    SessionsPaneScreen,
    SessionsScreen,
    SettingsScreen,
)
from remote_agents.adapters.tui.screens.base import ChoiceScreen, GatheredSelectionScreen
from remote_agents.adapters.tui.screens.confirm import (
    ConfirmScreen,
    HostPairingCodeModal,
    HostRemoteControlConfirmModal,
    HostRemoteControlDirectionModal,
)
from remote_agents.adapters.tui.screens.launch import ProjectsScreen
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import PairingCode, RemoteControlState

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()


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
        ConversationReference("c-" + "0" * 14 + "01"),
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )


@dataclass(slots=True)
class _Launcher(SessionUseCaseDouble):
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


async def _captured() -> str:
    return "some output"


def _context(*, console_hosted: bool) -> TuiContext:
    return replace(
        TuiContext(
            backend=backend_for(
                sessions=_Launcher(),  # type: ignore[arg-type]
                projects=_Creator(),  # type: ignore[arg-type]
                refresh_catalogue=lambda: (_PROJECT,),
                catalogue=(_PROJECT,),
                capture=lambda _session_id: _captured(),
            ),
            profiles=(ProfileAvailability("claude", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        ),
        console_hosted=console_hosted,
    )


_CAPABLE = (
    ProfileResumeCapability(
        ProfileId("claude"), catalogue_available=True, selected_resume_available=True
    ),
)
_PAGE = ConversationCataloguePage((_summary(),), 1, 1)

#: Every screen, by how it is put on the stack. `None` is the app's own resting position.
_ARRANGED: dict[type[Screen], Callable[[], Screen] | None] = {
    DashboardScreen: None,
    ProjectsScreen: ProjectsScreen,
    ProfilesScreen: ProfilesScreen,
    ProjectChooserScreen: lambda: ProjectChooserScreen(_PROJECT),
    AreasScreen: AreasScreen,
    NameScreen: lambda: NameScreen("infra"),
    ProjectReviewScreen: lambda: ProjectReviewScreen("infra", "new-project"),
    SessionsScreen: SessionsScreen,
    ProjectsPaneScreen: ProjectsPaneScreen,
    SessionsPaneScreen: SessionsPaneScreen,
    LimitsPaneScreen: LimitsPaneScreen,
    FeedScreen: FeedScreen,
    SessionDetailScreen: lambda: SessionDetailScreen(str(_SESSION_ID)),
    RenameScreen: lambda: RenameScreen(str(_SESSION_ID)),
    InspectScreen: lambda: InspectScreen("some output"),
    SettingsScreen: SettingsScreen,
    ResumeProjectsScreen: ResumeProjectsScreen,
    ResumeProfilesScreen: lambda: ResumeProfilesScreen(_PROJECT, _CAPABLE),
    ResumeConversationsScreen: lambda: ResumeConversationsScreen(_PROJECT, "claude", _PAGE),
    ConfirmScreen: lambda: ConfirmScreen("Stop it?", "Stop"),
    ForceConfirmModal: lambda: ForceConfirmModal.for_record(_record()),
    RemoteControlConfirmModal: lambda: RemoteControlConfirmModal.for_change(
        _record(), RemoteControlState.ACTIVE
    ),
    HostRemoteControlConfirmModal: lambda: HostRemoteControlConfirmModal.for_direction(
        RemoteControlState.ACTIVE
    ),
    HostRemoteControlDirectionModal: lambda: HostRemoteControlDirectionModal(
        (RemoteControlState.ACTIVE, RemoteControlState.INACTIVE)
    ),
    HostPairingCodeModal: lambda: HostPairingCodeModal(
        PairingCode(code="ABCD-1234", expires_at=datetime.now(UTC) + timedelta(minutes=5))
    ),
}

#: Bases, not positions: nothing navigates to one, so there is nothing to mount.
_BASES = {ChoiceScreen, GatheredSelectionScreen}

_FUNCTION_KEYS = {entry.key for entry in FUNCTION_KEYS}


def _swept() -> set[type[Screen]]:
    found: set[type[Screen]] = set()
    for module_info in pkgutil.iter_modules(screens_package.__path__):
        module = importlib.import_module(f"{screens_package.__name__}.{module_info.name}")
        for _name, member in inspect.getmembers(module, inspect.isclass):
            if issubclass(member, Screen) and member.__module__.startswith(
                screens_package.__name__
            ):
                found.add(member)
    return found


_SCREENS = sorted(_swept() - _BASES, key=lambda cls: cls.__name__)


def test_every_screen_in_the_package_is_arranged() -> None:
    swept = _swept() - _BASES
    assert swept == set(_ARRANGED), (
        "every Screen subclass in adapters/tui/screens/ needs an arrangement here: "
        f"missing {sorted(c.__name__ for c in swept - set(_ARRANGED))}, "
        f"stale {sorted(c.__name__ for c in set(_ARRANGED) - swept)}"
    )


@dataclass(frozen=True, slots=True)
class _Reading:
    headers: int
    footers: int
    #: The (key display, word) pairs the Footer drew on the top screen.
    footer_keys: frozenset[tuple[str, str]]
    #: The top screen's hint row, or `None` for a screen that has none.
    hint: str | None
    modal: bool


async def _read(screen_type: type[Screen], *, console_hosted: bool) -> _Reading:
    app = RemoteAgentsTui(_context(console_hosted=console_hosted))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        factory = _ARRANGED[screen_type]
        if factory is not None:
            await app.push_screen(factory())
            await pilot.pause()
        assert isinstance(app.screen, screen_type), f"{screen_type.__name__} is not on screen"
        stack = list(app.screen_stack)
        headers = sum(len(screen.query(Header)) for screen in stack)
        footers = sum(len(screen.query(Footer)) for screen in stack)
        footer_keys = frozenset(
            (widget.key_display, widget.description)
            for widget in app.screen.query(FooterKey)
            if widget.key not in _FUNCTION_KEYS and widget.description
        )
        hints = app.screen.query("#hint")
        hint = str(hints.first(Static).render()) if hints else None
        return _Reading(headers, footers, footer_keys, hint, isinstance(app.screen, ModalScreen))


@pytest.mark.parametrize("screen_type", _SCREENS, ids=lambda cls: cls.__name__)
async def test_console_hosting_draws_no_header_or_footer(screen_type: type[Screen]) -> None:
    reading = await _read(screen_type, console_hosted=True)
    assert (reading.headers, reading.footers) == (0, 0), (
        f"{screen_type.__name__} draws {reading.headers} Header(s) and {reading.footers} "
        "Footer(s) under console hosting; the tmux bar is the only key row there (DEC-105)"
    )


@pytest.mark.parametrize(
    "screen_type",
    [cls for cls in _SCREENS if not issubclass(cls, ModalScreen)],
    ids=lambda cls: cls.__name__,
)
async def test_a_bare_terminal_keeps_both_chrome_widgets(screen_type: type[Screen]) -> None:
    reading = await _read(screen_type, console_hosted=False)
    assert reading.headers >= 1 and reading.footers >= 1, (
        f"{screen_type.__name__} lost its chrome in a bare terminal, which has no tmux bar"
    )


@pytest.mark.parametrize(
    "screen_type",
    [cls for cls in _SCREENS if not issubclass(cls, ModalScreen)],
    ids=lambda cls: cls.__name__,
)
async def test_the_console_hint_row_names_every_key_the_footer_drew(
    screen_type: type[Screen],
) -> None:
    bare = await _read(screen_type, console_hosted=False)
    console = await _read(screen_type, console_hosted=True)
    assert console.hint is not None, f"{screen_type.__name__} has no hint row"
    for key, word in bare.footer_keys:
        assert key in console.hint and word in console.hint, (
            f"{screen_type.__name__}: the bare footer drew `{key} {word}`, and under console "
            f"hosting the hint row does not name it: {console.hint!r}"
        )
