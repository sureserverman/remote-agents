"""Every position the local surface can be in, one `Screen` each.

`ALL_SCREENS` is the registry `tests/unit/adapters/tui/test_screen_back_paths.py`
parametrizes over, so a screen added without a working back path fails a test rather than
stranding the owner on it.

**Only navigable screens belong in this namespace.** The Stage 2 gate sweeps `vars()` here
for `Screen` subclasses missing from `ALL_SCREENS`, so re-exporting a base class — or
`Screen` itself — would fail a check that is asking a fair question. `ChoiceScreen` and
`ConfirmScreen` are therefore imported from `.base` and `.confirm` by whoever needs them,
never from here.

`ALL_CONFIRMS` is re-exported alongside `ALL_SCREENS` because the Stage 3 gate sweeps it from
this namespace. It is a strict subset — the confirmations standing in front of a destructive
action — and its members are registered in `ALL_SCREENS` too: a modal is still a position the
owner can be in, still needs a back path, and still has a committed visual baseline.
"""

from __future__ import annotations

from remote_agents.adapters.tui.screens.confirm import (
    ALL_CONFIRMS,
    ForceConfirmModal,
    RemoteControlConfirmModal,
)
from remote_agents.adapters.tui.screens.dashboard import (
    DashboardScreen,
    ProjectChooserScreen,
    ProjectsPaneScreen,
)
from remote_agents.adapters.tui.screens.feed import FeedScreen
from remote_agents.adapters.tui.screens.launch import ProfilesScreen
from remote_agents.adapters.tui.screens.project import (
    AreasScreen,
    NameScreen,
    ProjectReviewScreen,
)
from remote_agents.adapters.tui.screens.resume import (
    ResumeConversationsScreen,
    ResumeProfilesScreen,
    ResumeProjectsScreen,
)
from remote_agents.adapters.tui.screens.sessions import (
    InspectScreen,
    OpeningAction,
    RenameScreen,
    SessionDetailScreen,
    SessionsPaneScreen,
    SessionsScreen,
)

#: Every screen the owner can reach, one class each. Nothing is repainted in place any more,
#: so this registry is the whole surface.
#:
#: **Deliberately not counted in words here.** This comment read "all fifteen positions" while
#: the tuple held nineteen entries: the numeral was written once and the registry grew past it
#: in silence, since nothing checks a count in prose. The membership *is* checked, in both
#: directions — `test_screen_back_paths.py` fails a screen listed here with no arrangement and
#: an arrangement naming no screen — so the list is the count, and a second copy of it in
#: English was only ever something to keep agreeing.
ALL_SCREENS = (
    DashboardScreen,
    ProjectsPaneScreen,
    SessionsPaneScreen,
    FeedScreen,
    ProjectChooserScreen,
    ProfilesScreen,
    AreasScreen,
    NameScreen,
    ProjectReviewScreen,
    SessionsScreen,
    SessionDetailScreen,
    RenameScreen,
    InspectScreen,
    ResumeProjectsScreen,
    ResumeProfilesScreen,
    ResumeConversationsScreen,
    ForceConfirmModal,
    RemoteControlConfirmModal,
)

__all__ = [
    "ALL_CONFIRMS",
    "ALL_SCREENS",
    "AreasScreen",
    "DashboardScreen",
    "FeedScreen",
    "ForceConfirmModal",
    "InspectScreen",
    "NameScreen",
    "OpeningAction",
    "ProfilesScreen",
    "ProjectChooserScreen",
    "ProjectReviewScreen",
    "ProjectsPaneScreen",
    "RemoteControlConfirmModal",
    "RenameScreen",
    "ResumeConversationsScreen",
    "ResumeProfilesScreen",
    "ResumeProjectsScreen",
    "SessionDetailScreen",
    "SessionsPaneScreen",
    "SessionsScreen",
]
