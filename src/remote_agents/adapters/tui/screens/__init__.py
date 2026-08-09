"""Every position the local surface can be in, one `Screen` each.

`ALL_SCREENS` is the registry `tests/unit/adapters/tui/test_screen_back_paths.py`
parametrizes over, so a screen added without a working back path fails a test rather than
stranding the owner on it.

**Only navigable screens belong in this namespace.** The Stage 2 gate sweeps `vars()` here
for `Screen` subclasses missing from `ALL_SCREENS`, so re-exporting a base class — or
`Screen` itself — would fail a check that is asking a fair question. `ChoiceScreen` is
therefore imported from `.base` by whoever needs it, never from here.
"""

from __future__ import annotations

from remote_agents.adapters.tui.screens.confirm import (
    ForceConfirmScreen,
    RemoteControlConfirmScreen,
)
from remote_agents.adapters.tui.screens.launch import (
    LabelScreen,
    ProfilesScreen,
    ProjectsScreen,
    ReviewScreen,
)
from remote_agents.adapters.tui.screens.project import (
    AreasScreen,
    NameScreen,
    ProjectReviewScreen,
)
from remote_agents.adapters.tui.screens.resume import (
    ResumeConfirmScreen,
    ResumeConversationsScreen,
    ResumeProfilesScreen,
    ResumeProjectsScreen,
)
from remote_agents.adapters.tui.screens.sessions import (
    InspectScreen,
    SessionDetailScreen,
    SessionsScreen,
)

#: Every screen the owner can reach — all sixteen positions, one class each. Nothing is
#: repainted in place any more, so this registry is the whole surface.
ALL_SCREENS = (
    ProjectsScreen,
    ProfilesScreen,
    LabelScreen,
    ReviewScreen,
    AreasScreen,
    NameScreen,
    ProjectReviewScreen,
    SessionsScreen,
    SessionDetailScreen,
    InspectScreen,
    ResumeProjectsScreen,
    ResumeProfilesScreen,
    ResumeConversationsScreen,
    ResumeConfirmScreen,
    ForceConfirmScreen,
    RemoteControlConfirmScreen,
)

__all__ = [
    "ALL_SCREENS",
    "AreasScreen",
    "ForceConfirmScreen",
    "InspectScreen",
    "LabelScreen",
    "NameScreen",
    "ProfilesScreen",
    "ProjectReviewScreen",
    "ProjectsScreen",
    "RemoteControlConfirmScreen",
    "ResumeConfirmScreen",
    "ResumeConversationsScreen",
    "ResumeProfilesScreen",
    "ResumeProjectsScreen",
    "ReviewScreen",
    "SessionDetailScreen",
    "SessionsScreen",
]
